"""Bounded reverse-image discovery and optional visual comparison, independent of OCR."""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from urllib.parse import urljoin, urlsplit

import httpx

from lookup.models import ImageMatch
from lookup.urls import X_HOSTS, classify, domain, snowflake_time
from sources import reverse_image

log = logging.getLogger(__name__)

DOWNLOAD_S = 5.0
DOWNLOAD_BYTES = 8 * 1024 * 1024
DIFF_S = 20.0
DIFF_NEED_S = DIFF_S + 3.0
# cap on each matched-page date read (Oxylabs /scrape with AI extraction, observed 4-12 s): the
# reads are never waited for, so this bounds their cost, not the lookup's time
PAGE_DATE_S = 15.0
# a page date before the web, or after the read, is a misread, not a date
PAGE_DATE_MIN = datetime(1991, 1, 1, tzinfo=UTC)
_X_DOMAINS = {host.removeprefix("www.") for host in X_HOSTS}
_IMAGE_FILE = re.compile(r"\.(?:jpe?g|png|gif|webp|avif|bmp|svg)$", re.I)
# slots kept for TinEye (one result page; at most half the cap): only its dated matches can be
# the image origin, so faster Yandex hits never fill the whole cap before TinEye answers
TINEYE_RESERVE = 10
MISSING = "variation matching is not installed on this server"
# a verified post with an image in one of these roles is the subject or a copy of it, so its
# image may be this picture; any other verified post counts only when its image URL is one of
# the picture's own copies (conservative: a possible carrier blocks an origin claim)
PICTURE_ROLES = ("submitted", "origin", "copy", "quote", "bluesky")
_UNSUITABLE = re.compile(
    r"\b(?:deepfakes?|fake[ds]?|manipulated|manipulation|doctored|doctoring)\b", re.I
)


# quoted spans ("...", “...”, '...' not inside a word): a caption quoted as evidence stays as is
# one quote style per span, no other quote mark inside, short (a caption, not a paragraph):
# mismatched quotes never exempt the model's own unquoted words
_Q = "\"“”‘’'"
_QUOTED = re.compile(
    rf"(\"[^{_Q}]{{0,160}}\"|“[^{_Q}]{{0,160}}”|‘[^{_Q}]{{0,160}}’|(?<!\w)'[^{_Q}]{{0,160}}'(?!\w))"
)


_LOOKUP_IMAGE = re.compile(r"/api/lookup/([^/]+)/image$")
_BSKY_IMAGE = re.compile(r"/img/[^/]+/plain/([^/]+)/([^/@]+)")


def _media_id(url: str) -> str:
    """One identity per picture rendition family, so a URL variant never hides a carrier:
    pbs.twimg.com by media id (any format/name/extension), cdn.bsky.app by (did, cid) (any
    size), this server's upload by lookup id, else host + path without extension, query and
    case. Over-matching only withholds an origin claim (the honest direction)."""
    try:
        parsed = httpx.URL(url)
    except Exception:
        return url
    host, path = (parsed.host or "").lower(), parsed.path
    if m := _LOOKUP_IMAGE.search(path):
        return f"lookup:{m.group(1)}"
    if host == "pbs.twimg.com":
        return "twimg:" + path.rsplit("/", 1)[-1].split(".", 1)[0]
    if host == "cdn.bsky.app" and (m := _BSKY_IMAGE.search(path)):
        return f"bsky:{m.group(1)}/{m.group(2)}"
    return f"{host}{re.sub(r'\.[A-Za-z0-9]{2,5}$', '', path)}".lower()


def _family_copy(data: bytes) -> bytes:
    from lookup.family import _shrink

    return _shrink(data)


def _bound(item):
    """Latest moment the picture is known to have existed: first_seen plus its uncertainty (a
    day-only crawl or page date ends at midnight; a post id's time is exact)."""
    unc = getattr(item, "time_uncertainty_s", None)
    if unc is not None:
        return item.first_seen + timedelta(seconds=unc)
    return item.first_seen + timedelta(days=item.time_source == "crawl_date")


_FRONT_PAGES = {"", "index", "home", "default", "start", "main", "portal"}


def _host(url: str | None) -> str:
    """A URL's host without www./m.: the same site only on the same host (conservative: a
    blog host serves many independent sites, alice.blogspot.com is not bob.blogspot.com)."""
    host = (urlsplit(url or "").hostname or "").lower()
    for prefix in ("www.", "m."):
        host = host.removeprefix(prefix)
    return host


def _front_page(url: str) -> bool:
    path = urlsplit(url).path.strip("/").lower()
    return path.rsplit(".", 1)[0] in _FRONT_PAGES  # /, /index.html, /home, /default.aspx


def _same_page(asked: str, final: str) -> bool:
    """Whether the page the scraper ended on is still the matched page: a redirect to another
    host (a repurposed domain, another blog on the same service) or to the site's front page
    prints someone else's date."""
    if _host(asked) != _host(final):
        return False
    return not _front_page(final) or _front_page(asked)


# the first snowflake ids (2010-11-04) are ~10^15; sequential pre-snowflake ids (to ~3*10^10)
# would decode to the epoch's first seconds: an id whose time is within a day of the epoch is
# not read as a time
MIN_DATED_ID = 86_400_000 << 22


