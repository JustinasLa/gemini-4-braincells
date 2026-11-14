"""Tools Gemini may call while it analyses one cluster (function calling, see agent.py).

Web tools (live Oxylabs Web API through sources/): `search_web`, `scrape_url`. Every result is
stored as an `evidence` row (kind='source', with its source_url) before the model sees it, and the
model gets that row's evidence_id back. So anything the verdict cites from the web has a stored,
clickable URL, and guard.py's id check covers tool results like any other evidence.

Lookup tools (read-only, our own DB): `get_cluster_posts`, `get_account`. They return context
only: nothing new to cite (the detector's signal evidence already covers that data).

Limits: a per-run `Budget` (total calls, web calls, wall time), a process-wide cache by URL/query
(and reuse of rows the sources worker already stored), and `scrape_url` only accepts URLs that
already appear in this cluster's posts or evidence, or in an earlier tool result of the same run.
"""

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg

from analyst.context import ClusterContext, EvidenceItem
from sources.checker import WebClient, _page_evidence, _search_evidence
from sources.links import checkable_url, domain
from sources.models import SourceEvidence
from sources.oxylabs import OxylabsError

log = logging.getLogger(__name__)

WEB_TOOLS = frozenset({"search_web", "scrape_url"})
MAX_SEARCH_HITS = 8
MAX_POSTS = 40


@dataclass
class Budget:
    """Hard limits for one agent run. Calls past a limit are refused, not executed."""

    max_tool_calls: int = 6
    max_web_calls: int = 4  # Oxylabs requests that are not served from a cache
    max_seconds: float = 120.0
    started: float = field(default_factory=time.monotonic)
    tool_calls: int = 0
    web_calls: int = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def exhausted(self) -> bool:
        """No further tool call of any kind is allowed."""
        return self.tool_calls >= self.max_tool_calls or self.elapsed() >= self.max_seconds

    def refusal(self) -> str | None:
        if self.tool_calls >= self.max_tool_calls:
            return f"tool call budget used up ({self.max_tool_calls} calls)"
        if self.elapsed() >= self.max_seconds:
            return f"time budget used up ({self.max_seconds:.0f} s)"
        return None

    def web_refusal(self) -> str | None:
        """Checked only before a live Oxylabs request: cache hits stay free."""
        if self.web_calls >= self.max_web_calls:
            return f"web call budget used up ({self.max_web_calls} live calls)"
        return None


class BudgetRefused(Exception):
    pass


class EvidenceSink(Protocol):
    """Where tool evidence is stored. Returns ids the verdict may cite."""

    def existing(self, key: str) -> EvidenceItem | None: ...
    def save(self, ev: SourceEvidence) -> EvidenceItem: ...


class DbSink:
    """Stores tool evidence for one cluster, committing each row so a failed verdict keeps it."""

    def __init__(self, conn: psycopg.Connection, cluster_id: int):
        self._conn = conn
        self._cluster_id = cluster_id

    def existing(self, key: str) -> EvidenceItem | None:
        r = self._conn.execute(
            """
            SELECT id, kind, summary, detail, source_url, model FROM evidence
            WHERE cluster_id = %s AND kind = 'source' AND detail->>'key' = %s
            ORDER BY id DESC LIMIT 1
            """,
            (self._cluster_id, key),
        ).fetchone()
        return _item(r["id"], r) if r else None

    def save(self, ev: SourceEvidence) -> EvidenceItem:
        from sources.store import save_evidence  # upsert by detail.key, ids stay stable

        [eid] = save_evidence(self._conn, self._cluster_id, [ev])
        self._conn.commit()
        return _item(eid, ev.model_dump())


class MemorySink:
    """Dry runs and tests: ids from a private range, nothing written."""

    def __init__(
        self,
        start_id: int = 900_000_000,
        *,
        read_through: Callable[[str], EvidenceItem | None] | None = None,
    ):
        self._next = start_id
        self._read_through = read_through  # e.g. DbSink(...).existing: reuse stored rows
        self.items: dict[str, EvidenceItem] = {}

    def existing(self, key: str) -> EvidenceItem | None:
        if key in self.items:
            return self.items[key]
        return self._read_through(key) if self._read_through else None

    def save(self, ev: SourceEvidence) -> EvidenceItem:
        item = _item(self._next, ev.model_dump())
        self._next += 1
        self.items[ev.detail["key"]] = item
        return item


def _item(eid: int, r: dict) -> EvidenceItem:
    return EvidenceItem(
        id=eid,
        kind="source",
        summary=r.get("summary") or "",
        detail=r.get("detail") or {},
        source_url=r.get("source_url"),
        model=r.get("model"),
    )


