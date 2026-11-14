"""One lookup: collect live, receipted data about a link, step by step (lookup/README.md).

The job object is shared with the API: every step writes into `self.result` under a lock, so a
poll mid-run returns everything found so far. Failures inside a step are recorded on that step
and the run continues: a missing check is not evidence either way.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from lookup import textsim
from lookup.bsky import BlueskyClient, BlueskyTimeUp, parse_time, post_url
from lookup.models import (
    Account,
    Card,
    Edge,
    LookupInput,
    LookupResult,
    MediaFinding,
    Metrics,
    Page,
    Post,
    Receipt,
    Spread,
    Step,
)
from lookup.urls import X_STATUS_RE, Target, classify, domain, snowflake_time, x_post_url
from lookup.xpage import PostPage, XItem, parse_post_page, parse_profile

log = logging.getLogger(__name__)

STEPS = [
    ("input", "you", "Read the input"),
    ("post", "oxylabs", "Read the post"),
    ("author", "oxylabs", "Read the author's profile and recent posts"),
    ("linked", "oxylabs", "Read the linked page"),
    ("copies_web", "oxylabs", "Search the web for the same text"),
    ("copies_x", "oxylabs", "Search X for the same text"),
    ("copies_bsky", "bluesky", "Find the same text on Bluesky"),
    ("coverage", "oxylabs", "Find coverage of the story"),
    ("media", "nemotron", "Read the images with Nemotron VL"),
    ("verify", "oxylabs", "Read candidate copies on their own pages"),
    ("monitor", "detector", "Check the clusters we monitor"),
    ("assess", "gemini", "Gemini reads the receipts"),
]
VERBATIM = 0.8
COPY_MIN = 0.5
ASSESS_RESERVE_S = 45.0  # the last seconds of the budget are Gemini's; a big reading can pass 30 s
X_MIN_SIM = 0.35  # an X search hit sharing fewer words with the text is not about it
OCR_MIN_WORDS = 8  # image text shorter than this is a label, not a message worth searching
_REL_AGO = re.compile(r"^\s*(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)
_REL_UNIT = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2629800,
    "year": 31557600,
}
SHORTENERS = {"t.co", "bit.ly", "buff.ly", "ow.ly", "tinyurl.com", "dlvr.it", "trib.al", "lnkd.in"}
MEDIA_HOSTS = {"pbs.twimg.com", "cdn.bsky.app", "video.bsky.app"}
UA = "Mozilla/5.0 (compatible; shill-or-chill-lookup/1.0)"
_BSKY_POST = re.compile(r"https?://bsky\.app/profile/([^/\s?#]+)/post/([a-z0-9]+)")


class BudgetExceeded(RuntimeError):
    """The lookup's time budget is used up: no new provider call starts."""


def _now() -> datetime:
    return datetime.now(UTC)


def snippet_time(snippet: str, at: datetime) -> tuple[datetime | None, float | None]:
    """A search engine's date hint and how uncertain it is (seconds after `time`).

    '23 hours ago' -> (at - 23 h, 3600): the engine rounds to the unit. 'Nov 13, 2026 — ...'
    -> (that midnight UTC, 86400): any time that day."""
    if m := _REL_AGO.match(snippet or ""):
        unit = _REL_UNIT[m.group(2).lower()]
        return at - timedelta(seconds=(int(m.group(1)) + 1) * unit), 2 * unit
    from sources.oxylabs import leading_date

    d = leading_date(snippet or "")
    return (datetime(d.year, d.month, d.day, tzinfo=UTC), 86400.0) if d else (None, None)


def _page_time(published_at: str | None) -> tuple[datetime | None, float | None]:
    """The publisher's date as extracted, and its uncertainty (seconds after `time`)."""
    if not published_at:
        return None, None
    raw = published_at.strip()
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if len(raw) <= 10:  # a date only: any time that day
        return t.replace(tzinfo=UTC), 86400.0
    if t.tzinfo is None:  # a clock time in an unknown zone: within about 14 h of UTC
        return t.replace(tzinfo=UTC) - timedelta(hours=14), 28 * 3600.0
    return t, 0.0


def _x_title(title: str) -> tuple[str | None, str | None]:
    """'Name on X: "text..."' -> (name, text); 'Name on X' -> (name, None)."""
    m = re.match(r'^(.*?) on X(?:: "(.*?)(?:"(?: / X)?)?)?\s*$', title or "", re.S)
    if not m:
        return None, None
    text = (m.group(2) or "").strip().removesuffix("...").removesuffix("…").strip() or None
    return m.group(1).strip() or None, text


def _x_img(url: str) -> str:
    """pbs.twimg.com image in a format Nemotron reads (jpg, large)."""
    url = re.sub(r"format=webp", "format=jpg", url)
    return url if "name=" in url else url + ("&" if "?" in url else "?") + "name=large"


@dataclass
class Deps:
    oxylabs: Any  # sources.oxylabs.OxylabsClient (search, scrape_facts, scrape_markdown)
    bsky: BlueskyClient | None = None
    store: Callable[[], Any] | None = None  # context manager factory yielding api.store.Store
    nemotron: Any = None  # media.nemotron.NemotronClient
    assessor: Any = None  # lookup.assess.Assessor
    http: httpx.Client | None = None  # media downloads
    verify_max: int = 8
    profiles_max: int = 4
    replies_max: int = 8
    media_max: int = 2
    time_budget_s: float = 150.0


