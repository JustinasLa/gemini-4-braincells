"""Bounded reverse-image discovery and optional visual comparison, independent of OCR."""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
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
MISSING = "variation matching is not installed on this server"
_UNSUITABLE = re.compile(r"\b(?:deepfake\w*|fake\w*|manipulat\w*)\b", re.I)


def _wording(text: str) -> str:
    return _UNSUITABLE.sub("edited", text or "")[:500]


def _pinned_url(raw: str) -> tuple[httpx.URL, str, str]:
    """Validate every DNS answer, then connect to a checked IP, not a second DNS lookup."""
    url = httpx.URL(raw)
    if url.scheme not in ("http", "https") or not url.host or url.userinfo or "%" in url.host:
        raise ValueError("image address is not permitted")
    port = url.port or (443 if url.scheme == "https" else 80)
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
            step.state, step.detail, step.found = state, detail, list(found)
            if state == "running":
                step.started_at = datetime.now(UTC)
            else:
                step.ended_at = datetime.now(UTC)

    def _receipt(self, tool, endpoint, summary, url, need_s=3.0):
        with self.job.lock:
            if not self._active():
                return None
            self.job._spend(need_s)
            return self.job.receipt(tool, endpoint, summary, source_url=url, ok=False)

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
            if self._active():
                self.job.result.image_matches = [
                    match if old.id == match.id else old
                    for old in self.job.result.image_matches
                    if old.id != match.id or match.relation != "different"
                ]

    @staticmethod
    def _comparison_summary(match):
        summary = f"Image comparison with the upload: {match.relation}"
        if match.first_seen:
            summary += f"; {match.engine} first seen {match.first_seen.isoformat()}"
            if match.time_source == "crawl_date":
                summary += " (crawl date, not publication)"
        return summary

    def _parallel(self, function, items, workers):
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="image-trace")
        futures = []
        try:
            futures = [pool.submit(function, item) for item in items]
            wait(futures, timeout=max(0.0, self.job._provider_deadline() - time.monotonic()))
            results = []
            for future in futures:
                if future.done() and not future.cancelled():
                    try:
                        result = future.result()
                    except Exception:
                        continue
                    if result is not None:
                        results.append(result)
            return results
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _search(self, engine_view):
        engine, sites = engine_view
        label = "TinEye" if engine == "tineye" else "Yandex sites" if sites else "Yandex imageview"
        rid = self._receipt(engine, "/scrape", f"{label}: search completion not confirmed", None)
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
            self._complete(rid, f"{label}: search unavailable", ok=False)
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

    def _compare(self, item):
        match, base, relate = item
        for url in dict.fromkeys([match.image_url, match.thumb_url]):
            if not url:
                continue
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
        results = self._parallel(
            self._search, [("tineye", False), ("yandex", False), ("yandex", True)], 3
        )
        candidates = [(hit, rid) for result, rid in results for hit in result.hits]
        candidates.sort(
            key=lambda pair: (
                pair[0].engine != "tineye",
                pair[0].first_seen or datetime.max.replace(tzinfo=UTC),
                pair[0].rank,
            )
        )
        seen, matches = set(), []
        for hit, rid in candidates:
            urls = {url for url in (hit.image_url, hit.thumb_url) if url}
            if seen & urls or len(matches) >= max(0, self.job.deps.image_matches_max):
                continue
            seen.update(urls)
            matches.append(
                ImageMatch(
                    id=f"img:{len(matches) + 1}",
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
            )
        with self.job.lock:
            if not self._active():
                return
            self.job.result.image_matches = matches
        self._step(
            "reverse_image",
            "done" if results else "failed",
            f"{len(results)}/3 searches answered; {len(matches)} candidate images",
            [match.id for match in matches],
        )
        if not matches:
            self._step("variations", "skipped", "No candidate images to compare")
            return
        try:
            relate = self.job.deps.relate or import_module("media.variants").relate
            describe = (
                self.job.deps.describe_changes or import_module("media.imagediff").describe_changes
            )
        except ImportError:
            self._step("variations", "skipped", MISSING)
            return
        self._step("variations", "running")
        base = self.job._upload
        if base is None:
            fetched = self._download(self.query_url)
            if fetched is not None:
                base, _rid = fetched
        if base is None:
            self._step("variations", "skipped", "The submitted image could not be downloaded")
            return
        compared = self._parallel(self._compare, [(match, base, relate) for match in matches], 8)
        by_id = {match.id: match for match, _data in compared}
        matches = [by_id.get(match.id, match) for match in matches]
        matches = [match for match in matches if match.relation != "different"]
        with self.job.lock:
            if not self._active():
                return
            self.job.result.image_matches = matches
        edits = [
            (match, data, base, describe)
            for match, data in compared
            if data is not None and match.relation in ("edited", "cropped")
        ][: max(0, self.job.deps.image_diffs_max)]
        diffs = self._parallel(self._diff, edits, 4) if self.job.deps.nemotron else []
        by_id = {match.id: match for match in diffs}
        with self.job.lock:
            if not self._active():
                return
            self.job.result.image_matches = [by_id.get(match.id, match) for match in matches]
        known = sum(match.relation != "unknown" for match in matches)
        self._step(
            "variations",
            "done",
            f"{known} images compared; {len(diffs)} change descriptions",
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