class WebCache:
    """Process-wide cache of Oxylabs results by URL/query, shared across clusters and runs.

    Holds the evidence *content*; each cluster still gets its own evidence row. Failures are
    cached for a shorter time so a broken page is not retried on every cluster.
    """

    def __init__(self, ttl_s: float = 6 * 3600, failure_ttl_s: float = 1800):
        self._ttl = ttl_s
        self._failure_ttl = failure_ttl_s
        self._data: dict[str, tuple[float, SourceEvidence | None]] = {}

    def get(self, key: str) -> tuple[bool, SourceEvidence | None]:
        hit = self._data.get(key)
        if hit and time.monotonic() < hit[0]:
            return True, hit[1]
        return False, None

    def put(self, key: str, value: SourceEvidence | None) -> None:
        ttl = self._ttl if value is not None else self._failure_ttl
        self._data[key] = (time.monotonic() + ttl, value)


# --- declarations the model sees (JSON schema, Gemini function calling) ---

DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "search_web",
        "description": (
            "Live web search (Oxylabs Web API /search). Use it to find where a claim, headline or "
            "phrase from the cluster appears elsewhere, who published it first, or whether "
            "mainstream outlets report the same event (which would point to an organic reaction). "
            "Put an exact phrase in double quotes. Returns result URLs, titles, snippets, dates "
            "and an evidence_id you can cite."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query, max 200 characters"},
                "why": {"type": "string", "description": "One short sentence: what you check"},
            },
            "required": ["query", "why"],
        },
    },
    {
        "name": "scrape_url",
        "description": (
            "Fetch one web page live (Oxylabs Web API /scrape) and extract its publisher, author, "
            "publication date, headline and main claims. Only URLs that appear in this cluster's "
            "posts or evidence, or in an earlier search_web result, are allowed. Returns an "
            "evidence_id you can cite."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http(s) URL to fetch"},
                "why": {"type": "string", "description": "One short sentence: what you check"},
            },
            "required": ["url", "why"],
        },
    },
    {
        "name": "get_cluster_posts",
        "description": (
            "Read the cluster's stored posts from our database: author handle, time, text and "
            "linked URLs. Context only: nothing here is citable by itself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": f"Max posts, 1-{MAX_POSTS}"},
            },
        },
    },
    {
        "name": "get_account",
        "description": (
            "Read one cluster member's profile from our database (handle, account age, "
            "followers, posts count, avatar/bio presence, PDS host). Context only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "DID or handle of a cluster member"},
            },
            "required": ["account"],
        },
    },
]


@dataclass
class ToolCallRecord:
    name: str
    args: dict
    latency_ms: int
    ok: bool
    cached: bool = False  # served from the process cache or an already stored row
    evidence_id: int | None = None
    source_url: str | None = None
    endpoint: str | None = None  # Oxylabs endpoint, for web tools
    error: str | None = None
    why: str | None = None