def _printed_day(item) -> str:
    """The day a page date prints: the middle of its window (service._page_time stores a clock
    time without a zone as that time minus 14 h, with 28 h of uncertainty), not its lower edge."""
    unc = getattr(item, "time_uncertainty_s", None) or 0
    return (item.first_seen + timedelta(seconds=unc / 2)).date().isoformat()


def _post_time(hit) -> tuple[datetime, str, str] | None:
    """(created_at, post URL, status id) when the hit's page or image URL is an X status: the
    post's own time is in its id (lookup.urls.snowflake_time), exact, and the picture was on X
    by then."""
    for url in (hit.page_url, hit.image_url):
        target = classify(url) if url else None
        if target and target.kind == "x_post" and (target.post_id or "").isdigit():
            if int(target.post_id) >= MIN_DATED_ID and (when := snowflake_time(target.post_id)):
                return when, target.url, target.post_id
    return None


def _num(value):
    return None if value is None else float(value)


def _box(value):
    return None if value is None else tuple(float(v) for v in value)


def _wording(text: str) -> str:
    """Our wording says "edited", never fake/deepfake/manipulated/doctored; quoted text (the
    caption a change quotes, e.g. Caption changed to 'FAKE NEWS') is kept verbatim."""
    parts = _QUOTED.split(text or "")
    return "".join(
        part if i % 2 else _UNSUITABLE.sub("edited", part) for i, part in enumerate(parts)
    )[:500]


def _pinned_url(raw: str) -> tuple[httpx.URL, str, str]:
    """Validate every DNS answer, then connect to a checked IP, not a second DNS lookup."""
    url = httpx.URL(raw)
    if url.scheme not in ("http", "https") or not url.host or url.userinfo or "%" in url.host:
        raise ValueError("image address is not permitted")
    port = 443 if url.scheme == "https" else 80
    if url.port is not None and url.port != port:
        raise ValueError("image port is not permitted")
    addresses = socket.getaddrinfo(url.host, port, type=socket.SOCK_STREAM)
    ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved for ip in ips):
        raise ValueError("image address is not public")
    host = f"[{url.host}]" if ":" in url.host else url.host
    authority = f"{host}:{url.port}" if url.port else host
    return url.copy_with(host=addresses[0][4][0]), authority, url.host


def start(job, pool: ThreadPoolExecutor) -> Trace:
    trace = Trace(job)
    reason = None
    if not job.deps.reverse_image:
        reason = "reverse image search is disabled on this server"
    elif job._upload:
        base = job.deps.public_base_url.rstrip("/")
        try:
            url = httpx.URL(base)
            valid = url.scheme in ("http", "https") and bool(url.host) and not url.userinfo
        except httpx.InvalidURL:
            valid = False
        if valid:
            trace.query_url = f"{base}/api/lookup/{job.result.id}/image"
        else:
            reason = "reverse image search needs a public image URL; configure PUBLIC_HOST"
    elif job.result.kind in ("x_post", "bsky_post"):
        trace.query_url = next((url for url in job._media_urls if "/card_img/" not in url), "")
        if not trace.query_url:
            reason = "the post has no image to trace (link-card thumbnails are excluded)"
    else:
        reason = "this input has no image to trace"
    if reason:
        job.skip("reverse_image", reason)
        job.skip("variations", reason)
        trace.future = Future()
        trace.future.set_result(None)
    else:
        trace.future = pool.submit(trace.run)
    return trace


