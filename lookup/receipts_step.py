"""The lookup's "screenshot" step: find the real post a screenshot in the submitted post shows.

`_media` remembers the submitted post's own images that Nemotron calls screenshots
(`Lookup._shots`). This step reads each one as a post (`NemotronClient.read_screenshot`) and
hands it to `lookup.receipts.find_original`, in parallel on a private pool, bounded by the
provider deadline. The found post joins the graph with role "screenshotted" and an edge
"screenshot_of"; it is never a copy, never in the spread and never the origin.
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from lookup.models import Post, ScreenshotReceipt
from lookup.receipts import Budget, ReceiptDeps, ScreenshotPost, find_original

if TYPE_CHECKING:
    from lookup.service import Lookup

log = logging.getLogger(__name__)

_PLATFORM = {"x": "X", "bluesky": "Bluesky"}
_HANDLE_IN_URL = re.compile(r"https?://(?:x\.com|twitter\.com|bsky\.app/profile)/([^/?#]+)")


CUTOFF_GRACE_S = 1.0  # checks end this long before the step stops waiting for them
READ_NEED_S = 5.0  # a screenshot read (~0.3-2 s with reasoning off) starts only with this left


def run(lk: Lookup) -> None:
    with lk.lock:
        shots = list(lk._shots)[: lk.deps.media_max]
    if not shots:
        media = lk._step("media")
        why = (
            "profile lookup"
            if lk.result.kind in ("x_profile", "bsky_profile")
            else "the post has no images"
            if not lk._media_urls
            else "Nemotron is not configured on this server"
            if not lk.deps.nemotron
            else "no image is a screenshot of a post"
            if media.state == "done"
            else f"the images were not read ({media.detail or media.state})"
        )
        lk.skip("screenshot", why)
        return
    reader = getattr(lk.deps.nemotron, "read_screenshot", None)
    if reader is None:
        lk.skip("screenshot", "screenshot reader not available on this server")
        return
    with lk.step("screenshot") as st:
        anchor = lk._subject_time or datetime.now(UTC)
        sink = _Sink(lk)
        pool = ThreadPoolExecutor(max_workers=len(shots), thread_name_prefix="lookup-shot")
        try:
            futures = [pool.submit(_check, lk, sink, reader, anchor, *s) for s in shots]
            wait(futures, timeout=max(0.0, lk._provider_deadline() - time.monotonic()))
        finally:
            sink.close()  # a check still running can't add receipts to the lookup any more
            pool.shutdown(wait=False, cancel_futures=True)
        parts, found = [], []
        for (url, h, _mime, _data), fut in zip(shots, futures, strict=True):
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                rec = fut.result()
            else:  # still running at the deadline (or, against its contract, raised)
                why = "the check failed" if fut.done() and not fut.cancelled() else None
                rec = _unchecked(h, why or "time budget used up")
                if not fut.done():  # its own receipts can't land now: say what happened
                    rid = lk.receipt(
                        "nemotron",
                        "chat.completions",
                        "Screenshot check: no answer within the time budget",
                        source_url=url,
                        ok=False,
                    )
                    rec.receipt_ids.append(rid)
            if rec is None:  # Nemotron saw no post in it
                continue
            oid = _record(lk, rec, url)
            if oid:
                found.append(oid)
            parts.append(_part(rec))
        st.found = found
        n = len(shots)
        st.detail = (
            f"{len(parts)} screenshot{'s' if len(parts) != 1 else ''}: " + "; ".join(parts)
            if parts
            else f"Nemotron found no post in the {'image' if n == 1 else f'{n} images'}"
        )


class _Sink:
    """lk.receipt until the step stops waiting; afterwards a straggler's receipts are dropped
    (its result is dropped too: the step already said "time budget used up")."""

    def __init__(self, lk: Lookup):
        self._lk, self._open, self._lock = lk, True, threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._open = False

    def __call__(self, *args, **kwargs) -> int:
        with self._lock:
            return self._lk.receipt(*args, **kwargs) if self._open else 0


def _read(reader, media, deadline: float):
    """The screenshot read, given the provider deadline when the reader accepts one."""
    try:
        params = inspect.signature(reader).parameters
    except (TypeError, ValueError):
        params = {}
    if "deadline" in params:
        return reader(media, deadline=deadline)
    return reader(media)


def _check(
    lk: Lookup,
    sink: _Sink,
    reader,
    anchor: datetime,
    url: str,
    h: str,
    mime: str,
    data: bytes | None,
) -> ScreenshotReceipt | None:
    """Read one screenshot and check it. Runs on the private pool; never raises."""
    from lookup.service import IMAGE_DOWNLOAD_S, BudgetExceeded, _download_image
    from media.nemotron import MediaInput

    try:
        lk._spend(READ_NEED_S)  # the read itself is bounded by the deadline passed to it
        if data is None:  # the image's reading came from the cache, without its bytes
            http = lk.deps.http or httpx.Client(timeout=20, follow_redirects=False)
            try:
                data, mime = _download_image(
                    http, url, deadline=lk._provider_deadline(IMAGE_DOWNLOAD_S)
                )
            except httpx.HTTPError as exc:  # a 404 or a dropped connection: not a crash
                log.info("screenshot %s not downloaded again: %s", url, exc)
                data = None
            finally:
                if http is not lk.deps.http:  # made here for this one download
                    http.close()
            if data is None:
                return _unchecked(h, "the screenshot could not be downloaded again")
            lk._spend(READ_NEED_S)
    except BudgetExceeded:
        return _unchecked(h, "time budget used up")
    started = time.monotonic()
    try:
        shot: ScreenshotPost | None = _read(
            reader,
            MediaInput(media_hash=f"lookup:{h}", mime_type=mime, data=data, url=url),
            lk._provider_deadline() - CUTOFF_GRACE_S,
        )
    except Exception as exc:
        log.info("screenshot read %s failed: %s", url, exc)
        timed_out = type(exc).__name__ == "NemotronTimeUp"
        rid = sink(
            "nemotron",
            "chat.completions",
            f"Nemotron could not read the screenshot: {type(exc).__name__}",
            source_url=url,
            ok=False,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        why = "time budget used up" if timed_out else "the screenshot could not be read"
        return _unchecked(h, why, [rid])
    latency = getattr(shot, "latency_ms", None) or int((time.monotonic() - started) * 1000)
    if shot is None:
        sink(
            "nemotron",
            "chat.completions",
            "Nemotron read the screenshot: no social post in it",
            source_url=url,
            latency_ms=latency,
        )
        return None
    where = _PLATFORM.get(shot.platform, shot.platform)
    who = f"@{shot.handle}" if shot.handle else "no legible handle"
    rid = sink(
        "nemotron",
        "chat.completions",
        f"Nemotron read the screenshot: {who} on {where}, '{(shot.text or '')[:80]}'",
        source_url=url,
        latency_ms=latency,
    )
    deps = ReceiptDeps(receipt=sink, oxylabs=lk.deps.oxylabs, bsky=lk.deps.bsky, anchor=anchor)
    try:
        # a little before the step's own cutoff, so every fetch's receipt lands in time
        rec = find_original(shot, deps, Budget(lk._provider_deadline() - CUTOFF_GRACE_S))
    except Exception as exc:  # find_original never raises by contract; keep the step honest
        log.warning("find_original %s failed: %s", url, exc, exc_info=True)
        rec = _unchecked(h, f"the check failed ({type(exc).__name__})")
        rec.platform, rec.handle = shot.platform, shot.handle
    if rid not in rec.receipt_ids:
        rec.receipt_ids.insert(0, rid)
    return rec


def _record(lk: Lookup, rec: ScreenshotReceipt, url: str) -> str | None:
    """Attach the receipt to the lookup; add the real post and its edge when one was found."""
    rec.media_url = url
    rec.post_id = lk.result.subject_id
    with lk.lock:
        lk.result.screenshots.append(rec)
    oid = rec.original_id
    if not oid:
        return None
    lk.add_post(
        Post(
            id=oid,
            platform="x" if oid.startswith("x:") else "bluesky",
            url=rec.original_url,
            author_handle=_handle(rec.original_url) or rec.handle,
            text=rec.original_text or "",
            created_at=rec.original_created_at,
            time_source=rec.original_time_source,
            role="screenshotted",
            metrics=rec.counts_now,
            metrics_as_of=datetime.now(UTC),
            receipt_id=rec.original_receipt_id,
            verified=True,
        )
    )
    with lk.lock:  # whichever step read it first, it is the screenshot's post, never a copy
        for k, q in enumerate(lk.result.posts):
            if q.id == oid and q.role != "submitted":
                lk.result.posts[k] = q.model_copy(update={"role": "screenshotted"})
    lk.add_edge(
        rec.post_id or url,  # the post carrying the screenshot (always known in practice)
        oid,
        "screenshot_of",
        sim=rec.similarity,
        tool="nemotron",
        note=rec.label,
    )
    return oid


def _unchecked(h: str, reason: str, rids: list[int] | None = None) -> ScreenshotReceipt:
    return ScreenshotReceipt(
        media_hash=f"lookup:{h}", label="unchecked", reason=reason, receipt_ids=rids or []
    )


def _handle(url: str | None) -> str | None:
    m = _HANDLE_IN_URL.match(url or "")
    return m.group(1) if m else None


def _part(rec: ScreenshotReceipt) -> str:
    who = f"@{rec.handle}'s" if rec.handle else "the"
    return {
        "matches": f"matches {who} real post",
        "differs": f"differs from {who} real post",
        "not_found": "no matching post found",
    }.get(rec.label, f"unchecked: {rec.reason or 'no reason given'}")
