"""Bounded reverse-image discovery and optional visual comparison, independent of OCR."""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime, timedelta
from importlib import import_module
from urllib.parse import urljoin

import httpx

from lookup.models import ImageMatch
from sources import reverse_image

DOWNLOAD_S = 5.0
DOWNLOAD_BYTES = 8 * 1024 * 1024
DIFF_S = 20.0
DIFF_NEED_S = DIFF_S + 3.0
# slots kept for TinEye (one result page; at most half the cap): only its dated matches can be
# the image origin, so faster Yandex hits never fill the whole cap before TinEye answers
TINEYE_RESERVE = 10
MISSING = "variation matching is not installed on this server"
_UNSUITABLE = re.compile(
    r"\b(?:deepfakes?|fake[ds]?|manipulated|manipulation|doctored|doctoring)\b", re.I
)


def _wording(text: str) -> str:
    return _UNSUITABLE.sub("edited", text or "")[:500]


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
        # ids queued for download + comparison: never evicted, never more than the cap in all
        self.admitted: set[str] = set()
        self.tried: dict[str, set[str]] = {}  # match id -> URLs already downloaded or refused
        self.failed: set[str] = set()  # admitted, every URL tried, no comparison
        self.compared: set[str] = set()  # comparisons published (incl. "different", dropped)
        self.tineye_settled = False  # TinEye answered or failed: its reserve is open to Yandex
        self.overflow: list[tuple] = []  # (hit, rid) Yandex hits held back for the reserve

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
            self.job._spend(need_s)
            return self.job.receipt(tool, endpoint, _wording(summary), source_url=url, ok=False)

    def _complete(self, rid, summary, *, ok=True, call=None, source_url=None, latency_ms=None):
        with self.job.lock:
            if not self._active():
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
                    "receipt_id",
                }
                self.matches[match.id] = self.matches[match.id].model_copy(
                    update=match.model_dump(include=fields)
                )
                if match.relation != "unknown":
                    self.compared.add(match.id)
                self._sync_matches()

    def _sync_matches(self):
        self.job.result.image_matches = [
            self.matches[mid]
            for mid in sorted(self.matches, key=self.priorities.__getitem__)
            if self.matches[mid].relation != "different"
        ]

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
                        and (not match.first_seen or hit.first_seen < match.first_seen)
                    ):
                        match.first_seen, match.time_source = hit.first_seen, hit.time_source
                        match.engine = hit.engine
                    elif not match.first_seen and hit.engine == "tineye":
                        match.engine = hit.engine
                    self.matches[mid] = match
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
                        width=hit.width,
                        height=hit.height,
                        receipt_id=rid,
                    )
                    self.matches[mid] = match
                    self.priorities[mid] = self._priority(hit)
                    added.append(match)
                self.aliases.update(dict.fromkeys(urls, mid))
            self._sync_matches()
        return added

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
        if match.first_seen:
            summary += f"; {match.engine} first seen {match.first_seen.isoformat()}"
            if match.time_source == "crawl_date":
                summary += " (crawl date, not publication)"
        return summary

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
            try:
                relation = relate(base, data)
                compared = match.model_copy(deep=True)
                compared.relation = relation.kind
                compared.distance = relation.distance
                compared.crop_box = relation.crop_box
                compared.edited_region = relation.edited_region
                compared.relation_note = _wording(relation.note)
                compared.receipt_id = rid
                compared = ImageMatch.model_validate(compared.model_dump())
                self._complete(rid, "Downloaded image; " + self._comparison_summary(compared))
                self._publish_match(compared)
                return compared, data
            except Exception:
                self._complete(rid, "Downloaded image; visual comparison unavailable")
        return match, None

    def _diff(self, item):
        match, data, base, describe = item
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
                base,
                data,
                timeout_s=max(0.0, self.job._provider_deadline(DIFF_S) - time.monotonic()),
            )
            if diff is None:
                self._complete(rid, "Image change description unavailable", ok=False)
                return None
            updated = match.model_copy(deep=True)
            updated.changes = [_wording(change) for change in diff.changes]
            updated.change_kinds = diff.kinds
            updated.receipt_id = rid
            updated = ImageMatch.model_validate(updated.model_dump())
            self._complete(
                rid,
                self._comparison_summary(updated)
                + "; observed changes: "
                + ("; ".join(updated.changes) or "none described"),
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
        pending = {}
        answered = finished = diffs_started = diffs_done = 0
        initialized = False
        skip = None
        waiting = []
        base = self.job._upload
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
                        self._step(
                            "reverse_image",
                            ("done" if answered else "failed") if finished == 3 else "running",
                            f"{answered}/3 searches answered; {len(self.matches)} candidate images",
                            self.matches,
                        )
                        if waiting and not initialized:
                            initialized = True
                            try:
                                relate = (
                                    self.job.deps.relate or import_module("media.variants").relate
                                )
                                describe = (
                                    self.job.deps.describe_changes
                                    or import_module("media.imagediff").describe_changes
                                )
                            except ImportError:
                                skip = MISSING
                            if not skip:
                                self._step("variations", "running")
                                if base is None:
                                    pending[comparisons.submit(self._download, self.query_url)] = (
                                        "base"
                                    )
                    elif stage == "base":
                        if value is None:
                            skip = "The submitted image could not be downloaded"
                        else:
                            base, _rid = value
                    elif stage == "compare" and value is not None:
                        match, data = value
                        if (
                            data is not None
                            and match.id in self.matches
                            and match.relation in ("edited", "cropped")
                            and self.job.deps.nemotron
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
        if skip or not initialized:
            self._step("variations", "skipped", skip or "No candidate images to compare")
            return
        with self.job.lock:
            matches = list(self.job.result.image_matches)
            compared = len(self.compared)
            dropped = sum(self.matches[mid].relation == "different" for mid in self.compared)
        detail = f"{compared} image{'' if compared == 1 else 's'} compared"
        if dropped:
            detail += f" ({dropped} a different picture, not shown)"
        self._step(
            "variations",
            "done",
            f"{detail}; {diffs_done} change descriptions",
            [match.id for match in matches],
        )

    def finish(self):
        """Close late workers and choose the image origin only after post verification joins."""
        with self.job.lock:
            self.closed.set()
            for sid in ("reverse_image", "variations"):
                step = self.job._step(sid)
                if step.state in ("pending", "running"):
                    step.state, step.detail = "skipped", "time budget used up"
                    step.ended_at = datetime.now(UTC)
            if not self.query_url:
                return
            result = self.job.result
            self.job._step("reverse_image").found = [match.id for match in result.image_matches]
            matches = sorted(
                result.image_matches,
                key=lambda match: (
                    match.first_seen or datetime.max.replace(tzinfo=UTC),
                    match.id,
                ),
            )
            posts = [post.created_at for post in result.posts if post.verified and post.created_at]
            origins = [
                match
                for match in matches
                if match.relation in ("same", "resized", "cropped")
                and match.first_seen
                and match.time_source
                and all(
                    match.first_seen + timedelta(days=match.time_source == "crawl_date") < created
                    for created in posts
                )
            ]
            result.image_origin_id = origins[0].id if origins else None
            if any(match.time_source == "crawl_date" for match in matches):
                self.job.note(
                    "A crawl date is when the engine first saw the picture, "
                    "not a publication date. "
                    "The picture is at least that old; its true first appearance may be earlier."
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
