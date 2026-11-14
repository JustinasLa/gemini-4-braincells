"""Same template, other content: find pictures in X reply threads that show the same page, app
screen or meme template as the traced picture, with other names or text in it.

People answer a screenshot with their own screenshot of the same page (a petition's signer
search showing another name; a meme template with another caption). Reverse image search and
perceptual hashes look for the same picture, so they miss these, and replies are not indexed by
search engines at all. This step reads the threads where they sit:

1. the threads: the submitted X post's own, or for an upload the X posts its text and site
   searches found (at most THREADS; FxTwitter /2/conversation, one page of ~30 replies each,
   the submitted post's two pages);
2. their replies' photos (at most CANDIDATES, deduplicated, not the traced picture itself);
3. Nemotron sees the traced picture beside each of them and says whether it shows the same
   template and what differs (media/templatematch.py), all at once.

A match is an `image_matches` entry with relation "template" and engine "x_replies": a picture
of the same kind, never a copy of this one, so it is never the image origin and never a carrier
of the text. Each comes with its reply's URL and exact time (snowflake) and its receipt.
"""

from __future__ import annotations

import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from importlib import import_module
from typing import TYPE_CHECKING, Any

import httpx

from lookup.fxtwitter import Conversation
from lookup.models import ImageMatch
from lookup.urls import snowflake_time, x_post_url

if TYPE_CHECKING:
    from lookup.service import Lookup

log = logging.getLogger(__name__)

THREADS = 6  # conversations read (in parallel, ~0.5 s each)
SUBJECT_PAGES = 2  # reply pages of the submitted post's own thread
CANDIDATES = 24  # reply photos compared
DOWNLOAD_S = 6.0
COMPARE_S = 15.0  # one Nemotron comparison (p50 ~0.4 s live)
COMPARE_WORKERS = 8
ENGINE = "x_replies"
_IMAGE_HOST = "pbs.twimg.com"


def _media_key(url: str) -> str:
    """One key per X photo, whatever its size/format suffix."""
    try:
        path = httpx.URL(url).path
    except Exception:
        return url
    return path.rsplit("/", 1)[-1].split(".", 1)[0]


def _small(url: str) -> str:
    """The 'small' rendition (680 px): enough to read a screenshot, a fraction of the bytes."""
    u = httpx.URL(url)
    return str(u.copy_with(params={"format": u.params.get("format") or "jpg", "name": "small"}))


def _order(m: ImageMatch):
    return (m.first_seen is None, m.first_seen, m.id)


def _failed(why: str) -> Exception:
    from lookup.service import StepUnavailable

    return StepUnavailable(why, "failed")


def _skip(why: str) -> Exception:
    """The step can't run here: shown as skipped with this reason."""
    from lookup.service import StepUnavailable

    return StepUnavailable(why, "skipped")