class ToolBox:
    """Executes the model's tool calls for one cluster run."""

    def __init__(
        self,
        ctx: ClusterContext,
        *,
        web: WebClient | None,
        sink: EvidenceSink,
        cache: WebCache,
        budget: Budget,
        conn: psycopg.Connection | None = None,
        requested_by: str = "gemini",
    ):
        self.ctx = ctx
        self._web = web
        self._sink = sink
        self._cache = cache
        self.budget = budget
        self._conn = conn
        self._requested_by = requested_by
        self.new_evidence: dict[int, EvidenceItem] = {}
        # every evidence item a tool returned to the model (new or already stored): citable
        self.returned: dict[int, EvidenceItem] = {}
        self.records: list[ToolCallRecord] = []
        self._allowed_urls: set[str] = set()
        for e in ctx.evidence:
            self._allow(_urls_in(e.detail))
            if e.source_url:
                self._allow([e.source_url])
        if conn is not None:
            self._allow(
                r["url"]
                for r in conn.execute(
                    """
                    SELECT DISTINCT unnest(s.embed_urls) AS url FROM cluster_posts cp
                    JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
                    WHERE cp.cluster_id = %s LIMIT 500
                    """,
                    (ctx.id,),
                ).fetchall()
            )

    def declarations(self) -> list[dict[str, Any]]:
        names = {"get_cluster_posts", "get_account"} if self._conn is not None else set()
        if self._web is not None:
            names |= WEB_TOOLS
        return [d for d in DECLARATIONS if d["name"] in names]

    def allow_urls(self, urls: Iterable[str]) -> None:
        self._allow(urls)

    def _allow(self, urls: Iterable[str]) -> None:
        for u in urls:
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                self._allowed_urls.add(u.rstrip("/"))
                norm = checkable_url(u)
                if norm:
                    self._allowed_urls.add(norm.rstrip("/"))

    def call(self, name: str, args: dict) -> dict:
        """Run one tool call and return the function response for the model."""
        args = dict(args or {})
        started = time.monotonic()
        rec = ToolCallRecord(name=name, args=args, latency_ms=0, ok=False, why=args.get("why"))
        try:
            refusal = self.budget.refusal()
            if refusal:
                rec.error = refusal
                return {"error": refusal + "; write your verdict with what you have"}
            handler: Callable[[dict, ToolCallRecord], dict] | None = {
                "search_web": self._search_web,
                "scrape_url": self._scrape_url,
                "get_cluster_posts": self._get_cluster_posts,
                "get_account": self._get_account,
            }.get(name)
            if handler is None or name not in {d["name"] for d in self.declarations()}:
                rec.error = f"unknown tool {name}"  # not counted against the budget
                return {"error": rec.error}
            self.budget.tool_calls += 1
            try:
                out = handler(args, rec)
            except BudgetRefused as exc:
                rec.error = str(exc)
                return {"error": f"{exc}; write your verdict with what you have"}
            except Exception as exc:  # a broken tool must not cost the cluster its verdict
                log.exception("analyst tool %s failed for cluster %s", name, self.ctx.id)
                if isinstance(exc, psycopg.Error) and self._conn is not None:
                    self._conn.rollback()
                rec.error = f"{type(exc).__name__}"
                return {"error": "tool failed; do not retry it"}
            rec.ok = "error" not in out
            if not rec.ok:
                rec.error = str(out["error"])
            return out
        finally:
            rec.latency_ms = int((time.monotonic() - started) * 1000)
            self.records.append(rec)
            log.info(
                "analyst tool call cluster=%s tool=%s ok=%s cached=%s endpoint=%s url=%s "
                "evidence_id=%s latency_ms=%d error=%s",
                self.ctx.id,
                name,
                rec.ok,
                rec.cached,
                rec.endpoint,
                rec.source_url or args.get("url"),
                rec.evidence_id,
                rec.latency_ms,
                rec.error,
            )

    # --- web tools ---

    def _web_evidence(
        self, key: str, rec: ToolCallRecord, fetch: Callable[[], SourceEvidence]
    ) -> EvidenceItem | None:
        """Stored row for `key` -> cached content -> live Oxylabs call (counted in the budget)."""
        stored = self._sink.existing(key)
        if stored is not None:
            rec.cached = True
            return stored
        hit, ev = self._cache.get(key)
        if hit:
            rec.cached = True
            if ev is None:
                return None
        else:
            refusal = self.budget.web_refusal()
            if refusal:
                raise BudgetRefused(refusal)
            self.budget.web_calls += 1
            try:
                ev = fetch()
            except OxylabsError as exc:
                log.warning("analyst web tool failed (%s): %s", key, exc)
                self._cache.put(key, None)
                return None
            self._cache.put(key, ev)
        detail = {**ev.detail, "requested_by": self._requested_by, "why": rec.why}
        return self._sink.save(ev.model_copy(update={"detail": detail}))

    def _search_web(self, args: dict, rec: ToolCallRecord) -> dict:
        query = " ".join(str(args.get("query") or "").split())[:200]
        if not query:
            return {"error": "query is empty"}
        rec.endpoint = "/search"
        key = f"search:agent:{query.lower()}"
        item = self._web_evidence(
            key,
            rec,
            lambda: _agent_search_evidence(key, query, self._web.search(query, MAX_SEARCH_HITS)),
        )
        if item is None:
            return {"error": "search failed; do not retry it"}
        self._remember(item, rec)
        hits = item.detail.get("hits") or []
        self._allow(h.get("url") for h in hits)
        return {
            "evidence_id": item.id,
            "summary": item.summary,
            "results": [
                {k: h.get(k) for k in ("url", "title", "snippet", "published_hint")} for h in hits
            ],
        }

    def _scrape_url(self, args: dict, rec: ToolCallRecord) -> dict:
        raw = str(args.get("url") or "").strip()
        rec.endpoint = "/scrape"
        url = checkable_url(raw)
        if url is None:
            return {"error": "not a checkable URL (Bluesky, media file, local or IP host)"}
        if url.rstrip("/") not in self._allowed_urls and raw.rstrip("/") not in self._allowed_urls:
            return {
                "error": "URL not allowed: only URLs from this cluster's posts or evidence, or "
                "from an earlier search_web result, may be fetched"
            }
        key = f"page:{url}"
        item = self._web_evidence(
            key,
            rec,
            lambda: _page_evidence(key, self._web.scrape_facts(url), *self._sharing(url)),
        )
        if item is None:
            return {"error": "page could not be fetched; do not retry it"}
        self._remember(item, rec)
        d = item.detail
        return {
            "evidence_id": item.id,
            "summary": item.summary,
            **{
                k: d.get(k)
                for k in ("final_url", "http_status", "publisher", "author", "published_at")
            },
            "headline": d.get("headline"),
            "claims": d.get("claims") or [],
        }

    def _remember(self, item: EvidenceItem, rec: ToolCallRecord) -> None:
        rec.evidence_id = item.id
        rec.source_url = item.source_url
        self.returned[item.id] = item
        if item.id not in self.ctx.evidence_ids:
            self.new_evidence[item.id] = item

    def _sharing(self, url: str) -> tuple[int, int]:
        """(accounts in the cluster that posted `url`, accounts in the cluster) when we can tell."""
        if self._conn is None:
            return 0, self.ctx.size or 0
        r = self._conn.execute(
            """
            SELECT count(DISTINCT s.did) FILTER (WHERE %s = ANY(s.embed_urls)) AS shared_by,
                   count(DISTINCT s.did) AS n
            FROM cluster_posts cp JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
            WHERE cp.cluster_id = %s
            """,
            (url, self.ctx.id),
        ).fetchone()
        return int(r["shared_by"] or 0), int(r["n"] or self.ctx.size or 0)

    # --- read-only DB lookups ---

    def _get_cluster_posts(self, args: dict, rec: ToolCallRecord) -> dict:
        try:
            limit = max(1, min(int(args.get("limit") or 20), MAX_POSTS))
        except (TypeError, ValueError):
            limit = 20
        rows = self._conn.execute(
            """
            SELECT s.uri, s.did, a.handle, s.created_at, s.text,
                   coalesce(s.embed_urls, '{}') AS urls
            FROM cluster_posts cp
            JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
            LEFT JOIN accounts a ON a.did = s.did
            WHERE cp.cluster_id = %s AND s.deleted_at IS NULL
            ORDER BY s.seen_at LIMIT %s
            """,
            (self.ctx.id, limit),
        ).fetchall()
        posts = [
            {
                "author": r["handle"] or r["did"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "text": " ".join((r["text"] or "").split())[:300],
                "links": list(r["urls"])[:3],
            }
            for r in rows
        ]
        self._allow(u for p in posts for u in p["links"])
        return {"posts": posts, "note": "context only, not citable"}

    def _get_account(self, args: dict, rec: ToolCallRecord) -> dict:
        who = str(args.get("account") or "").strip().removeprefix("@")
        r = self._conn.execute(
            """
            SELECT a.did, a.handle, a.display_name,
                   coalesce(a.did_created_at, a.created_at) AS since,
                   a.followers, a.follows, a.posts_count, a.has_avatar, a.description_len,
                   a.pds_host, a.handle_changes, a.status
            FROM cluster_members m JOIN accounts a ON a.did = m.did
            WHERE m.cluster_id = %s AND (a.did = %s OR lower(a.handle) = lower(%s))
            LIMIT 1
            """,
            (self.ctx.id, who, who),
        ).fetchone()
        if r is None:
            return {"error": "not a member of this cluster (or no profile stored)"}
        out = {k: v for k, v in r.items() if v is not None}
        if "since" in out:
            out["since"] = out["since"].isoformat()
        out["note"] = "context only, not citable"
        return out


def _agent_search_evidence(key: str, query: str, hits) -> SourceEvidence:
    """sources' web_search evidence, with a summary that names Gemini's own query."""
    ev = _search_evidence(key, "agent", query.strip('"'), query, hits)
    d = ev.detail
    if not hits:
        summary = f"Web search for {query} (asked by Gemini) returned no results."
    else:
        summary = (
            f"Web search for {query} (asked by Gemini): {len(hits)} results on "
            f"{d['distinct_domains']} domains"
        )
        if d.get("earliest_dated"):
            e = d["earliest_dated"]
            summary += f"; earliest dated: {domain(e['url'])} ({e['published_hint']})"
        summary += "."
    return ev.model_copy(update={"summary": summary})


def _urls_in(detail: dict) -> list[str]:
    out = []
    for k in ("url", "final_url", "post_urls"):
        v = detail.get(k)
        out.extend(v if isinstance(v, list) else [v])
    for h in detail.get("hits") or []:
        out.append(h.get("url"))
    out.extend(detail.get("embed_urls") or [])
    return [u for u in out if isinstance(u, str)]