class Lookup:
    def __init__(self, job_id: str, inp: LookupInput, deps: Deps):
        self.deps = deps
        self.lock = threading.RLock()
        self.target: Target | None = classify(inp.url) if inp.url else None
        kind = self.target.kind if self.target else "text"
        self.result = LookupResult(
            id=job_id,
            input=inp,
            kind=kind,
            started_at=_now(),
            steps=[Step(id=s, tool=t, label=lbl) for s, t, lbl in STEPS],
        )
        self.deadline = time.monotonic() + deps.time_budget_s
        self._subject_text: str | None = (inp.text or "").strip() or None
        self._subject_time: datetime | None = None
        self._card_url: str | None = None
        self._card_title: str | None = None
        self._media_urls: list[str] = []
        self._bsky_handles: set[str] = set()
        self._final: dict | None = None

    # ---------------------------------------------------------------- bookkeeping
    def snapshot(self) -> dict:
        """Live view while running; once finished, the frozen final result (late workers that
        outlived the time budget can't change what clients saw as done)."""
        with self.lock:
            if self._final is not None:
                return self._final
            return self.result.model_dump(mode="json", by_alias=True)

    def _step(self, sid: str) -> Step:
        return next(s for s in self.result.steps if s.id == sid)

    @contextmanager
    def step(self, sid: str) -> Iterator[Step]:
        with self.lock:
            st = self._step(sid)
            st.state, st.started_at = "running", _now()
        try:
            yield st
        except (BudgetExceeded, BlueskyTimeUp):
            with self.lock:
                st.state = "skipped" if not st.found else "done"
                st.detail = (st.detail + " · " if st.detail else "") + "time budget used up"
                st.ended_at = _now()
            return
        except Exception as exc:  # recorded, never fatal for the run
            log.warning("lookup %s step %s failed: %s", self.result.id, sid, exc, exc_info=True)
            with self.lock:
                st.state = "failed"
                st.detail = st.detail or f"{type(exc).__name__}: {str(exc)[:160]}"
                st.ended_at = _now()
            return
        with self.lock:
            if st.state == "running":
                st.state = "done"
            st.ended_at = _now()

    def skip(self, sid: str, why: str) -> None:
        with self.lock:
            st = self._step(sid)
            if st.state == "pending":
                st.state, st.detail = "skipped", why

    def receipt(
        self,
        tool: str,
        endpoint: str,
        summary: str,
        *,
        source_url: str | None = None,
        query: str | None = None,
        call: dict | None = None,
        ok: bool = True,
        latency_ms: int | None = None,
    ) -> int:
        call = call or {}
        with self.lock:
            rid = len(self.result.receipts) + 1
            self.result.receipts.append(
                Receipt(
                    id=rid,
                    tool=tool,
                    endpoint=endpoint,
                    summary=summary[:400],
                    source_url=source_url,
                    query=query,
                    request_id=call.get("request_id"),
                    latency_ms=latency_ms if latency_ms is not None else call.get("latency_ms"),
                    ok=ok,
                    at=_now(),
                )
            )
            return rid

    def add_post(self, p: Post) -> Post:
        """Insert or merge by id. The submitted post keeps its role whatever step saw it first;
        otherwise the better-read copy (its own page/API over a search snippet) wins."""
        with self.lock:
            for k, old in enumerate(self.result.posts):
                if old.id != p.id:
                    continue
                pinned = next((r for r in ("submitted", "origin") if r in (old.role, p.role)), None)
                if old.verified != p.verified:  # a post's own page/API beats a search snippet
                    base = p if p.verified else old
                else:
                    better = p.role == "submitted" or old.role in ("mention", "author_recent")
                    base = p if better else old
                if base is p and old.similarity is not None and p.similarity is None:
                    base = p.model_copy(update={"similarity": old.similarity})
                merged = base.model_copy(update={"role": pinned or base.role})
                if merged.role == "submitted":
                    merged.similarity = 1.0
                self.result.posts[k] = merged
                return merged
            if p.role == "submitted":
                p = p.model_copy(update={"similarity": 1.0})
            self.result.posts.append(p)
            return p

    def add_page(self, pg: Page) -> Page:
        with self.lock:
            for old in self.result.pages:
                if old.url == pg.url or (pg.final_url and old.final_url == pg.final_url):
                    if pg.role == "linked":
                        old.role = "linked"
                    # a "linked" page always comes from /scrape (_linked): it was read
                    if (
                        pg.time_source == "page"
                        or pg.role == "linked"
                        or (pg.receipt_id and not old.publisher and pg.publisher)
                    ):
                        # the page was read: its own facts beat a search snippet's
                        for f in (
                            "final_url",
                            "publisher",
                            "author",
                            "published_at",
                            "headline",
                            "time",
                            "time_source",
                            "time_uncertainty_s",
                            "receipt_id",
                        ):
                            val = getattr(pg, f)
                            if val is not None:
                                setattr(old, f, val)
                        if pg.claims:
                            old.claims = pg.claims
                    elif pg.role == "linked" and pg.receipt_id:
                        old.publisher = old.publisher or pg.publisher
                        old.headline = pg.headline or old.headline
                        old.receipt_id = pg.receipt_id
                    return old
            pg.id = f"page:{len(self.result.pages) + 1}"
            self.result.pages.append(pg)
            return pg

    def add_edge(
        self,
        frm: str,
        to: str,
        rel: str,
        *,
        sim: float | None = None,
        tool: str = "oxylabs",
        note: str | None = None,
    ) -> None:
        if frm == to:
            return
        with self.lock:
            if any(e.from_ == frm and e.to == to for e in self.result.edges):
                return
            self.result.edges.append(
                Edge(**{"from": frm, "to": to, "rel": rel, "sim": sim, "tool": tool, "note": note})
            )

    def add_account(self, a: Account) -> None:
        with self.lock:
            for k, old in enumerate(self.result.accounts):
                if old.platform == a.platform and old.handle.lower() == a.handle.lower():
                    self.result.accounts[k] = a
                    return
            self.result.accounts.append(a)

    def note(self, text: str) -> None:
        with self.lock:
            if text not in self.result.notes:
                self.result.notes.append(text)

    def _left(self) -> float:
        return max(1.0, self.deadline - time.monotonic())

    def _provider_deadline(self) -> float:
        """Absolute (monotonic) end for a provider call made now: Gemini's reserve stays free."""
        return self.deadline - ASSESS_RESERVE_S

    def _spend(self, need_s: float = 3.0, reserve_s: float = ASSESS_RESERVE_S) -> None:
        """Gate before every paid or slow provider call; keeps time back for Gemini's reading."""
        if time.monotonic() + need_s + reserve_s > self.deadline:
            raise BudgetExceeded("time budget used up")

    # ---------------------------------------------------------------- run
    def run(self) -> None:
        try:
            self._run()
            with self.lock:
                self.result.state = "done"
        except Exception as exc:
            log.exception("lookup %s failed", self.result.id)
            with self.lock:
                self.result.state = "failed"
                self.result.error = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            with self.lock:
                self.result.finished_at = _now()
                for st in self.result.steps:
                    if st.state in ("pending", "running"):
                        st.state = "skipped" if st.state == "pending" else "failed"
                        st.detail = st.detail or "time budget used up"
                self._final = self.result.model_dump(mode="json", by_alias=True)

    def _run(self) -> None:
        t, kind = self.target, self.result.kind
        with self.step("input") as st:
            st.detail = {
                "x_post": "X post",
                "x_profile": "X profile",
                "bsky_post": "Bluesky post",
                "bsky_profile": "Bluesky profile",
                "web": "Web page",
                "text": "Text",
            }[kind]
            if t:
                st.detail += f" · {t.url}"
        pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="lookup")
        try:
            self._phases(pool, t, kind)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        self._finish_graph()
        self._assess()

    def _phases(self, pool: ThreadPoolExecutor, t: Target | None, kind: str) -> None:
        first = []
        if kind == "x_post":
            first.append(pool.submit(self._x_post))
            if t and t.handle:
                first.append(pool.submit(self._x_author, t.handle, True))
            else:
                self.skip("author", "no handle in the link")
        elif kind == "x_profile":
            self.skip("post", "a profile, not a post")
            first.append(pool.submit(self._x_author, t.handle, True))
        elif kind == "bsky_post":
            first.append(pool.submit(self._bsky_post))
            first.append(pool.submit(self._bsky_author, t.handle))
        elif kind == "bsky_profile":
            self.skip("post", "a profile, not a post")
            first.append(pool.submit(self._bsky_author, t.handle))
        elif kind == "web":
            self.skip("post", "a web page, not a social post")
            self.skip("author", "a web page, not a social post")
            first.append(pool.submit(self._linked, t.url, True))
        else:
            self.skip("post", "text input")
            self.skip("author", "text input")
            self.skip("linked", "text input")
        wait(first, timeout=self._left())

        if kind in ("x_profile", "bsky_profile"):
            for sid in ("linked", "copies_web", "copies_x", "copies_bsky", "coverage", "media"):
                self.skip(sid, "profile lookup")
            second = []
        else:
            second = []
            if not textsim.phrase(self._subject_text) and self._media_urls:
                # image-only post: read the image first, then search for its text
                self._media(ocr_as_text=True)
            if kind != "web":
                if self._card_url:
                    second.append(pool.submit(self._linked, self._card_url, False))
                else:
                    self.skip("linked", "the post links no page")
            if self._step("media").state == "pending":
                second.append(pool.submit(self._media))
            second += [
                pool.submit(self._copies_web),
                pool.submit(self._copies_x),
                pool.submit(self._copies_bsky),
                pool.submit(self._coverage),
            ]
        wait(second, timeout=self._left())
        if kind not in ("x_profile", "bsky_profile"):
            self._verify()  # after every search, so candidates from all of them are read
        else:
            self.skip("verify", "profile lookup")
        # last: the link, text and accounts found by every step above are known now
        self._monitor()

    # ---------------------------------------------------------------- X
    def _scrape_x(self, url: str) -> tuple[str, dict]:
        self._spend()
        md, _meta = self.deps.oxylabs.scrape_markdown(url, deadline=self._provider_deadline())
        return md, dict(self.deps.oxylabs.last_call)

    def _x_post_from_item(
        self,
        it: XItem,
        role: str,
        *,
        sim: float | None,
        rid: int | None,
        verified: bool,
        as_of: datetime | None,
    ) -> Post:
        return Post(
            id=f"x:{it.status_id}",
            platform="x",
            url=x_post_url(it.handle, it.status_id),
            author_handle=it.handle,
            author_name=it.name,
            text=it.text,
            created_at=it.created_at,
            time_source="snowflake",
            role=role,
            similarity=sim,
            metrics=Metrics(
                views=it.views,
                replies=it.replies,
                reposts=it.reposts,
                likes=it.likes,
                bookmarks=it.bookmarks,
            ),
            metrics_as_of=as_of,
            card=Card(title=it.card_title, domain=it.card_domain, url=it.card_url)
            if it.card_url or it.card_title
            else None,
            media_urls=[_x_img(u) for u in it.media_urls],
            receipt_id=rid,
            verified=verified,
        )

    def _x_post(self) -> None:
        t = self.target
        with self.step("post") as st:
            md, call = self._scrape_x(t.url)
            page: PostPage = parse_post_page(md, t.post_id)
            m = page.main
            if m is None:
                rid = self.receipt(
                    "oxylabs",
                    "/scrape",
                    "X post page read, but the post was not "
                    "on it (deleted, protected or age-restricted)",
                    source_url=t.url,
                    call=call,
                    ok=False,
                )
                st.state, st.detail = "failed", "post not visible logged out"
                self._subject_time = snowflake_time(t.post_id)
                return
            counts = ", ".join(
                f"{v:,} {k}"
                for k, v in (
                    ("views", m.views),
                    ("replies", m.replies),
                    ("reposts", m.reposts),
                    ("likes", m.likes),
                    ("bookmarks", m.bookmarks),
                )
                if v is not None
            )
            rid = self.receipt(
                "oxylabs",
                "/scrape",
                f"X post by @{m.handle} read: {counts}",
                source_url=t.url,
                call=call,
            )
            now = _now()
            post = self._x_post_from_item(
                m, "submitted", sim=1.0, rid=rid, verified=True, as_of=now
            )
            if m.quoted_text:
                post.text = (m.text + "\n\n> " + m.quoted_text).strip()
            self.add_post(post)
            with self.lock:
                self.result.subject_id = post.id
            self._subject_text = m.text or m.quoted_text
            self._subject_time = m.created_at
            self._card_url, self._card_title = m.card_url, m.card_title
            self._media_urls = list(post.media_urls)
            if m.card_image and not self._media_urls:
                self._media_urls.append(_x_img(m.card_image))
            self.receipt(
                "x-id",
                "snowflake",
                "Exact creation times of X posts are read from "
                "their ids (snowflakes), not from the page",
                source_url=t.url,
            )
            found = [post.id]
            if m.quoted_id:
                q = Post(
                    id=f"x:{m.quoted_id}",
                    platform="x",
                    url=x_post_url(m.quoted_handle, m.quoted_id),
                    author_handle=m.quoted_handle,
                    text=m.quoted_text or "",
                    created_at=snowflake_time(m.quoted_id),
                    time_source="snowflake",
                    role="mention",
                    receipt_id=rid,
                )
                self.add_post(q)
                self.add_edge(q.id, post.id, "quote", note="quoted by the submitted post")
                found.append(q.id)
            prev = None
            for a in page.ancestors:
                ap = self.add_post(
                    self._x_post_from_item(
                        a, "mention", sim=None, rid=rid, verified=False, as_of=now
                    )
                )
                if prev:
                    self.add_edge(prev, ap.id, "reply")
                prev = ap.id
                found.append(ap.id)
            if prev:
                self.add_edge(prev, post.id, "reply", note="the submitted post replies to it")
            for r in page.replies[: self.deps.replies_max]:
                rp = self.add_post(
                    self._x_post_from_item(r, "reply", sim=None, rid=rid, verified=False, as_of=now)
                )
                self.add_edge(post.id, rp.id, "reply")
                found.append(rp.id)
            st.found = found
            st.detail = counts or "post read"

    def _x_author(self, handle: str, subject: bool) -> Account | None:
        ctx = self.step("author") if subject else _null_step()
        with ctx as st:
            url = f"https://x.com/{handle}"
            md, call = self._scrape_x(url)
            p = parse_profile(md, handle)
            if p.followers is None and p.joined is None:
                raise RuntimeError("profile not readable logged out")
            per_day = None
            ids = sorted(int(it.status_id) for it in p.recent if it.status_id)
            if len(ids) >= 3:
                span = (snowflake_time(ids[-1]) - snowflake_time(ids[0])).total_seconds()
                per_day = round((len(ids) - 1) / max(span / 86400, 1 / 24), 1)
            rid = self.receipt(
                "oxylabs",
                "/scrape",
                f"X profile @{handle}: joined {p.joined or '?'}, "
                f"{_fmt(p.followers)} followers, {_fmt(p.posts_count)} posts",
                source_url=url,
                call=call,
            )
            ref = self._subject_time or _now()
            age = None
            if p.joined:
                y, mth = (int(x) for x in (p.joined.split("-") + ["1"])[:2])
                age = max(0, (ref - datetime(y, mth, 1, tzinfo=UTC)).days)
            acct = Account(
                platform="x",
                handle=handle,
                name=p.name,
                url=url,
                avatar_url=p.avatar_url,
                bio=p.bio,
                location=p.location,
                joined=p.joined,
                age_days=age,
                followers=p.followers,
                following=p.following,
                posts_count=p.posts_count,
                recent_posts_per_day=per_day,
                receipt_id=rid,
            )
            self.add_account(acct)
            if subject:
                found = []
                now = _now()
                for it in p.recent[:10]:
                    rp = self.add_post(
                        self._x_post_from_item(
                            it, "author_recent", sim=None, rid=rid, verified=False, as_of=now
                        )
                    )
                    found.append(rp.id)
                st.found = found
                st.detail = (
                    f"joined {p.joined or '?'} · {_fmt(p.followers)} followers · "
                    f"{len(p.recent)} recent posts"
                    + (f" · ~{per_day}/day" if per_day is not None else "")
                )
            return acct

    # ---------------------------------------------------------------- Bluesky
    def _bsky_post_obj(self, pv: dict, role: str, sim: float | None, rid: int | None) -> Post:
        author = pv.get("author") or {}
        rec = pv.get("record") or {}
        emb = pv.get("embed") or {}
        card = None
        ext = emb.get("external") or (emb.get("media") or {}).get("external")
        if ext:
            card = Card(title=ext.get("title"), domain=domain(ext.get("uri")), url=ext.get("uri"))
        imgs = emb.get("images") or (emb.get("media") or {}).get("images") or []
        return Post(
            id=pv["uri"],
            platform="bluesky",
            url=post_url(pv["uri"], author.get("handle")),
            author_handle=author.get("handle"),
            author_name=author.get("displayName"),
            text=rec.get("text") or "",
            created_at=parse_time(rec.get("createdAt")),
            time_source="api",
            role=role,
            similarity=sim,
            metrics=Metrics(
                likes=pv.get("likeCount"),
                reposts=pv.get("repostCount"),
                replies=pv.get("replyCount"),
                quotes=pv.get("quoteCount"),
            ),
            metrics_as_of=_now(),
            card=card,
            media_urls=[i["fullsize"] for i in imgs if i.get("fullsize")],
            receipt_id=rid,
            verified=True,
        )

    def _bsky_post(self) -> None:
        t = self.target
        with self.step("post") as st:
            if not self.deps.bsky:
                raise RuntimeError("Bluesky client not configured")
            self._spend()
            th = self.deps.bsky.thread(
                t.handle, t.post_id, depth=1, deadline=self._provider_deadline()
            )
            pv = th.get("post")
            if not pv:
                raise RuntimeError("post not found (deleted or blocked)")
            rid = self.receipt(
                "bluesky",
                "getPostThread",
                f"Bluesky post read: {pv.get('likeCount', 0)} likes, "
                f"{pv.get('repostCount', 0)} reposts, "
                f"{pv.get('replyCount', 0)} replies",
                source_url=t.url,
                call=self.deps.bsky.last_call,
            )
            post = self.add_post(self._bsky_post_obj(pv, "submitted", 1.0, rid))
            with self.lock:
                self.result.subject_id = post.id
            self._subject_text, self._subject_time = post.text, post.created_at
            self._card_url = post.card.url if post.card else None
            self._card_title = post.card.title if post.card else None
            self._media_urls = list(post.media_urls)
            if post.author_handle:
                self._bsky_handles.add(post.author_handle)
            found = [post.id]
            parent = th.get("parent") or {}
            if parent.get("post"):
                pp = self.add_post(self._bsky_post_obj(parent["post"], "mention", None, rid))
                self.add_edge(pp.id, post.id, "reply", tool="bluesky")
                found.append(pp.id)
            for r in (th.get("replies") or [])[: self.deps.replies_max]:
                if r.get("post"):
                    rp = self.add_post(self._bsky_post_obj(r["post"], "reply", None, rid))
                    self.add_edge(post.id, rp.id, "reply", tool="bluesky")
                    found.append(rp.id)
            st.found, st.detail = found, f"{len(found) - 1} replies/parents read"

    def _bsky_author(self, actor: str) -> None:
        with self.step("author") as st:
            if not self.deps.bsky:
                raise RuntimeError("Bluesky client not configured")
            self._spend()
            pr = self.deps.bsky.profile(actor, deadline=self._provider_deadline())
            created = parse_time(pr.get("createdAt"))
            ref = self._subject_time or _now()
            rid = self.receipt(
                "bluesky",
                "getProfile",
                f"Bluesky profile @{pr.get('handle')}: created "
                f"{created.date() if created else '?'}, "
                f"{pr.get('followersCount')} followers, "
                f"{pr.get('postsCount')} posts",
                source_url=f"https://bsky.app/profile/{pr.get('handle')}",
                call=self.deps.bsky.last_call,
            )
            feed = []
            try:
                self._spend()
                feed = self.deps.bsky.author_feed(
                    actor, limit=20, deadline=self._provider_deadline()
                )
            except (httpx.HTTPError, BudgetExceeded, BlueskyTimeUp):
                pass
            feed = [pv for pv in feed if (pv.get("author") or {}).get("did") == pr.get("did")]
            times = sorted(
                t for t in (parse_time((p.get("record") or {}).get("createdAt")) for p in feed) if t
            )
            per_day = None
            if len(times) >= 3:
                span = (times[-1] - times[0]).total_seconds()
                per_day = round((len(times) - 1) / max(span / 86400, 1 / 24), 1)
            self.add_account(
                Account(
                    platform="bluesky",
                    handle=pr.get("handle") or actor,
                    name=pr.get("displayName"),
                    url=f"https://bsky.app/profile/{pr.get('handle') or actor}",
                    avatar_url=pr.get("avatar"),
                    bio=pr.get("description"),
                    joined=created.date().isoformat() if created else None,
                    age_days=(ref - created).days if created else None,
                    followers=pr.get("followersCount"),
                    following=pr.get("followsCount"),
                    posts_count=pr.get("postsCount"),
                    recent_posts_per_day=per_day,
                    receipt_id=rid,
                )
            )
            self._bsky_handles.add(pr.get("handle") or actor)
            found = []
            me = (pr.get("did"), pr.get("handle"))
            own = [
                pv
                for pv in feed
                if (pv.get("author") or {}).get("did") in me
                or (pv.get("author") or {}).get("handle") in me
            ]
            for pv in own[:10]:  # the feed also lists reposts of other people's posts
                rp = self.add_post(self._bsky_post_obj(pv, "author_recent", None, rid))
                found.append(rp.id)
            st.found = found
            st.detail = (
                f"created {created.date() if created else '?'} · "
                f"{pr.get('followersCount')} followers"
            )

    # ---------------------------------------------------------------- pages
    def _resolve(self, url: str) -> str:
        """t.co and other shorteners -> the page they point to (Oxylabs rejects some of them).

        Reads only the shortener's own redirect (Location header, no redirect is followed), so
        the server never requests the target itself: no SSRF through a short link."""
        if domain(url) not in SHORTENERS or not url.startswith("https://"):
            return url
        http = self.deps.http or httpx.Client(timeout=10)
        try:
            res = http.head(url, follow_redirects=False, headers={"user-agent": UA})
            loc = res.headers.get("location", "")
            return loc if res.is_redirect and loc.startswith(("https://", "http://")) else url
        except httpx.HTTPError:
            return url

    def _linked(self, url: str, is_input: bool) -> None:
        with self.step("linked") as st:
            short = url
            url = self._resolve(url)
            self._spend()
            facts = self.deps.oxylabs.scrape_facts(url, deadline=self._provider_deadline())
            call = dict(self.deps.oxylabs.last_call)
            pub = facts.published_at
            ptime, punc = _page_time(pub)
            rid = self.receipt(
                "oxylabs",
                "/scrape",
                f"Linked page read: {facts.publisher or domain(facts.final_url)}"
                f" — {facts.headline or 'no headline'}" + (f" (published {pub})" if pub else ""),
                source_url=facts.final_url or url,
                call=call,
            )
            pg = self.add_page(
                Page(
                    id="",
                    url=short if short != url else url,
                    final_url=facts.final_url or url,
                    role="linked",
                    publisher=facts.publisher,
                    author=facts.author,
                    published_at=pub,
                    headline=facts.headline,
                    claims=facts.claims,
                    time=ptime,
                    time_source="page" if ptime else None,
                    time_uncertainty_s=punc,
                    receipt_id=rid,
                )
            )
            if is_input:
                self._subject_text = self._subject_text or facts.headline
                self._card_title = facts.headline
                self._subject_time = ptime
                with self.lock:
                    self.result.subject_id = pg.id
            elif self.result.subject_id:
                self.add_edge(pg.id, self.result.subject_id, "link", note="the post links it")
            st.found = [pg.id]
            st.detail = f"{facts.publisher or domain(facts.final_url)}: {facts.headline or '-'}"

    # ---------------------------------------------------------------- searches
    def _search(self, query: str, n: int = 10):
        self._spend()
        hits = self.deps.oxylabs.search(query, max_results=n, deadline=self._provider_deadline())
        return hits, dict(self.deps.oxylabs.last_call)

    def _phrase(self) -> str | None:
        return textsim.phrase(self._subject_text) or textsim.phrase(self._card_title)

    def _classify_hit(
        self, hit, rid: int, at: datetime, *, reference: str, min_sim: float = 0.0
    ) -> str | None:
        """Turn one search hit into a post or page; returns the new node id.

        X hits that share few words with the reference are dropped (search engines match loose
        keywords); pages below `min_sim` too."""
        snippet = textsim.strip_snippet(hit.snippets[0]) if hit.snippets else ""
        if m := X_STATUS_RE.match(hit.url):
            handle, sid = m.group(1), m.group(2)
            pid = f"x:{sid}"
            if pid == self.result.subject_id:
                return None
            name, own = _x_title(hit.title)
            sim_own = textsim.similarity(own, reference, truncated=True) if own else None
            sim_snip = textsim.similarity(snippet, reference, truncated=True) if snippet else 0.0
            sim = sim_own if sim_own is not None else sim_snip
            if max(sim, sim_snip) < max(min_sim, X_MIN_SIM):
                return None
            # a search hit is a candidate only; its own page decides copy/quote/reply (verify)
            post = self.add_post(
                Post(
                    id=pid,
                    platform="x",
                    url=x_post_url(handle, sid),
                    author_handle=handle,
                    author_name=name,
                    text=own or snippet,
                    created_at=snowflake_time(sid),
                    time_source="snowflake",
                    role="mention",
                    similarity=round(sim, 2),
                    receipt_id=rid,
                )
            )
            return post.id
        if _BSKY_POST.match(hit.url):
            return None  # Bluesky copies come from its own search (exact times, API data)
        if re.match(r"https?://(?:www\.)?(?:x|twitter)\.com/", hit.url):
            return None  # profile or search pages, not posts
        sim = textsim.similarity(snippet, reference, truncated=True) if snippet else 0.0
        if sim < min_sim:
            return None
        t, unc = snippet_time(hit.snippets[0] if hit.snippets else "", at)
        pg = self.add_page(
            Page(
                id="",
                url=hit.url,
                role="reference" if sim >= COPY_MIN else "coverage",
                headline=hit.title or None,
                snippet=snippet[:300] or None,
                published_hint=hit.published_hint.isoformat() if hit.published_hint else None,
                time=t,
                time_source="search_hint" if t else None,
                time_uncertainty_s=unc,
                receipt_id=rid,
            )
        )
        return pg.id

    def _copies_web(self) -> None:
        phrase = self._phrase()
        if not phrase:
            self.skip("copies_web", "text too short for an exact-phrase search")
            return
        with self.step("copies_web") as st:
            q = f'"{phrase}"'
            hits, call = self._search(q)
            at = _now()
            rid = self.receipt(
                "oxylabs",
                "/search",
                f"Web search for the exact text: {len(hits)} results",
                query=q,
                call=call,
            )
            ref = self._subject_text or phrase
            st.found = [i for h in hits if (i := self._classify_hit(h, rid, at, reference=ref))]
            st.detail = f"{len(st.found)} pages or posts carry the text"

    def _copies_x(self) -> None:
        phrase = self._phrase()
        if not phrase:
            self.skip("copies_x", "text too short for an exact-phrase search")
            return
        with self.step("copies_x") as st:
            q = f'site:x.com "{phrase}"'
            hits, call = self._search(q)
            at = _now()
            rid = self.receipt(
                "oxylabs",
                "/search",
                f"X search for the exact text: {len(hits)} results",
                query=q,
                call=call,
            )
            ref = self._subject_text or phrase
            found = [i for h in hits if (i := self._classify_hit(h, rid, at, reference=ref))]
            st.found = found
            st.detail = f"{len(found)} candidate posts on X (read on their own pages next)"

    def _verify(self) -> None:
        ref = self._subject_text or self._phrase()
        with self.lock:
            cands = [
                p
                for p in self.result.posts
                if p.platform == "x"
                and not p.verified
                and p.role == "mention"
                and (p.similarity or 0) >= X_MIN_SIM
                and p.id != self.result.subject_id
            ]
        pages = self._older_page_candidates()
        if not ref or not (cands or pages):
            self.skip("verify", "no candidate copies to read")
            return
        with self.step("verify") as st:
            cands.sort(key=lambda p: -(p.similarity or 0))
            cands = cands[: self.deps.verify_max]
            jobs = [lambda p=p: self._safe_verify(p, ref) for p in cands]
            jobs += [lambda pg=pg: self._safe_verify_page(pg) for pg in pages]
            self._run_bounded(jobs, workers=6)
            # who are the copiers? read a few profiles (account age, followers)
            with self.lock:
                copiers = list(
                    dict.fromkeys(
                        p.author_handle
                        for p in self.result.posts
                        if p.platform == "x"
                        and p.verified
                        and p.role in ("copy", "quote")
                        and p.author_handle
                    )
                )[: self.deps.profiles_max]
            self._run_bounded([lambda h=h: self._safe_x_author(h) for h in copiers], workers=4)
            ids = {p.id for p in cands}
            with self.lock:
                kinds = [p.role for p in self.result.posts if p.id in ids]
                present = {p.id for p in self.result.posts}
                unread = sum(
                    1
                    for p in self.result.posts
                    if p.platform == "x" and not p.verified and p.role == "mention"
                )
            st.found = [i for i in ids if i in present]
            st.detail = (
                f"{len(cands)} read: {kinds.count('copy')} copies · {kinds.count('quote')} quotes"
                f" · {kinds.count('reply')} replies"
                + (f" · {unread} more candidates not read" if unread else "")
            )

    def _run_bounded(self, jobs: list, workers: int) -> None:
        """Run jobs in parallel until the provider deadline; never wait past it."""
        if not jobs:
            return
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="lookup-v")
        try:
            futures = [pool.submit(j) for j in jobs]
            wait(futures, timeout=max(0.0, self._provider_deadline() - time.monotonic()))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _older_page_candidates(self, n: int = 2) -> list[Page]:
        """Search-hit pages carrying the text whose date hint is before every post: read them,
        because a snippet's date is not the page's own (they can't be the origin unread)."""
        with self.lock:
            first = min(
                (
                    p.created_at
                    for p in self.result.posts
                    if p.created_at and p.verified and p.role in ("submitted", "copy", "bluesky")
                ),
                default=self._subject_time,
            )
            if first is None:
                return []
            pages = [
                pg
                for pg in self.result.pages
                if pg.role == "reference"
                and pg.time_source == "search_hint"
                and pg.time
                and pg.time < first
            ]
        return sorted(pages, key=lambda pg: pg.time)[:n]

    def _safe_verify_page(self, pg: Page) -> None:
        try:
            self._spend()
            facts = self.deps.oxylabs.scrape_facts(pg.url, deadline=self._provider_deadline())
            call = dict(self.deps.oxylabs.last_call)
        except Exception as exc:
            log.info("verify page %s failed: %s", pg.url, exc)
            return
        t, unc = _page_time(facts.published_at)
        rid = self.receipt(
            "oxylabs",
            "/scrape",
            f"Older page read: {facts.publisher or domain(facts.final_url)}"
            + (f", published {facts.published_at}" if facts.published_at else ", no date found"),
            source_url=facts.final_url or pg.url,
            call=call,
        )
        with self.lock:
            pg.final_url = facts.final_url or pg.url
            pg.publisher = facts.publisher or pg.publisher
            pg.author = facts.author or pg.author
            pg.published_at = facts.published_at
            pg.claims = facts.claims or pg.claims
            pg.receipt_id = rid
            if t is not None:
                pg.time, pg.time_source, pg.time_uncertainty_s = t, "page", unc

    def _safe_verify(self, p: Post, reference: str) -> None:
        try:
            self._verify_x(p, reference)
        except BudgetExceeded:
            pass
        except Exception as exc:
            log.info("verify %s failed: %s", p.url, exc)

    def _safe_x_author(self, handle: str) -> None:
        try:
            self._x_author(handle, False)
        except Exception as exc:
            log.info("copier profile @%s not read: %s", handle, exc)

    def _verify_x(self, p: Post, reference: str) -> None:
        sid = p.id.split(":", 1)[1]
        md, call = self._scrape_x(p.url)
        page = parse_post_page(md, sid)
        m = page.main
        if m is None:
            return
        subj = self.result.subject_id
        sim_own = textsim.similarity(m.text, reference)
        sim_quoted = textsim.similarity(m.quoted_text, reference) if m.quoted_text else 0.0
        replies_to_subject = any(f"x:{a.status_id}" == subj for a in page.ancestors)
        quoted_pid = f"x:{m.quoted_id}" if m.quoted_id else None
        quoted_other = None  # the post it quotes, when that is another post carrying the text
        if subj and quoted_pid == subj:
            role, sim = "quote", 1.0
        elif quoted_pid and sim_quoted >= COPY_MIN:
            role, sim = "quote", sim_quoted
            quoted_other = quoted_pid
        elif replies_to_subject:
            role, sim = "reply", sim_own
        elif sim_own >= COPY_MIN:
            role, sim = "copy", sim_own
        else:
            role, sim = "mention", sim_own
        if role == "mention" and sim < X_MIN_SIM:
            with self.lock:  # its own page shows it isn't about this text: drop the search hit
                self.result.posts = [q for q in self.result.posts if q.id != p.id]
            return
        kind = {
            "quote": "quotes it",
            "reply": "replies to it",
            "copy": f"copies it ({sim:.0%} same words)",
            "mention": "mentions it",
        }[role]
        rid = self.receipt(
            "oxylabs",
            "/scrape",
            f"@{m.handle}'s post read: {kind}; {_fmt(m.views)} views, {_fmt(m.likes)} likes",
            source_url=p.url,
            call=call,
        )
        post = self._x_post_from_item(
            m, role, sim=round(sim, 2), rid=rid, verified=True, as_of=_now()
        )
        if m.quoted_text and role == "quote":
            post.text = (m.text + "\n\n> " + m.quoted_text).strip()
        self.add_post(post)
        if quoted_other:
            # the quoted post's text and id were read on this page: it carries the text too
            q = self.add_post(
                Post(
                    id=quoted_other,
                    platform="x",
                    url=x_post_url(m.quoted_handle, m.quoted_id),
                    author_handle=m.quoted_handle,
                    text=m.quoted_text or "",
                    created_at=snowflake_time(m.quoted_id),
                    time_source="snowflake",
                    role="copy" if sim_quoted >= COPY_MIN else "mention",
                    similarity=round(sim_quoted, 2),
                    receipt_id=rid,
                    verified=True,
                )
            )
            self.add_edge(q.id, post.id, "quote", note="quotes this post")
        elif subj and role in ("quote", "reply"):
            self.add_edge(subj, post.id, role)

    def _copies_bsky(self) -> None:
        phrase = textsim.phrase(self._subject_text, 8) or textsim.phrase(self._card_title, 8)
        if not phrase or not self.deps.bsky:
            self.skip("copies_bsky", "text too short" if not phrase else "Bluesky not configured")
            return
        with self.step("copies_bsky") as st:
            q = f'"{phrase}"'
            self._spend()
            posts = self.deps.bsky.search(q, limit=25, deadline=self._provider_deadline())
            rid = self.receipt(
                "bluesky",
                "searchPosts",
                f"Bluesky search for the exact text: {len(posts)} posts",
                query=q,
                source_url=f"https://bsky.app/search?q={q}",
                call=self.deps.bsky.last_call,
            )
            ref = self._subject_text or phrase
            found = []
            for pv in posts:
                if pv.get("uri") == self.result.subject_id:
                    continue
                text = (pv.get("record") or {}).get("text") or ""
                sim = textsim.similarity(text, ref)
                sim = max(sim, textsim.similarity(text, ref, truncated=True) * 0.9)
                if sim < COPY_MIN:
                    continue
                bp = self.add_post(self._bsky_post_obj(pv, "bluesky", round(sim, 2), rid))
                if bp.author_handle:
                    self._bsky_handles.add(bp.author_handle)
                found.append(bp.id)
            st.found = found
            st.detail = f"{len(found)} Bluesky posts with the same text"

    def _coverage(self) -> None:
        q = self._card_title or textsim.phrase(self._subject_text, 14)
        if not q:
            self.skip("coverage", "nothing to search for")
            return
        with self.step("coverage") as st:
            q = q.replace('"', "")
            hits, call = self._search(q, 10)
            at = _now()
            rid = self.receipt(
                "oxylabs", "/search", f"Coverage search: {len(hits)} results", query=q, call=call
            )
            ref = self._subject_text or q
            found = []
            for h in hits:
                if X_STATUS_RE.match(h.url) or "x.com/" in h.url or "twitter.com/" in h.url:
                    continue
                if (nid := self._classify_hit(h, rid, at, reference=ref)) and nid.startswith(
                    "page:"
                ):
                    found.append(nid)
            st.found = found
            st.detail = f"{len(found)} pages on the story"

    # ---------------------------------------------------------------- media (Nemotron)
    def _media(self, ocr_as_text: bool = False) -> None:
        urls = self._media_urls[: self.deps.media_max]
        if not urls:
            self.skip("media", "the post has no images")
            return
        if not self.deps.nemotron:
            self.skip("media", "Nemotron is not configured on this server")
            return
        with self.step("media") as st:
            from media.nemotron import MediaInput, is_vl_model

            http = self.deps.http or httpx.Client(timeout=20, follow_redirects=False)
            texts, read = [], 0
            for url in urls:
                if domain(url) not in MEDIA_HOSTS or not url.startswith("https://"):
                    continue  # only the platforms' own image CDNs are downloaded
                own_media = "/card_img/" not in url  # link-card thumbnails: describe only
                data, mime = _download_image(http, url)
                if data is None:
                    continue
                h = hashlib.sha256(data).hexdigest()
                self._spend(45)  # one bounded Nemotron attempt
                started = time.monotonic()
                a = self.deps.nemotron.analyze(
                    MediaInput(media_hash=f"lookup:{h}", mime_type=mime, data=data, url=url),
                    context=self._subject_text,
                )
                if not is_vl_model(a.model):
                    self.note(
                        f"Media reading skipped: the endpoint answered as {a.model}, "
                        "not a vision-language model."
                    )
                    continue
                rid = self.receipt(
                    "nemotron",
                    "chat.completions",
                    f"Nemotron read the image: {a.description[:200]}",
                    source_url=url,
                    latency_ms=a.latency_ms or int((time.monotonic() - started) * 1000),
                )
                with self.lock:
                    self.result.media.append(
                        MediaFinding(
                            media_url=url,
                            post_id=self.result.subject_id,
                            description=a.description,
                            extracted_text=a.extracted_text,
                            reuse_cues=a.reuse_cues,
                            manipulation_cues=a.manipulation_cues,
                            confidence=a.confidence,
                            model=a.model,
                            latency_ms=a.latency_ms,
                            receipt_id=rid,
                        )
                    )
                read += 1
                if own_media:
                    texts.extend(a.extracted_text)
            found = []
            # the longest line first: headers ("NETWORK & OPERATIONS", dates) make poor queries
            texts = sorted(texts, key=lambda t: -len(t.split()))
            ocr = textsim.phrase(" ".join(texts), 10)
            if len(textsim.tokens(" ".join(texts))) < OCR_MIN_WORDS:
                ocr = None
            if ocr and ocr_as_text:
                # the post is the image: its text is what the copy searches look for
                self._subject_text = " ".join(texts)
                self.note(
                    "The post has no text of its own; the searches use the text Nemotron "
                    "read inside its image."
                )
                ocr = None
            if ocr and textsim.similarity(ocr, self._subject_text, truncated=True) < VERBATIM:
                # the image's own text, searched like the post text: where does it circulate?
                q = f'"{ocr}"'
                hits, call = self._search(q, 10)
                at = _now()
                rid = self.receipt(
                    "oxylabs",
                    "/search",
                    f"Search for the text inside the image (Nemotron OCR): {len(hits)} results",
                    query=q,
                    call=call,
                )
                for hit in hits:
                    ref = " ".join(texts)
                    if nid := self._classify_hit(hit, rid, at, reference=ref, min_sim=COPY_MIN):
                        found.append(nid)
                        if self.result.subject_id and nid.startswith("x:"):
                            self.add_edge(
                                nid,
                                self.result.subject_id,
                                "media",
                                tool="nemotron",
                                note="same text inside the image",
                            )
            st.found = found
            st.detail = f"{read} image(s) read" + (
                f" · OCR text found in {len(found)} places"
                if ocr
                else " · its text drives the searches"
                if ocr_as_text and texts
                else ""
            )

    # ---------------------------------------------------------------- monitor
    def _monitor(self) -> None:
        if not self.deps.store:
            self.skip("monitor", "database not configured")
            return
        with self.step("monitor") as st:
            from signals.textnorm import normalize_url

            urls = {u for u in (self.result.input.url, self._card_url) if u}
            with self.lock:
                urls |= {
                    pg.final_url for pg in self.result.pages if pg.role == "linked" and pg.final_url
                }
            variants = sorted({v for u in urls for v in (u, normalize_url(u)) if v})
            phrase = textsim.phrase(self._subject_text)
            found: dict[int, Any] = {}
            why: dict[int, set[str]] = {}

            def add(rows, reason: str) -> None:
                for c in rows:
                    found[c.id] = c
                    why.setdefault(c.id, set()).add(reason)

            with self.lock:
                handles = list(self._bsky_handles)[:10]
            try:
                store_cm = self.deps.store()
                store = store_cm.__enter__()
            except Exception as exc:  # DB down: say so, don't fail the lookup
                log.warning("lookup monitor: database unreachable: %s", type(exc).__name__)
                with self.lock:
                    st.state, st.detail = "skipped", "monitor database unreachable right now"
                return
            try:
                if variants:
                    add(store.clusters_for_url(variants), "link")
                if phrase:
                    add(store.clusters_for_text(phrase), "text")
                for h in handles:  # accounts seen in this lookup: context, not a verdict on it
                    add(store.clusters_for_handle(h), "account")
            finally:
                store_cm.__exit__(None, None, None)
            # the verdict applies only when the cluster shared this link or this text
            applies = {cid for cid, r in why.items() if r & {"link", "text"}}
            self.receipt(
                "detector",
                "db",
                f"Monitored clusters checked: {len(applies)} share the link or text, "
                f"{len(found) - len(applies)} only share an account",
                query=phrase,
            )
            rows = []
            for cid, c in found.items():
                row = c.model_dump(mode="json")
                row["match"] = sorted(why[cid])
                row["verdict_applies"] = cid in applies
                rows.append(row)
            rows.sort(key=lambda r: (not r["verdict_applies"], -(r.get("score") or 0)))
            with self.lock:
                self.result.clusters = rows
                if applies:
                    self.result.status = "matched"
            if set(found) - applies:
                self.note(
                    "Some accounts in this lookup also appear in monitored clusters about other "
                    "content. That is context about those accounts, not a verdict on this link."
                )
            st.detail = (
                f"the link or text is in {len(applies)} monitored cluster(s)"
                if applies
                else f"accounts seen here are in {len(found)} other cluster(s); not this link"
                if found
                else "not in the Bluesky clusters we monitor"
            )

    # ---------------------------------------------------------------- graph + assess
    def _finish_graph(self) -> None:
        with self.lock:
            posts = self.result.posts
            subj = next((p for p in posts if p.id == self.result.subject_id), None)
            # posts that carry the submitted text, oldest first
            carriers = sorted(
                (
                    p
                    for p in posts
                    if p.created_at
                    and p.verified  # only posts read on their own page or API
                    and p.role in ("submitted", "copy", "bluesky")
                    and (p.similarity or 0) >= COPY_MIN
                ),
                key=lambda p: p.created_at,
            )
            origin_id = carriers[0].id if carriers else self.result.subject_id
            origin_t = carriers[0].created_at if carriers else self._subject_time

            # a page that carries the text is the origin only if even the latest time its date
            # allows (hint precision) is before every post
            def latest(pg: Page) -> datetime:
                return pg.time + timedelta(seconds=pg.time_uncertainty_s or 0)

            older = sorted(
                (
                    pg
                    for pg in self.result.pages
                    if pg.role in ("reference", "linked")
                    and pg.time
                    and pg.time_source == "page"  # read from the page, not a search snippet
                    and pg.time_uncertainty_s is not None
                    and origin_t
                    and latest(pg) < origin_t - timedelta(minutes=5)
                ),
                key=latest,
            )
            if older:
                origin_id = older[0].id
                # a linked origin already has its edge to the post that links it (_linked);
                # an older copy of the text did not necessarily link it
                if carriers and older[0].role != "linked":
                    self.add_edge(
                        origin_id,
                        carriers[0].id,
                        "cites",
                        note="older page with the same text (date read on the page)",
                    )
            self.result.origin_id = origin_id
            if carriers and carriers[0].role == "copy" and carriers[0].id == origin_id:
                carriers[0].role = "origin"
            for p in carriers[1:]:
                self.add_edge(
                    carriers[0].id,
                    p.id,
                    "copy",
                    sim=p.similarity,
                    tool="bluesky" if p.platform == "bluesky" else "oxylabs",
                    note="same text, later",
                )
            if subj:
                for pg in self.result.pages:
                    if pg.role in ("coverage", "reference") and pg.id != origin_id:
                        self.add_edge(subj.id, pg.id, "cites", note="same story")
            self.result.spread = _spread(carriers)
            if any(p.platform == "x" for p in posts):
                self.note(
                    "X counts are what the logged-out page showed when it was read; X "
                    "hides zero counts. Post times come from the post ids."
                )
            self.note("Search results are a sample of where the text appears, not every copy.")
            if self.result.status != "matched":
                self.note(
                    "Not in the Bluesky clusters we monitor, so there is no coordination "
                    "verdict. One link's footprint can't show coordination; the copy "
                    "pattern below is data, not a verdict."
                )

    def _assess(self) -> None:
        if not self.deps.assessor:
            self.skip("assess", "Gemini is not configured on this server")
            return
        with self.lock:
            empty = not self.result.posts and not self.result.pages
        if empty:
            self.skip("assess", "nothing was found to read")
            return
        with self.step("assess") as st:
            self._spend(need_s=15, reserve_s=0)
            with self.lock:
                snap = self.result.model_copy(deep=True)
            a = self.deps.assessor.assess(snap, deadline=self.deadline)
            rid = self.receipt(
                "gemini",
                "generateContent",
                f"{a.model} read the receipts: {a.summary[:200]}",
                latency_ms=a.latency_ms,
            )
            with self.lock:
                self.result.assessment = a
            st.detail = f"{a.model} · cites {len(a.cites)} receipts · receipt #{rid}"


MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _download_image(http: httpx.Client, url: str) -> tuple[bytes | None, str]:
    """Stream at most MAX_IMAGE_BYTES of an image; (None, '') if it isn't an image or too big."""
    with http.stream("GET", url, headers={"user-agent": UA}) as res:
        res.raise_for_status()
        mime = res.headers.get("content-type", "").split(";")[0].strip()
        if not mime.startswith("image/"):
            return None, ""
        buf = bytearray()
        for chunk in res.iter_bytes():
            buf += chunk
            if len(buf) > MAX_IMAGE_BYTES:
                return None, ""
    return bytes(buf), mime


def _spread(posts: list[Post]) -> Spread | None:
    if not posts:
        return None
    accts = {(p.platform, (p.author_handle or "").lower()) for p in posts}
    verb = [p for p in posts if (p.similarity or 0) >= VERBATIM]
    first, last = posts[0].created_at, posts[-1].created_at
    hour = {
        (p.platform, (p.author_handle or "").lower())
        for p in posts
        if (p.created_at - first).total_seconds() <= 3600
    }
    gaps = [
        (b.created_at - a.created_at).total_seconds()
        for a, b in zip(verb, verb[1:], strict=False)
        if (a.author_handle or "").lower() != (b.author_handle or "").lower()
    ]
    return Spread(
        n_posts=len(posts),
        n_accounts=len(accts),
        n_verbatim=len(verb),
        first_at=first,
        last_at=last,
        span_s=(last - first).total_seconds(),
        first_hour_accounts=len(hour),
        min_gap_s=min(gaps) if gaps else None,
    )


def _fmt(n: int | None) -> str:
    if n is None:
        return "?"
    for d, s in ((1_000_000, "M"), (1_000, "K")):
        if n >= d:
            return f"{n / d:.1f}".rstrip("0").rstrip(".") + s
    return str(n)


@contextmanager
def _null_step() -> Iterator[Step]:
    yield Step(id="_", tool="_", label="_")