class ThreadScan:
    def __init__(self, job: Lookup, reference: tuple[bytes | None, str | None]):
        self.job = job
        self.ref_bytes, self.ref_url = reference
        self.matcher = (
            job.deps.match_templates or import_module("media.templatematch").same_template
        )
        # the scan's state, written under job.lock; once `closed` (close(): the lookup's end
        # or the scan's own) nothing of the scan changes the result any more
        self.closed = False
        self.closed_clean = True  # closed by the scan itself (not cut off by the lookup's end)
        self.threads_read = 0
        self.unread = 0  # reply pages that could not be read
        self.pictures = 0  # reply pictures sent to comparison
        self.compared = 0
        self.failed_cmp = 0
        self.unchanged = 0
        self.found: list[ImageMatch] = []
        self.rid: int | None = None  # the comparison's receipt

    # ------------------------------------------------------------ threads
    def _threads(self, roots: list[str]) -> tuple[list[tuple[str, dict, Conversation]], list]:
        """Read each root's conversation (and the subject's second page), in parallel.
        Returns (pages read, [(root id, why) for each thread that could not be read])."""
        job = self.job

        def read(sid: str, pages: int):
            got, cursor = [], None
            for _ in range(pages):
                conv, call, gone = job._fx_read(sid, replies=True, cursor=cursor)
                if conv is None:
                    why = "FxTwitter says the post is gone" if gone else "FxTwitter did not answer"
                    if got:
                        why = f"reply page {len(got) + 1}: {why}"
                    return got, (sid, why, call)
                got.append((sid, call, conv))
                cursor = conv.cursor
                if not cursor:
                    break
            return got, None

        subj = job.result.subject_id
        out: list[tuple[str, dict, Conversation]] = []
        failed: list[tuple[str, str, dict]] = []
        with ThreadPoolExecutor(max_workers=len(roots) or 1) as pool:
            futs = {
                pool.submit(read, sid, SUBJECT_PAGES if f"x:{sid}" == subj else 1): sid
                for sid in roots
            }
            for f, sid in futs.items():
                try:
                    got, fail = f.result()
                except Exception as exc:  # BudgetExceeded or a client bug: that thread only
                    log.info("lookup %s: thread read failed: %s", job.result.id, exc)
                    got, fail = [], (sid, f"not read ({type(exc).__name__})", {})
                out += got
                if fail:
                    failed.append(fail)
        return out, failed

    # ------------------------------------------------------------ run
    def _receipt(self, *args, **kw) -> int | None:
        with self.job.lock:
            return None if self.closed else self.job.receipt(*args, **kw)

    def run(self, roots: list[str]) -> None:
        job = self.job
        with job.step("thread_variants"):
            if job.deps.fxtwitter is None:
                raise _skip("FxTwitter is not configured on this server")
            if not roots:
                raise _skip("no X thread to read")
            ref = self.ref_bytes or self._download(self.ref_url)
            if not ref:
                raise _skip("the traced picture could not be downloaded")
            threads, failed = self._threads(roots[:THREADS])
            for sid, why, call in failed:  # missing evidence is recorded, not hidden
                self._receipt(
                    "fxtwitter",
                    "/2/conversation",
                    f"Replies to post {sid} could not be read: {why}",
                    source_url=f"https://x.com/i/status/{sid}",
                    call=call,
                    ok=False,
                )
            if not threads and failed:
                raise _failed(f"no reply thread could be read ({len(failed)} tried)")
            with job.lock:
                self.threads_read, self.unread = len(threads), len(failed)
            ref_key = _media_key(self.ref_url) if self.ref_url else None
            seen: set[str] = {ref_key} if ref_key else set()
            cands: list[tuple[Any, str, str]] = []  # (reply XItem, image url, thread root id)
            for sid, call, conv in threads:
                n_photos = 0
                for r in conv.replies:
                    for url in r.media_urls:
                        key = _media_key(url)
                        if key in seen or httpx.URL(url).host != _IMAGE_HOST:
                            continue
                        seen.add(key)
                        n_photos += 1
                        if len(cands) < CANDIDATES:
                            cands.append((r, url, sid))
                self._receipt(
                    "fxtwitter",
                    "/2/conversation",
                    f"Replies to @{conv.main.handle}'s post read through FxTwitter: "
                    f"{len(conv.replies)} replies, {n_photos} new reply pictures",
                    source_url=x_post_url(conv.main.handle, sid),
                    call=call,
                )
            if cands:
                images = self._download_all([url for _, url, _ in cands])
                usable = [(c, b) for c, b in zip(cands, images, strict=True) if b]
                self._compare(ref, usable)
            self.close()

    def _detail(self) -> str:
        parts = [f"{self.threads_read} reply pages read"]
        if self.rid is None:
            parts.append(
                "no reply pictures to compare"
                if self.closed_clean
                else "time budget used up before the comparison"
            )
        else:
            late = self.pictures - self.compared - self.failed_cmp
            parts.append(f"{self.compared} reply pictures compared")
            parts.append(f"{len(self.found)} show the same template with other content")
            if late:
                parts.append(f"{late} not compared in time")
        if self.unread:
            parts.append(f"{self.unread} reply pages could not be read")
        return " · ".join(parts)

    def close(self, *, at_end: bool = False) -> None:
        """Settle the step and the comparison receipt once: from the scan itself when it is
        done, or from the lookup before its reading (`at_end`), whichever comes first. Calls
        still running after that change nothing (the reading and the frozen result agree)."""
        job = self.job
        with job.lock:
            if self.closed:
                return
            self.closed_clean = not at_end
            self.closed = True
            if self.rid is not None:
                late = self.pictures - self.compared - self.failed_cmp
                rec = job.result.receipts[self.rid - 1]
                rec.summary = (
                    f"Nemotron compared {self.compared} reply pictures with the traced one, one "
                    f"by one: {len(self.found)} show the same template with other content"
                    + (f"; {self.unchanged} looked unchanged, not counted" * bool(self.unchanged))
                    + (f"; {self.failed_cmp} comparisons failed" if self.failed_cmp else "")
                    + (f"; {late} not answered in time" if late else "")
                )[:400]
                rec.ok = self.compared > 0
            st = job._step("thread_variants")
            if st.state != "running":  # skipped or failed by the step itself
                return
            st.found = [m.id for m in sorted(self.found, key=_order)]
            st.state, st.detail, st.ended_at = "done", self._detail(), datetime.now(UTC)

    # ------------------------------------------------------------ images
    def _download(self, url: str | None) -> bytes | None:
        if not url or httpx.URL(url).host != _IMAGE_HOST:
            return None
        job = self.job
        from lookup.service import _download_image

        deadline = min(time.monotonic() + DOWNLOAD_S, job._provider_deadline())
        try:
            with httpx.Client(transport=job.deps.image_transport, follow_redirects=False) as http:
                data, _mime = _download_image(http, _small(url), deadline)
            return data
        except Exception as exc:
            log.info("lookup %s: reply image download failed: %s", job.result.id, exc)
            return None

    def _download_all(self, urls: list[str]) -> list[bytes | None]:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(self._download, urls))

    # ------------------------------------------------------------ compare
    def _compare(self, ref: bytes, usable: list) -> None:
        """Each reply picture beside the traced one, one call each, all at once (~0.5 s for a
        thread's worth on Verda). Matches are published as they come in, so a slow call never
        holds back one that is done; the wait ends at the provider deadline and nothing waits
        for late calls after it. One receipt for the whole comparison, settled by close()."""
        job = self.job
        client = job.deps.nemotron
        if client is None:
            raise _skip("no vision model on this server to compare the pictures")

        def one(data: bytes):
            left = job._provider_deadline() - time.monotonic()
            if left < 2.0:
                return None
            return self.matcher(client, ref, data, timeout_s=min(COMPARE_S, left))

        with job.lock:
            if self.closed:
                return
            self.pictures = len(usable)
            self.rid = job.receipt(
                "nemotron",
                "chat.completions",
                f"Nemotron is comparing {len(usable)} reply pictures with the traced one",
            )
        pool = ThreadPoolExecutor(max_workers=COMPARE_WORKERS)
        futs = {pool.submit(one, data): (c, data) for c, data in usable}
        wait_s = max(0.0, job._provider_deadline() - time.monotonic())
        try:
            for fut in as_completed(futs, timeout=wait_s):
                (reply, url, root), data = futs[fut]
                try:
                    res = fut.result()
                except Exception:
                    res = None
                with job.lock:
                    if self.closed:
                        break
                    if res is None:
                        self.failed_cmp += 1
                        continue
                    v, ms, _model = res
                    self.compared += 1
                    rec = job.result.receipts[self.rid - 1]
                    rec.latency_ms = max(rec.latency_ms or 0, ms)
                    if not v.same_template:
                        continue
                    if not v.differs:
                        # the same layout with nothing different is a copy of the picture, not
                        # another version of it (reverse image search finds copies)
                        self.unchanged += 1
                        continue
                    self.found.append(self._match(reply, url, root, data, v.differs, self.rid))
                    job._publish_templates(sorted(self.found, key=_order))
        except TimeoutError:  # concurrent.futures' is the builtin since 3.11
            pass  # the provider deadline: what came back is kept, the rest is counted as late
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _match(self, reply, url: str, root: str, data: bytes, differs: str, rid: int):
        when = snowflake_time(reply.status_id)
        note = f"Reply by @{reply.handle}: same template, other content"
        return ImageMatch(
            id=f"xr:{reply.status_id}:{hashlib.sha256(data).hexdigest()[:8]}",
            image_url=url,
            thumb_url=_small(url),
            page_url=x_post_url(reply.handle, reply.status_id),
            domain=f"x.com/{reply.handle}",
            engine=ENGINE,
            first_seen=when,
            time_source="snowflake" if when else None,
            relation="template",
            relation_note=(f"{note}: {differs}" if differs else note)[:300],
            changes=[differs] if differs else [],
            changes_from="upload" if differs else None,
            receipt_id=rid,
        )
