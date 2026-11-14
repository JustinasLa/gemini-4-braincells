"""Client for the Oxylabs Web API (https://webapi.oxylabs.io/v1): live /search and /scrape.

Auth is the API key as a Bearer token (Basic auth is rejected by the Web API). Every call is
logged with endpoint, HTTP status, latency and the Oxylabs request id.
"""

import logging
import re
import threading
import time
from datetime import date, datetime
from typing import Any

import httpx

from common.config import get_settings
from lookup.xpage import page_unavailable_reason
from sources.links import X_HOSTS, domain
from sources.models import PageFacts, SearchHit

log = logging.getLogger(__name__)

BASE_URL = "https://webapi.oxylabs.io/v1"
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

# Schema for /scrape's AI extraction. Every field is optional: many pages lack some of them.
FACTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "publisher": {"type": "string", "description": "Organisation or site that published it"},
        "author": {"type": "string", "description": "Named author, if any"},
        "published_at": {"type": "string", "description": "Publication date, ISO 8601"},
        "headline": {"type": "string", "description": "Headline or page title"},
        "claims": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Up to 3 main factual claims the page makes, one sentence each",
        },
    },
}

_DATE_PREFIX = re.compile(r"^\s*([A-Z][a-z]{2,8})\.? (\d{1,2}), (\d{4})\s*[—–\-·]")


class OxylabsError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.request_id = request_id


def leading_date(snippet: str) -> date | None:
    """'Dec 19, 2024 — text' -> date(2024, 12, 19). Relative dates ('1 month ago') -> None."""
    m = _DATE_PREFIX.match(snippet)
    if not m:
        return None
    month, day, year = m.groups()
    for fmt, mon in (("%B %d %Y", month), ("%b %d %Y", month[:3])):
        try:
            return datetime.strptime(f"{mon} {day} {year}", fmt).date()
        except ValueError:
            continue
    return None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


class OxylabsClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        backoff_s: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ):
        if not api_key:
            raise ValueError("OXYLABS_WEB_API_KEY is not set")
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._local = threading.local()

    @property
    def last_call(self) -> dict:
        """{endpoint, status, latency_ms, request_id} of this thread's latest call (receipts)."""
        return getattr(self._local, "last_call", {})

    @classmethod
    def from_settings(cls) -> "OxylabsClient":
        return cls(get_settings().oxylabs_web_api_key)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OxylabsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def search(
        self, query: str, max_results: int = 10, *, deadline: float | None = None
    ) -> list[SearchHit]:
        data = self._post("/search", {"query": query, "max_results": max_results}, deadline)
        hits = []
        for i, r in enumerate(data.get("results") or [], start=1):
            if not r.get("url"):
                continue
            snippets = [s for s in r.get("outline") or [] if isinstance(s, str)]
            hits.append(
                SearchHit(
                    url=r["url"],
                    title=r.get("title") or "",
                    snippets=snippets,
                    position=(r.get("metadata") or {}).get("position", i),
                    published_hint=leading_date(snippets[0]) if snippets else None,
                )
            )
        return hits

    def scrape_facts(self, url: str, *, deadline: float | None = None) -> PageFacts:
        """Scrape `url` and let Oxylabs extract publisher, date, headline and main claims."""
        data = self._post(
            "/scrape",
            {"url": url, "output": ["json", "markdown"], "json": {"schema": FACTS_SCHEMA}},
            deadline,
        )
        results = data.get("results") or []
        if not results:
            raise OxylabsError(f"/scrape returned no result for {url}")
        meta = results[0].get("metadata") or {}
        facts = results[0].get("json")
        facts = facts if isinstance(facts, dict) else {}
        claims = facts.get("claims") if isinstance(facts.get("claims"), list) else []
        return PageFacts(
            url=url,
            final_url=meta.get("url") or url,
            http_status=((meta.get("statuses") or {}).get("http_request") or {}).get("code"),
            publisher=_text(facts.get("publisher")),
            author=_text(facts.get("author")),
            published_at=_text(facts.get("published_at")),
            unavailable=(
                page_unavailable_reason(results[0].get("markdown") or "", x_url)
                if (
                    x_url := next(
                        (u for u in (meta.get("url"), url) if u and domain(u) in X_HOSTS), None
                    )
                )
                else None
            ),
            headline=_text(facts.get("headline")),
            claims=[c for c in (_text(x) for x in claims) if c][:5],
        )

    def scrape_markdown(self, url: str, *, deadline: float | None = None) -> tuple[str, dict]:
        """The page as markdown (no AI extraction) plus the result metadata (final url, status)."""
        data = self._post("/scrape", {"url": url, "output": ["markdown"]}, deadline)
        results = data.get("results") or []
        if not results or not isinstance(results[0].get("markdown"), str):
            raise OxylabsError(f"/scrape returned no markdown for {url}")
        return results[0]["markdown"], results[0].get("metadata") or {}

    def _post(self, path: str, body: dict, deadline: float | None = None) -> dict:
        """`deadline` (time.monotonic()): each attempt's timeout is clamped to it and no retry
        starts after it."""
        attempt = 0
        while True:
            started = time.monotonic()
            status: int | None = None
            payload: dict = {}
            try:
                timeout = None
                if deadline is not None:
                    left = deadline - time.monotonic()
                    if left <= 1:
                        raise OxylabsError(f"oxylabs {path}: time budget used up")
                    timeout = min(self._http.timeout.read or left, left)
                res = (
                    self._http.post(path, json=body, timeout=timeout)
                    if timeout is not None
                    else self._http.post(path, json=body)
                )
                status = res.status_code
                try:
                    payload = res.json()
                except ValueError:
                    payload = {}
            except httpx.TransportError as exc:
                log.warning("oxylabs %s transport error: %s", path, type(exc).__name__)
            request_id = (payload.get("metadata") or {}).get("request_id")
            self._local.last_call = {
                "endpoint": path,
                "status": status,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "request_id": request_id,
            }
            log.info(
                "oxylabs call endpoint=%s status=%s latency_ms=%d request_id=%s attempt=%d",
                path,
                status,
                (time.monotonic() - started) * 1000,
                request_id,
                attempt + 1,
            )
            if status == 200:
                return payload
            out_of_time = deadline is not None and (
                time.monotonic() + self._backoff_s * 2**attempt >= deadline
            )
            if (
                (status is None or status in RETRY_STATUS)
                and attempt < self._max_retries
                and not out_of_time
            ):
                time.sleep(self._backoff_s * 2**attempt)
                attempt += 1
                continue
            reason = payload.get("detail") or payload.get("title") or "request failed"
            raise OxylabsError(
                f"oxylabs {path} failed ({status}): {reason}", status=status, request_id=request_id
            )