class Trace:
    def __init__(self, job):
        self.job = job
        self.closed = threading.Event()
        self.query_url = ""
        self.future: Future | None = None
        self.matches: dict[str, ImageMatch] = {}
        self.aliases: dict[str, str] = {}
        self.priorities: dict[str, tuple] = {}
        self.next_match = 1
        self.out_of_time = False  # a call was refused by the time budget (skip, not failure)
        # ids queued for download + comparison: never evicted, never more than the cap in all
        self.admitted: set[str] = set()
        self.tried: dict[str, set[str]] = {}  # match id -> URLs already downloaded or refused
        self.failed: set[str] = set()  # admitted, every URL tried, no comparison
        self.compare_receipts: dict[str, int | None] = {}  # match id -> its comparison receipt
        self.compared: set[str] = set()  # comparisons published (incl. "different", dropped)
        self.tineye_settled = False  # TinEye answered or failed: its reserve is open to Yandex
        self.overflow: list[tuple] = []  # (hit, rid) Yandex hits held back for the reserve
        # every downloaded copy, "different" ones included, and the submitted picture: the
        # family step (lookup/family.py) matches the photograph inside them
        self.images: dict[str, bytes] = {}
        self.base: bytes | None = job._upload
        self.post_receipts: dict[str, int] = {}  # X status URL -> its post-id time receipt
        self.page_reads: set[str] = set()  # page URLs (being) read for their printed date
        self.page_dates: dict[str, tuple[datetime, float | None, int]] = {}  # url -> date read
        # match id -> every page it was found on (a match shows one page_url; a later hit for
        # the same picture can come from another page, and any of them can date it)
        self.match_pages: dict[str, list[str]] = {}
        self.page_futures: list[Future] = []
        # no more page dates: the family step read its copies' dates, or finish() chose the origin
        self.settled = threading.Event()

    def _active(self) -> bool:
        return (
            not self.closed.is_set()
            and self.job._final is None
            and time.monotonic() < self.job._provider_deadline()
        )

    def _step(self, sid, state, detail=None, found=()):
        with self.job.lock:
            if not self._active():
                return
            step = self.job._step(sid)
            step.state, step.detail, step.found = (
                state,
                _wording(detail) if detail else detail,
                list(found),
            )
            if state == "running":
                step.started_at = datetime.now(UTC)
            else:
                step.ended_at = datetime.now(UTC)

    def _receipt(self, tool, endpoint, summary, url, need_s=3.0):
        with self.job.lock:
            if not self._active():
                return None
            from lookup.service import BudgetExceeded

            try:
                self.job._spend(need_s)
            except BudgetExceeded:
                self.out_of_time = True
                return None
            return self.job.receipt(tool, endpoint, _wording(summary), source_url=url, ok=False)

    def _pages_open(self) -> bool:
        """Page-date reads outlive the trace's future (they never delay it or the family step)
        and count until finish() chooses the origin."""
        return (
            not self.settled.is_set()
            and self.job._final is None
            and time.monotonic() < self.job._provider_deadline()
        )

    def freeze_page_dates(self):
        """No more page dates: the family step (lookup/family.py) has read its copies' dates,
        and the image origin must rest on the same ones (job.lock held or not)."""
        with self.job.lock:
            self.settled.set()

    def _complete(
        self, rid, summary, *, ok=True, call=None, source_url=None, latency_ms=None, gate=None
    ):
        with self.job.lock:
            if not (gate or self._active)():
                return
            receipt = self.job.result.receipts[rid - 1]
            receipt.summary, receipt.ok = _wording(summary)[:400], ok
            if source_url:
                receipt.source_url = source_url
            if call:
                receipt.request_id = call.get("request_id")
                receipt.latency_ms = call.get("latency_ms")
                receipt.cached = bool(call.get("cached"))
                if receipt.cached and call.get("fetched_at"):
                    receipt.at = call["fetched_at"]
            if latency_ms is not None:
                receipt.latency_ms = latency_ms

    def _publish_match(self, match):
        with self.job.lock:
            if self._active() and match.id in self.matches:
                fields = {
                    "relation",
                    "distance",
                    "crop_box",
                    "edited_region",
                    "relation_note",
                    "changes",
                    "change_kinds",
                    "changes_from",
                    "changes_withdrawn",
                    "receipt_id",
                }
                self.matches[match.id] = self.matches[match.id].model_copy(
                    update=match.model_dump(include=fields)
                )
                if match.relation != "unknown":
                    self.compared.add(match.id)
                self._sync_matches()

    def _sync_matches(self):
        # thread pictures of the same template (lookup/threadvariants.py) are not this trace's
        templates = [m for m in self.job.result.image_matches if m.relation == "template"]
        self.job.result.image_matches = [
            self.matches[mid]
            for mid in sorted(self.matches, key=self.priorities.__getitem__)
            if self.matches[mid].relation != "different"
        ] + templates

    def _accept(self, result, rid):
        return self._accept_hits([(hit, rid) for hit in result.hits])

    def _settle_tineye(self):
        """TinEye answered (or failed): its reserved slots open to the Yandex hits held back."""
        with self.job.lock:
            self.tineye_settled = True
            held, self.overflow = self.overflow, []
        return self._accept_hits(held)

    def _accept_hits(self, pairs):
        added = []
        with self.job.lock:
            if not self._active():
                return added
            for hit, rid in sorted(pairs, key=lambda pair: self._priority(pair[0])):
                posted = _post_time(hit)
                if posted and (not hit.first_seen or posted[0] < _bound(hit)):
                    # the post's exact time beats a crawl date that day or later
                    hit = replace(hit, first_seen=posted[0], time_source="snowflake")
                else:
                    posted = None
                urls = [url for url in (hit.image_url, hit.thumb_url) if url]
                mid = next((self.aliases[url] for url in urls if url in self.aliases), None)
                if mid is not None:
                    match = self.matches[mid].model_copy(deep=True)
                    if match.page_url is None and hit.page_url:
                        # the page's own domain comes with it (not the image host's)
                        match.page_url, match.domain = hit.page_url, hit.domain or match.domain
                    for field in ("domain", "thumb_url", "width", "height"):
                        if getattr(match, field) is None:
                            setattr(match, field, getattr(hit, field))
                    if mid in self.failed and any(
                        url not in self.tried.get(mid, ()) for url in urls
                    ):
                        # a new URL for a match whose downloads all failed: one more try
                        self.failed.discard(mid)
                        added.append(match)
                    if (
                        hit.first_seen
                        and hit.time_source
                        and (not match.first_seen or _bound(hit) < _bound(match))
                    ):
                        match.first_seen, match.time_source = hit.first_seen, hit.time_source
                        match.time_uncertainty_s = None
                        if hit.page_url:  # the page shown is the one the date is about
                            match.page_url = hit.page_url
                            match.domain = hit.domain or domain(hit.page_url)
                        match.time_receipt_id = self._post_receipt(posted) if posted else None
                        match.engine = hit.engine
                    elif not match.first_seen and hit.engine == "tineye":
                        match.engine = hit.engine
                    pages = self.match_pages.setdefault(mid, [])
                    if hit.page_url and hit.page_url not in pages:
                        pages.append(hit.page_url)
                    match = self._with_page_date(match)  # a page read for another match
                    self.matches[mid] = match
                    if match.changes_from and self._match_is_older(match) != (
                        match.changes_from == "match"
                    ):
                        self._withdraw_description(mid)
                    self.priorities[mid] = self._priority(
                        match, min(self.priorities[mid][2], hit.rank)
                    )
                else:
                    cap = max(0, self.job.deps.image_matches_max)
                    limit = (
                        cap
                        if hit.engine == "tineye" or self.tineye_settled
                        else cap - min(TINEYE_RESERVE, cap // 2)
                    )
                    if len(self.matches) >= limit:
                        if hit.engine != "tineye" and not self.tineye_settled and cap > limit:
                            self.overflow.append((hit, rid))
                            continue
                        # only a match not yet queued for download can make room
                        evictable = [m for m in self.matches if m not in self.admitted]
                        if not evictable or len(self.matches) > limit:
                            continue
                        worst = max(evictable, key=self.priorities.__getitem__)
                        if self._priority(hit) >= self.priorities[worst]:
                            continue
                        del self.matches[worst]
                        del self.priorities[worst]
                        self.match_pages.pop(worst, None)
                        self.aliases = {
                            url: old for url, old in self.aliases.items() if old != worst
                        }
                    mid = f"img:{self.next_match}"
                    self.next_match += 1
                    match = ImageMatch(
                        id=mid,
                        image_url=hit.image_url,
                        thumb_url=hit.thumb_url,
                        page_url=hit.page_url,
                        domain=hit.domain,
                        engine=hit.engine,
                        first_seen=hit.first_seen,
                        time_source=hit.time_source,
                        time_receipt_id=self._post_receipt(posted) if posted else None,
                        width=hit.width,
                        height=hit.height,
                        receipt_id=rid,
                    )
                    self.match_pages[mid] = [hit.page_url] if hit.page_url else []
                    match = self._with_page_date(match)  # its page was read for another match
                    self.matches[mid] = match
                    self.priorities[mid] = self._priority(hit)
                    added.append(match)
                self.aliases.update(dict.fromkeys(urls, mid))
            self._sync_matches()
        return added

    def _post_receipt(self, posted) -> int | None:
        """One receipt per X status whose id dates a match (job.lock held; no provider call)."""
        when, url, status_id = posted
        if url not in self.post_receipts:
            self.post_receipts[url] = self.job.receipt(
                "x-id",
                "snowflake",
                f"Image match on X post {status_id}: posted {when:%Y-%m-%d %H:%M:%S} UTC "
                "(time inside the post id, not read from the page)",
                source_url=url,
            )
        return self.post_receipts[url]

    @staticmethod
    def _priority(match, rank=None):
        return (
            match.engine != "tineye",
            match.first_seen or datetime.max.replace(tzinfo=UTC),
            match.rank if rank is None else rank,
        )

    @staticmethod
    def _comparison_summary(match):
        summary = f"Image comparison with the upload: {match.relation}"
        if match.first_seen and match.time_source == "page":
            summary += f"; page dated {_printed_day(match)} (date printed on the page)"
        elif match.first_seen:
            summary += f"; {match.engine} first seen {match.first_seen.isoformat()}"
            if match.time_source == "crawl_date":
                summary += " (crawl date, not publication)"
            elif match.time_source == "page":
                summary += " (date printed on the page)"
            elif match.time_source == "snowflake":
                summary += " (time inside the X post id)"
        return summary

    def _page_candidates(self) -> list[str]:
        """Matched pages to read for their printed date, best match first (_priority: TinEye's
        earliest first, then Yandex's undated ones), each page once, at most
        image_page_dates_max per lookup. X posts are skipped (their id has the exact time), and
        so are bare image files and matches already compared as a different picture."""
        with self.job.lock:
            if not self._active():
                return []
            left = max(0, self.job.deps.image_page_dates_max) - len(self.page_reads)
            picked = []
            for mid in sorted(self.matches, key=self.priorities.__getitem__):
                match = self.matches[mid]
                for url in self.match_pages.get(mid) or [match.page_url]:
                    if left <= len(picked):
                        break
                    if (
                        url in self.page_reads
                        or url in picked
                        or match.relation == "different"
                        or not url
                        or not url.startswith(("http://", "https://"))
                        or match.engine == "x_replies"
                        or url in (match.image_url, match.thumb_url)
                        or domain(url) in _X_DOMAINS
                        or _IMAGE_FILE.search(urlsplit(url).path)
                    ):
                        continue
                    picked.append(url)
            self.page_reads.update(picked)
        return picked

    def _with_page_date(self, match):
        """`match` dated by the earliest printed date of the pages it was found on, when that
        bound is earlier than the one it has; else `match` itself (job.lock held)."""
        got = [
            (*self.page_dates[url], url)
            for url in self.match_pages.get(match.id) or [match.page_url]
            if url in self.page_dates
        ]
        if not got:
            return match
        when, unc, rid, url = min(got, key=lambda d: d[0] + timedelta(seconds=d[1] or 0))
        dated = match.model_copy(
            update={
                # the page shown is the one whose printed date this is
                "page_url": url,
                "domain": domain(url),
                "first_seen": when,
                "time_source": "page",
                "time_uncertainty_s": unc,
                "time_receipt_id": rid,
            }
        )
        if match.first_seen and _bound(dated) >= _bound(match):
            return match  # an earlier bound is already known
        return dated

    def _page_date(self, url):
        """Read the publication date printed on a matched page, as the text lookup reads a
        source page (Oxylabs scrape_facts, service._page_time): a dated bound with its own
        receipt. It replaces the match's date only when its latest possible time is earlier."""
        from lookup.service import (  # service imports this module
            FACTS_CAP_S,
            BudgetExceeded,
            _page_time,
        )

        with self.job.lock:
            if not self._pages_open():
                return None
            try:
                self.job._spend()
            except BudgetExceeded:
                return None  # an optional read: the trace's own steps are not "out of time"
            rid = self.job.receipt(
                "oxylabs", "/scrape", "Matched page read: date not confirmed", source_url=url,
                ok=False,
            )  # fmt: skip
        client = self.job.deps.oxylabs
        try:
            facts = client.scrape_facts(
                url,
                deadline=self.job._provider_deadline(min(FACTS_CAP_S, PAGE_DATE_S)),
            )
            call = dict(client.last_call)
            if facts.unavailable:
                raise ValueError(f"page unavailable: {facts.unavailable}")
            if facts.http_status is not None and facts.http_status >= 400:
                raise ValueError(f"page returned HTTP {facts.http_status}")
        except Exception as exc:
            log.info("image trace %s page date %s failed: %s", self.job.result.id, url, exc)
            self._complete(
                rid,
                "Matched page read: page could not be read, no date",
                ok=False,
                source_url=url,
                call=dict(getattr(client, "last_call", None) or {}),
                gate=self._pages_open,
            )
            return None
        when, unc = _page_time(facts.published_at)
        if when is not None and not (PAGE_DATE_MIN <= when <= datetime.now(UTC)):
            when = None
        final = facts.final_url or url
        moved = not _same_page(url, final)
        # one transaction: a date finish() no longer takes leaves its receipt "not confirmed"
        with self.job.lock:
            if not self._pages_open():
                return None
            return self._settle_page(url, rid, facts, final, when, unc, moved, call)

    def _settle_page(self, url, rid, facts, final, when, unc, moved, call):
        """The page read's receipt and its date for every match on the page (job.lock held)."""
        self._complete(
            rid,
            f"Matched page read: {facts.publisher or domain(final) or 'page'}"
            + (
                f"; the page now redirects to {domain(final)}"
                + ("'s front page" if _host(url) == _host(final) else "")
                + ", so its date is not the matched page's"
                if moved
                else f", published {facts.published_at} (the date printed on the page)"
                if when
                else ", no publication date found"
            ),
            call=call,
            source_url=final,
            gate=self._pages_open,
        )
        if when is None or moved:
            return None
        # every match on this page: the page existed with them by its date
        self.page_dates[url] = (when, unc, rid)
        changed = []
        for mid, match in list(self.matches.items()):
            dated = self._with_page_date(match)
            if dated is match:
                continue
            self.matches[mid] = dated
            if dated.changes_from and self._match_is_older(dated) != (
                dated.changes_from == "match"
            ):
                self._withdraw_description(mid)
            self.priorities[mid] = self._priority(dated, self.priorities[mid][2])
            changed.append(mid)
        if changed:
            self._sync_matches()
        return changed

    def _search(self, engine_view):
        engine, sites = engine_view
        label = "TinEye" if engine == "tineye" else "Yandex sites" if sites else "Yandex imageview"
        request_url = (
            reverse_image.tineye_url(self.query_url)
            if engine == "tineye"
            else reverse_image.yandex_url(self.query_url, sites=sites)
        )
        rid = self._receipt(
            engine, "/scrape", f"{label}: search completion not confirmed", request_url
        )
        if rid is None:
            return None
        try:
            if engine == "tineye":
                search = self.job.deps.search_tineye or reverse_image.search_tineye
                result = search(
                    self.job.deps.oxylabs,
                    self.query_url,
                    deadline=self.job._provider_deadline(reverse_image.TINEYE_CAP_S),
                )
            else:
                search = self.job.deps.search_yandex or reverse_image.search_yandex
                result = search(
                    self.job.deps.oxylabs,
                    self.query_url,
                    sites=sites,
                    deadline=self.job._provider_deadline(reverse_image.YANDEX_CAP_S),
                )
            count = result.total if result.total is not None else len(result.hits)
            summary = f"{label}: {count:,} matches"
            if result.earliest:
                summary += (
                    f", earliest crawl {result.earliest:%Y-%m-%d} "
                    "(when the engine first saw it, not a publish date)"
                )
            self._complete(rid, summary, call=result.call, source_url=result.request_url)
            return result, rid
        except Exception:
            self._complete(
                rid,
                f"{label}: search unavailable",
                ok=False,
                source_url=request_url,
                call=dict(self.job.deps.oxylabs.last_call),
            )
            return None

    def _download(self, raw):
        from lookup.service import UA  # service imports this module

        end = self.job._provider_deadline(DOWNLOAD_S)
        for _hop in range(4):
            rid = self._receipt(
                "fetch", "GET image", "Image download completion not confirmed", raw
            )
            if rid is None:
                return None
            try:
                pinned, authority, hostname = _pinned_url(raw)
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                with httpx.Client(
                    transport=self.job.deps.image_transport,
                    timeout=left,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    with client.stream(
                        "GET",
                        pinned,
                        headers={
                            "Host": authority,
                            "Accept": "image/*",
                            "Accept-Encoding": "identity",
                            # CDNs (Wikimedia) refuse httpx's default python-httpx agent
                            "User-Agent": UA,
                        },
                        extensions={"sni_hostname": hostname},
                    ) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            raw = urljoin(raw, response.headers["location"])
                            self._complete(
                                rid, "Image download redirected; next address checked separately"
                            )
                            continue
                        response.raise_for_status()
                        if (
                            not response.headers.get("content-type", "")
                            .lower()
                            .startswith("image/")
                        ):
                            raise ValueError
                        if (
                            response.headers.get("content-encoding", "identity").lower()
                            != "identity"
                        ):
                            raise ValueError
                        if int(response.headers.get("content-length", 0)) > DOWNLOAD_BYTES:
                            raise ValueError
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            if time.monotonic() >= end or not self._active():
                                raise TimeoutError
                            data.extend(chunk)
                            if len(data) > DOWNLOAD_BYTES:
                                raise ValueError
                        if not data:
                            raise ValueError
                self._complete(rid, f"Downloaded image ({len(data):,} bytes)")
                return bytes(data), rid
            except Exception:
                self._complete(rid, "Image download refused or unavailable", ok=False)
                return None
        return None

    def _next_url(self, mid):
        """The match's next untried URL (full image first, then thumbnail), read under the lock
        so a thumbnail added while this comparison runs is still tried; None marks it failed."""
        with self.job.lock:
            current = self.matches.get(mid)
            tried = self.tried.setdefault(mid, set())
            for url in (current.image_url, current.thumb_url) if current else ():
                if url and url not in tried:
                    tried.add(url)
                    return url
            self.failed.add(mid)
            return None

    def _compare(self, item):
        match, base, relate = item
        while url := self._next_url(match.id):
            fetched = self._download(url)
            if fetched is None:
                continue
            data, rid = fetched
            with self.job.lock:
                self.images[match.id] = _family_copy(data)
            try:
                relation = relate(base, data)
                compared = match.model_copy(deep=True)
                compared.relation = relation.kind
                # plain floats: relate() may hand back numpy numbers
                compared.distance = _num(relation.distance)
                compared.crop_box = _box(relation.crop_box)
                compared.edited_region = _box(relation.edited_region)
                compared.relation_note = _wording(relation.note)
                compared.receipt_id = rid
                compared = ImageMatch.model_validate(compared.model_dump())
                self._complete(rid, "Downloaded image; " + self._comparison_summary(compared))
                self._publish_match(compared)
                return compared, data
            except Exception:
                self._complete(rid, "Downloaded image; visual comparison unavailable")
        return match, None

    def _match_is_older(self, match) -> bool:
        """Which side media.imagediff reads as "original" (`match` is read from self.matches, so a
        date that arrived later counts). A comparison convention, not a chronology: for an
        upload the changes read from the copy to the upload (when someone uploads a picture says
        nothing about when it was made or edited). For a post, the copy goes first only when its
        first-seen upper bound proves it existed before the post; otherwise the post's picture
        does. Neither says who copied whom or which picture was made first."""
        if self.job._upload is not None:
            return True
        with self.job.lock:
            match = self.matches.get(match.id, match)
            subject = next(
                (p for p in self.job.result.posts if p.id == self.job.result.subject_id), None
            )
        posted = subject.created_at if subject else None
        posted = posted or self.job._subject_time
        if not (match.first_seen and posted):
            return False
        return _bound(match) <= posted

    def _withdraw_description(self, mid):
        """A date arrived after the change description was published and flipped which picture
        it starts from: its sentences read the other way, so they are taken back (job.lock)."""
        match = self.matches[mid]
        self.matches[mid] = match.model_copy(
            update={
                "changes": [],
                "change_kinds": [],
                "changes_from": None,
                "changes_withdrawn": True,
                # the match cites its comparison again (its relation and first-seen summary)
                "receipt_id": self.compare_receipts.get(mid, match.receipt_id),
            }
        )
        if match.receipt_id:
            # written here, not through _complete: this runs inside an _accept_hits transaction
            # that was admitted under job.lock (the job can't freeze mid-way), and a deadline
            # passing since then must not leave the wrong-way sentences on an ok receipt
            receipt = self.job.result.receipts[match.receipt_id - 1]
            receipt.ok = False
            receipt.summary = (
                "Image change description withdrawn: a later date for this copy changed which "
                "picture it starts from"
            )

    def _diff(self, item):
        match, data, base, describe = item
        older = self._match_is_older(match)
        original, variant = (data, base) if older else (base, data)
        rid = self._receipt(
            "nemotron",
            "image diff",
            "Image change description completion not confirmed",
            match.image_url,
            DIFF_NEED_S,
        )
        if rid is None:
            return None
        try:
            diff = describe(
                self.job.deps.nemotron,
                original,
                variant,
                timeout_s=max(0.0, self.job._provider_deadline(DIFF_S) - time.monotonic()),
            )
            if diff is None:
                self._complete(rid, "Image change description unavailable", ok=False)
                return None
            updated = match.model_copy(deep=True)
            updated.changes = [_wording(change) for change in diff.changes]
            updated.change_kinds = list(dict.fromkeys(diff.kinds))
            updated.changes_from = "match" if older else "upload"
            with self.job.lock:
                self.compare_receipts.setdefault(match.id, self.matches[match.id].receipt_id)
            updated.receipt_id = rid
            updated = ImageMatch.model_validate(updated.model_dump())
            with self.job.lock:  # re-check and publish as one step: no date slips in between
                if self._match_is_older(match) != older:
                    # a first-seen date arrived while Nemotron read the pair and flipped the
                    # order: its sentences describe the other direction, so they are not shown
                    self._complete(
                        rid,
                        "Image change description not shown: the copy's date arrived during "
                        "the reading and changed which picture it starts from",
                        ok=False,
                        latency_ms=diff.latency_ms,
                    )
                    return None
                self._complete(
                    rid,
                    self._comparison_summary(updated)
                    + (
                        "; observed changes from this copy to the submitted picture: "
                        if older
                        else "; observed changes from the submitted picture to this copy: "
                    )
                    # media.imagediff: [] means Nemotron saw no visible change
                    + ("; ".join(updated.changes) or "Nemotron saw no visible change"),
                    latency_ms=diff.latency_ms,
                )
                self._publish_match(updated)
            return updated
        except Exception:
            self._complete(rid, "Image change description unavailable", ok=False)
            return None

    def run(self):
        from lookup.service import BudgetExceeded

        try:
            self._run()
        except BudgetExceeded:
            self._step("reverse_image", "skipped", "time budget used up")
            self._step("variations", "skipped", "time budget used up")
        except Exception:
            log.warning("image trace %s failed", self.job.result.id, exc_info=True)
            with self.job.lock:
                searching = self.job._step("reverse_image").state == "running"
            if searching:
                self._step("reverse_image", "failed", "Reverse image search unavailable")
            self._step("variations", "failed", "Image comparison unavailable")
        finally:
            with self.job.lock:
                self.closed.set()

    def _run(self):
        self.job._spend()
        self._step("reverse_image", "running")
        searches = ThreadPoolExecutor(max_workers=3, thread_name_prefix="image-trace-search")
        comparisons = ThreadPoolExecutor(max_workers=8, thread_name_prefix="image-trace-compare")
        descriptions = ThreadPoolExecutor(max_workers=4, thread_name_prefix="image-trace-diff")
        # page-date reads: started beside the comparisons, never waited for (see _pages_open)
        pages = ThreadPoolExecutor(
            max_workers=max(1, min(4, self.job.deps.image_page_dates_max)),
            thread_name_prefix="image-trace-page",
        )
        pending = {}
        answered = finished = diffs_started = diffs_done = 0
        initialized = False
        skip = None
        waiting = []
        base = self.job._upload
        describe = None
        try:
            for engine_view in (("tineye", False), ("yandex", False), ("yandex", True)):
                pending[searches.submit(self._search, engine_view)] = "search:" + engine_view[0]
            while pending and self._active():
                done, _ = wait(
                    pending,
                    timeout=max(0.0, self.job._provider_deadline() - time.monotonic()),
                    return_when=FIRST_COMPLETED,
                )
                for future in [future for future in pending if future in done]:
                    stage = pending.pop(future)
                    try:
                        value = future.result()
                    except Exception:
                        value = None
                    if stage.startswith("search"):
                        finished += 1
                        if value is not None:
                            answered += 1
                            waiting.extend(self._accept(*value))
                        if stage == "search:tineye":
                            waiting.extend(self._settle_tineye())
                        if self.tineye_settled:
                            # TinEye's dated matches are known: their pages, and the undated
                            # ones', are read for a printed date beside the comparisons
                            for item in self._page_candidates():
                                self.page_futures.append(pages.submit(self._page_date, item))
                        self._step(
                            "reverse_image",
                            ("done" if answered else "skipped" if self.out_of_time else "failed")
                            if finished == 3
                            else "running",
                            f"{answered}/3 searches answered; {len(self.matches)} candidate images"
                            + ("; time budget used up" if self.out_of_time else ""),
                            self.matches,
                        )
                        if waiting and not initialized:
                            initialized = True
                            try:
                                relate = (
                                    self.job.deps.relate or import_module("media.variants").relate
                                )
                            except ImportError:
                                skip = MISSING
                            try:  # change descriptions are optional: relations stand alone
                                describe = (
                                    self.job.deps.describe_changes
                                    or import_module("media.imagediff").describe_changes
                                )
                            except ImportError:
                                describe = None
                            if not skip:
                                self._step("variations", "running")
                                if base is None:
                                    pending[comparisons.submit(self._download, self.query_url)] = (
                                        "base"
                                    )
                    elif stage == "base":
                        if value is None:
                            skip = (
                                "time budget used up"
                                if self.out_of_time
                                else "The submitted image could not be downloaded"
                            )
                        else:
                            base, _rid = value
                            self.base = base
                    elif stage == "compare" and value is not None:
                        match, data = value
                        if (
                            data is not None
                            and match.id in self.matches
                            and match.relation in ("edited", "cropped")
                            and self.job.deps.nemotron
                            and describe is not None
                            and diffs_started < max(0, self.job.deps.image_diffs_max)
                            and self._active()
                        ):
                            diffs_started += 1
                            item = (self.matches[match.id], data, base, describe)
                            pending[descriptions.submit(self._diff, item)] = "diff"
                    elif stage == "diff" and value is not None:
                        diffs_done += 1
                    if base is not None and not skip and self._active():
                        cap = max(0, self.job.deps.image_matches_max)
                        for match in waiting:
                            with self.job.lock:
                                # the cap counts every match ever queued, so an evicted or
                                # replaced match never frees a download; a retry (new URL for
                                # an admitted match) is already counted
                                admit = match.id in self.matches and (
                                    match.id in self.admitted or len(self.admitted) < cap
                                )
                                if admit:
                                    self.admitted.add(match.id)
                            if admit:
                                pending[
                                    comparisons.submit(self._compare, (match, base, relate))
                                ] = "compare"
                        waiting.clear()
        finally:
            for pool in (searches, comparisons, descriptions):
                pool.shutdown(wait=False, cancel_futures=True)
            pages.shutdown(wait=False)  # running and queued reads finish on their own
        if skip or not initialized:
            self._step("variations", "skipped", skip or "No candidate images to compare")
            return
        with self.job.lock:
            matches = [m for m in self.job.result.image_matches if m.relation != "template"]
            shown = sum(m.changes_from is not None for m in matches)
            compared = len(self.compared)
            dropped = sum(self.matches[mid].relation == "different" for mid in self.compared)
        detail = f"{compared} image{'' if compared == 1 else 's'} compared"
        if dropped:
            detail += f" ({dropped} a different picture, not shown)"
        self._step(
            "variations",
            "done",
            f"{detail}; {shown} change descriptions"
            + ("" if describe is not None else " (not installed on this server)"),
            [match.id for match in matches],
        )

    def finish(self):
        """Close late workers and choose the image origin only after post verification joins."""
        with self.job.lock:
            self.closed.set()
            self.settled.set()  # page dates that landed by now count; later ones are dropped
            for sid in ("reverse_image", "variations"):
                step = self.job._step(sid)
                if step.state in ("pending", "running"):
                    step.state, step.detail = "skipped", "time budget used up"
                    step.ended_at = datetime.now(UTC)
            if not self.query_url:
                return
            result = self.job.result
            own_matches = [m for m in result.image_matches if m.relation != "template"]
            self.job._step("reverse_image").found = [match.id for match in own_matches]
            matches = sorted(
                own_matches,
                key=lambda match: (
                    match.first_seen or datetime.max.replace(tzinfo=UTC),
                    match.id,
                ),
            )
            # only verified posts that may carry the picture bound it: the subject or a copy of
            # it with an image, or any post whose image URL is one of the picture's own copies.
            # Text-only copies, the author's other posts and replies don't date this picture.
            own = {_media_id(self.query_url)} | {
                _media_id(url)
                for match in matches
                if match.relation in ("same", "resized", "cropped")
                for url in (match.image_url, match.thumb_url)
                if url
            }
            posts = [
                post.created_at
                for post in result.posts
                if post.verified
                and post.created_at
                and post.media_urls
                and (post.role in PICTURE_ROLES or own & {_media_id(u) for u in post.media_urls})
            ]
            # the earliest trustworthy time: the copy whose latest possible time (crawl day,
            # page date with its uncertainty, exact post-id time) is earliest, and before every
            # post that may carry the picture
            origins = sorted(
                (
                    match
                    for match in matches
                    if match.relation in ("same", "resized", "cropped")
                    and match.first_seen
                    and match.time_source
                    and all(_bound(match) < created for created in posts)
                ),
                key=lambda match: (_bound(match), match.id),
            )
            result.image_origin_id = origins[0].id if origins else None
            if any(match.time_source == "crawl_date" for match in matches):
                self.job.note(
                    "A crawl date is when the engine first saw the picture, "
                    "not a publication date. "
                    "The picture is at least that old; its true first appearance may be earlier."
                )
            if any(match.time_source == "page" for match in matches):
                self.job.note(
                    "A page date is the publication date printed on the matched page; "
                    "a page edited later can carry a newer picture under an older date."
                )
            if not origins:
                self.job.note(
                    "No copy found that is provably older than the posts; "
                    "the image origin is unknown."
                )
            parent = result.image_origin_id or next(
                (match.id for match in matches if match.relation in ("same", "resized")),
                result.subject_id,
            )
            for match in matches:
                if parent and match.relation in ("edited", "cropped"):
                    self.job.add_edge(
                        parent,
                        match.id,
                        "variant",
                        tool="nemotron" if match.changes else match.engine,
                    )
