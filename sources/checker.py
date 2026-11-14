"""Source checks for one cluster: what its shared links say, and where its message appears.

Produces `SourceEvidence` items; storing them is `sources.store`'s job. Every item carries the
URL it came from. Failures are logged and skipped: a missing check is not evidence either way.
"""

import logging
import time
from collections.abc import Mapping, Sequence
from typing import Protocol

from sources.links import claim_phrase, domain, top_links
from sources.models import ClusterPost, PageFacts, SearchHit, SourceEvidence
from sources.oxylabs import OxylabsError

log = logging.getLogger(__name__)

SCRAPE_MODEL = "oxylabs-web-api/scrape"
SEARCH_MODEL = "oxylabs-web-api/search"
EARLIEST_NOTE = "Earliest date shown among the top search results. A hint, not proof of origin."


class WebClient(Protocol):
    def search(self, query: str, max_results: int = 10) -> list[SearchHit]: ...
    def scrape_facts(self, url: str) -> PageFacts: ...


class SourceChecker:
    def __init__(
        self,
        client: WebClient,
        *,
        max_links: int = 3,
        max_hits: int = 8,
        cache_ttl_s: float = 6 * 3600,
        failure_ttl_s: float = 1800,
    ):
        self._client = client
        self._max_links = max_links
        self._max_hits = max_hits
        self._ttl = cache_ttl_s
        self._failure_ttl = failure_ttl_s
        # key -> (expires_at, value); value None = the call failed recently, don't retry yet
        self._cache: dict[str, tuple[float, object]] = {}

    def check(
        self,
        posts: Sequence[ClusterPost],
        *,
        stored: Mapping[str, dict] | None = None,
        adhoc: bool = False,
    ) -> list[SourceEvidence]:
        """Evidence for checks not already stored. `stored` maps evidence key -> detail.

        `adhoc`: the posts are someone's own input (/api/analyze), not a cluster, so summaries
        describe that input and carry no account counts.
        """
        stored = stored or {}
        n_accounts = len({p.did for p in posts})
        out: list[SourceEvidence] = []

        for url, shared_by in top_links(posts, self._max_links):
            key = f"page:{url}"
            if key in stored:  # scraped in an earlier pass: reuse its headline, don't re-scrape
                headline = stored[key].get("headline")
            else:
                facts = self._cached(f"scrape:{url}", lambda u=url: self._client.scrape_facts(u))
                if facts is None:
                    continue
                if adhoc:  # one person's input: account counts would read as a cluster
                    out.append(_page_evidence(key, facts, None, None))
                else:
                    out.append(_page_evidence(key, facts, shared_by, n_accounts))
                headline = facts.headline
            if headline:
                ev = self._search_evidence("headline", headline, stored, adhoc)
                if ev:
                    out.append(ev)

        phrase = claim_phrase(p.text for p in posts)
        if phrase:
            ev = self._search_evidence("post_text", phrase, stored, adhoc)
            if ev:
                out.append(ev)
        return out

    def _search_evidence(
        self, purpose: str, phrase: str, stored: Mapping[str, dict], adhoc: bool = False
    ) -> SourceEvidence | None:
        key = f"search:{purpose}:{phrase.lower()}"
        if key in stored:
            return None
        query = f'"{phrase}"'
        hits = self._cached(f"search:{query}", lambda: self._client.search(query, self._max_hits))
        if hits is None:
            return None
        return _search_evidence(key, purpose, phrase, query, hits, adhoc=adhoc)

    def _cached(self, key: str, fetch):
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now < hit[0]:
            return hit[1]
        try:
            value = fetch()
        except OxylabsError as exc:
            log.warning("source check skipped for %.0fs (%s): %s", self._failure_ttl, key, exc)
            self._cache[key] = (now + self._failure_ttl, None)
            return None
        self._cache[key] = (now + self._ttl, value)
        return value


def _page_evidence(
    key: str, f: PageFacts, shared_by: int | None, n_accounts: int | None
) -> SourceEvidence:
    """`shared_by`/`n_accounts` are None for ad-hoc input, which has no accounts to count."""
    title = f'"{f.headline}"' if f.headline else domain(f.final_url)
    by = ", ".join(x for x in (f.publisher, f.author, f.published_at) if x) or "publisher unknown"
    if shared_by is None or n_accounts is None:
        summary = f"The linked page: {title} ({by})."
    else:
        summary = f"Linked page shared by {shared_by} of {n_accounts} accounts: {title} ({by})."
    if f.http_status and f.http_status != 200:
        summary += f" The page returned HTTP {f.http_status}."
    return SourceEvidence(
        summary=summary,
        detail={
            "key": key,
            "type": "linked_page",
            "url": f.url,
            "final_url": f.final_url,
            "domain": domain(f.final_url),
            "http_status": f.http_status,
            "shared_by_accounts": shared_by,
            "cluster_accounts": n_accounts,
            "publisher": f.publisher,
            "author": f.author,
            "published_at": f.published_at,
            "headline": f.headline,
            "claims": f.claims,
        },
        source_url=f.final_url,
        model=SCRAPE_MODEL,
    )


def _search_evidence(
    key: str, purpose: str, phrase: str, query: str, hits: list[SearchHit], *, adhoc: bool = False
) -> SourceEvidence:
    domains = sorted({domain(h.url) for h in hits})
    dated = [h for h in hits if h.published_hint]
    earliest = min(dated, key=lambda h: (h.published_hint, h.position)) if dated else None
    if purpose == "post_text":
        what = "Your text" if adhoc else "The cluster's message text"
    else:
        what = "The linked headline"
    if not hits:
        summary = f'{what} returned no web search results for "{phrase}".'
    else:
        summary = f"{what} matches {len(hits)} web results on {len(domains)} domains"
        if earliest:
            summary += f"; earliest dated: {domain(earliest.url)} ({earliest.published_hint})"
        summary += "."
    return SourceEvidence(
        summary=summary,
        detail={
            "key": key,
            "type": "web_search",
            "purpose": purpose,
            "query": query,
            "distinct_domains": len(domains),
            "hits": [
                {
                    "url": h.url,
                    "domain": domain(h.url),
                    "title": h.title,
                    "snippet": h.snippets[0] if h.snippets else "",
                    "position": h.position,
                    "published_hint": h.published_hint.isoformat() if h.published_hint else None,
                }
                for h in hits
            ],
            "earliest_dated": (
                {"url": earliest.url, "published_hint": earliest.published_hint.isoformat()}
                if earliest
                else None
            ),
            "note": EARLIEST_NOTE,
        },
        source_url=earliest.url if earliest else (hits[0].url if hits else None),
        model=SEARCH_MODEL,
    )
