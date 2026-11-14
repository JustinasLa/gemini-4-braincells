"""The picture's family tree: where the photograph came from and how it branched into memes.

The image trace (lookup/imagetrace.py) finds copies of the submitted picture and compares each
with it pixel by pixel; a copy with a new caption, a poster frame or a pasted face comes out
"different" there and is not shown. This step asks a looser question of every copy it
downloaded: is the same PHOTOGRAPH in it (media.align, feature matching)? The copies that pass
are the family. Nemotron reads each one (media.memeread: its added text, frame, who the text
mocks), and the earliest clean copy is compared with the others for edits to the photo itself
(a face pasted in). Copies then group by what they add to the photograph:

    milestones (Know Your Meme / Wikipedia, quoted) -> source photo -> branch -> variant
                                                                      -> the submission

The trunk is the template's documented history, kept only when the encyclopedia's own picture
matches the same photograph. Branches are what our search found. Dates are upper bounds (when a
crawler or the Internet Archive first saw a copy), and the tree says what each copy adds, never
who copied whom.
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from importlib import import_module
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from lookup.models import (
    Family,
    FamilyMember,
    FamilyNode,
    FamilyQuote,
    FamilySource,
    FamilyText,
)

log = logging.getLogger(__name__)

STEP = "family"
READS_MAX = 14  # Nemotron reads of copies (the submission is read as well)
DIFFS_MAX = 4  # edit checks of clean copies against the source photo (+1 for the submission)
WAYBACK_MAX = 6
FETCH_S = 6.0
FETCH_BYTES = 8 * 1024 * 1024
READ_S = 8.0
DIFF_S = 12.0
SOURCE_S = 12.0  # an encyclopedia search + page read
STORE_SIDE = 1024  # a copy kept for this step is re-encoded to at most this side
THUMB_SIDE = 480  # the tree's picture of a copy
BRANCH_LABEL = {
    "caption": "Captions",
    "label": "Labels on the scene",
    "poster": "Poster frames",
    "face_swap": "Face swaps",
    "edit": "Photo edits",
    "screenshot": "Screenshots",
}
BRANCH_CHANGE = {
    "caption": "text added over the photo",
    "label": "labels placed on the scene",
    "poster": "framed as a poster with a title",
    "face_swap": "a face replaced",
    "edit": "the photo itself edited",
    "screenshot": "shared inside a screenshot",
}


def _key(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _same_caption(a: str, b: str) -> bool:
    a, b = _key(a), _key(b)
    if not a or not b:
        return a == b
    return a == b or SequenceMatcher(None, a, b).ratio() >= 0.85


SMALL_PX = 300  # media.memeread.SMALL_IMAGE_PX: text read from a smaller copy may be inexact


def _side(data: bytes) -> int:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            return max(image.size)
    except Exception:
        return 0


def _bound(when: datetime | None, source: str | None) -> datetime:
    if when is None:
        return datetime.max.replace(tzinfo=UTC)
    return when + timedelta(days=source == "crawl_date")


def _shrink(data: bytes) -> bytes:
    """A copy kept for the family step: big images re-encoded so 20 of them stay small."""
    if len(data) <= 600_000:
        return data
    try:
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.thumbnail((STORE_SIDE, STORE_SIDE))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=88)
            return out.getvalue()
    except Exception:
        return data


@dataclass
class _Copy:
    id: str
    data: bytes
    match: Any  # ImageMatch, or None for the submission and encyclopedia pictures
    placement: Any = None
    read: Any = None
    crop: bytes | None = None
    edits: list[str] | None = None
    kind: str = "copy"
    first_seen: datetime | None = None
    time_source: str | None = None
    archived_url: str | None = None
    receipt_id: int | None = None
    changes: list[str] | None = None  # the submission: every change from the source photo


def start(job, trace, pool: ThreadPoolExecutor) -> Future:
    """Submit the family step; a finished future when there is no picture to build from."""
    if not trace.query_url:
        job.skip(STEP, "no picture to build a family tree for")
        done: Future = Future()
        done.set_result(None)
        return done
    return pool.submit(Builder(job, trace).run)


class Builder:
    def __init__(self, job, trace):
        self.job = job
        self.trace = trace
        self.lock = threading.Lock()
        self.notes: list[str] = []
        self.sources: list[tuple[Any, FamilySource]] = []  # (OriginEntry, FamilySource)

    # ------------------------------------------------------------ plumbing
    def _active(self) -> bool:
        return self.job._final is None and time.monotonic() < self.job._provider_deadline()

    def _left(self, cap: float) -> float:
        return max(0.0, min(cap, self.job._provider_deadline() - time.monotonic()))

    def _set_step(self, state: str, detail: str | None = None, found=()):
        with self.job.lock:
            if self.job._final is not None:
                return
            step = self.job._step(STEP)
            step.state, step.detail, step.found = state, detail, list(found)
            if state == "running":
                step.started_at = datetime.now(UTC)
            else:
                step.ended_at = datetime.now(UTC)

    def _receipt(self, tool, endpoint, summary, url=None, ok=True, latency_ms=None):
        with self.job.lock:
            if self.job._final is not None:
                return None
            return self.job.receipt(
                tool, endpoint, summary, source_url=url, ok=ok, latency_ms=latency_ms
            )

    def _get(self, url: str, *, accept: str, cap_s: float = FETCH_S) -> bytes | None:
        """GET a public URL (every DNS answer checked, redirects re-checked), or None."""
        from lookup.imagetrace import _pinned_url
        from lookup.service import UA

        end = time.monotonic() + self._left(cap_s)
        for _hop in range(4):
            try:
                pinned, authority, hostname = _pinned_url(url)
                left = end - time.monotonic()
                if left <= 0.2:
                    return None
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
                            "Accept": accept,
                            "Accept-Encoding": "identity",
                            "User-Agent": UA,
                        },
                        extensions={"sni_hostname": hostname},
                    ) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            url = urljoin(url, response.headers["location"])
                            continue
                        if response.status_code != 200:
                            return None
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            data.extend(chunk)
                            if len(data) > FETCH_BYTES or time.monotonic() > end:
                                return None
                        return bytes(data) or None
            except Exception:
                return None
        return None

    def _json(self, url: str, cap_s: float = FETCH_S) -> dict | None:
        raw = self._get(url, accept="application/json", cap_s=cap_s)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    # ------------------------------------------------------------ run
    def run(self):
        from lookup.service import BudgetExceeded

        try:
            self._run()
        except BudgetExceeded:
            self._set_step("skipped", "time budget used up")
        except Exception:
            log.warning("family tree %s failed", self.job.result.id, exc_info=True)
            self._set_step("failed", "Family tree unavailable")

    def _run(self):
        self.job._spend(1.0)
        self._set_step("running")
        workers = ThreadPoolExecutor(max_workers=6, thread_name_prefix="family")
        try:
            self._build(workers)
        finally:
            workers.shutdown(wait=False, cancel_futures=True)

    def _build(self, workers: ThreadPoolExecutor):
        deps = self.job.deps
        nemotron = deps.nemotron
        try:
            align = import_module("media.align")
        except ImportError:
            self._set_step("skipped", "photo matching is not installed on this server")
            return
        if not nemotron:
            self._set_step(
                "skipped", "reading the copies needs Nemotron, not set up on this server"
            )
            return
        memeread = import_module("media.memeread")

        # 1. the submission: read it now (an upload) for the template's name
        base = self.job._upload
        submitted_read: Future | None = None
        sources: Future | None = None
        if base is not None and memeread:
            submitted_read = workers.submit(self._read, memeread, base, "your picture")
            sources = workers.submit(self._sources_after, submitted_read, align, base)

        # 2. the image trace's copies
        wait([self.trace.future], timeout=self._left(60.0))
        base = base if base is not None else getattr(self.trace, "base", None)
        if base is None:
            self._set_step("skipped", "the submitted picture could not be downloaded")
            return
        if submitted_read is None and memeread:
            submitted_read = workers.submit(self._read, memeread, base, "the post's picture")
            sources = workers.submit(self._sources_after, submitted_read, align, base)
        matcher = align.Matcher(base)
        with self.job.lock:
            images = dict(getattr(self.trace, "images", {}))
            matches = {mid: m for mid, m in self.trace.matches.items() if mid in images}
        copies: list[_Copy] = []
        for mid, data in images.items():
            try:
                placement = matcher.locate(data)
            except Exception:
                continue
            if placement is None or not placement.same_photo:
                continue
            match = matches.get(mid)
            copies.append(
                _Copy(
                    id=mid,
                    data=data,
                    match=match,
                    placement=placement,
                    first_seen=match.first_seen if match else None,
                    time_source=match.time_source if match else None,
                )
            )
        checked = len(images)
        copies.sort(key=lambda c: (_bound(c.first_seen, c.time_source), c.id))
        submitted = _Copy(
            id="submitted",
            data=base,
            match=None,
            placement=align.Placement(1000, 1.0, 1.0, 0.0, (0.0, 0.0, 1.0, 1.0), 1.0),
        )

        # 3. read every copy (oldest first: they decide the branches)
        reads = (
            {
                workers.submit(self._read, memeread, c.data, c.match.domain if c.match else c.id): c
                for c in copies[:READS_MAX]
            }
            if memeread
            else {}
        )
        wait(list(reads), timeout=self._left(READ_S + 2))
        for future, copy in reads.items():
            if future.done() and future.result():
                copy.read, copy.receipt_id = future.result()
        if submitted_read is not None:
            wait([submitted_read], timeout=self._left(READ_S))
            if submitted_read.done() and submitted_read.result():
                submitted.read, submitted.receipt_id = submitted_read.result()

        self._drop_scene_text([c for c in copies if c.read] + [submitted])

        # 4. the source photo: the earliest copy with nothing added (largest on a tie)
        for c in copies:
            c.kind = self._kind(c)
        clean = [c for c in copies if c.kind == "copy" and c.read is not None]
        root = min(
            clean,
            key=lambda c: (
                _bound(c.first_seen, c.time_source),
                -((c.match.width or 0) * (c.match.height or 0)) if c.match else 0,
            ),
            default=None,
        )
        if root is not None:
            root.kind = "source"
            self._crop(align, root)
            # 5. edits to the photograph itself: clean-looking copies and the submission
            diffs = [c for c in clean if c is not root][:DIFFS_MAX]
            diffs += [c for c in copies if c.read and c.read.edits and c not in diffs][:1]
            futures = {workers.submit(self._edits, align, root, c): c for c in diffs}
            futures[workers.submit(self._edits, align, root, submitted)] = submitted
            wait(list(futures), timeout=self._left(DIFF_S + 2))
            for c in futures.values():
                if c.edits:
                    joined = " ".join(c.edits).lower()
                    if c.read is not None:
                        c.kind = "face_swap" if "face" in joined else "edit"
        submitted.kind = self._kind(submitted)

        # 6. dates from the Internet Archive for the oldest undated or crawl-dated copies
        dated = [c for c in ([root] if root else []) + copies if c is not None and c.read]
        self._wayback(workers, dated)

        if sources is not None:
            wait([sources], timeout=self._left(SOURCE_S))
        with self.lock:
            have = {s.source for _, s in self.sources}
        if not have and self._active():
            # the image search may have found the encyclopedia's own pages meanwhile
            from sources.memeorigin import kym_entry_url, wikipedia_title

            with self.job.lock:
                urls = [m.page_url for m in self.trace.matches.values() if m.page_url]
            if any(kym_entry_url(u) or wikipedia_title(u) for u in urls):
                self._find_sources("", urls, align, base)
        family = self._assemble(root, copies, submitted)
        family.notes = self.notes
        detail = (
            f"{len([c for c in copies if c.read])} of {checked} downloaded copies hold the same "
            f"photograph · {len([n for n in family.nodes if n.kind == 'variant'])} variants in "
            f"{len([n for n in family.nodes if n.kind == 'branch'])} branches"
        )
        if self.sources:
            detail += " · history from " + " and ".join(s.source for _, s in self.sources)
        with self.job.lock:
            if self.job._final is not None:
                return
            self.job.result.family = family
        self._set_step("done", detail, [m.id for m in family.members if m.id != "submitted"])

    # ------------------------------------------------------------ steps
    def _read(self, memeread, data: bytes, where: str):
        if not self._active():
            return None
        started = time.monotonic()
        read = memeread.read_meme(self.job.deps.nemotron, data, timeout_s=self._left(READ_S))
        if read is None:
            self._receipt("nemotron", "meme read", f"Meme read of {where}: unavailable", ok=False)
            return None
        bits = []
        if read.caption:
            bits.append("text " + " / ".join(f"'{t.text}'" for t in read.text[:4]))
        if read.frame != "none":
            bits.append(f"{read.frame} frame")
        if read.aimed_at:
            bits.append(f"aimed at {read.aimed_at}")
        if read.template:
            bits.append(f"template named: {read.template}")
        rid = self._receipt(
            "nemotron",
            "meme read",
            f"Meme read of {where}: " + ("; ".join(bits) or "no added text, no frame"),
            latency_ms=read.latency_ms or round((time.monotonic() - started) * 1000),
        )
        return read, rid

    def _drop_scene_text(self, reads: list[_Copy]):
        """Text read on many copies from different places is part of the photographed scene
        (a number on a fire truck), not an edit: it is taken out of every read."""
        if len(reads) < 4:
            return
        counts: dict[str, int] = {}
        for c in reads:
            for key in {_key(t.text) for t in c.read.text}:
                counts[key] = counts.get(key, 0) + 1
        scene = {k for k, n in counts.items() if k and n >= 3 and n >= 0.4 * len(reads)}
        if not scene:
            return
        from dataclasses import replace

        for c in reads:
            kept = [t for t in c.read.text if _key(t.text) not in scene]
            if len(kept) != len(c.read.text):
                c.read = replace(c.read, text=kept, aimed_at=c.read.aimed_at if kept else "")
        self.notes.append(
            "Text found on most copies ("
            + ", ".join(f"'{k}'" for k in sorted(scene))
            + ") is treated as part of the photograph, not as an edit."
        )

    @staticmethod
    def _kind(c: _Copy) -> str:
        if c.kind in ("source", "face_swap", "edit"):
            return c.kind
        read = c.read
        if read is None:
            return "copy"
        if read.frame == "screenshot":
            return "screenshot"
        if read.frame == "poster":
            return "poster"
        texts = [t for t in read.text if t.where != "watermark"]
        if not texts:
            return "copy"
        if any(t.where == "on_picture" for t in texts) and not any(
            t.where in ("top", "bottom") for t in texts
        ):
            return "label"
        return "caption"

    def _crop(self, align, c: _Copy) -> bytes | None:
        if c.crop is None:
            try:
                c.crop = align.photo_crop(c.data, c.placement)
            except Exception:
                c.crop = None
        return c.crop

    def _edits(self, align, root: _Copy, c: _Copy):
        """Edits to the photograph (not its added text) between the source photo and `c`."""
        if not self._active() or not self.job.deps.nemotron:
            return
        try:
            relate = self.job.deps.relate or import_module("media.variants").relate
            describe = (
                self.job.deps.describe_changes or import_module("media.imagediff").describe_changes
            )
        except ImportError:
            return
        a, b = self._crop(align, root), self._crop(align, c)
        if not a or not b:
            return
        try:
            relation = relate(a, b)
        except Exception:
            return
        if relation.kind != "edited" and c.id != "submitted":
            return
        diff = describe(self.job.deps.nemotron, a, b, timeout_s=self._left(DIFF_S))
        if diff is None:
            return
        changes = [s for s in diff.changes if s]
        visual = [
            s
            for s in changes
            if re.search(r"\b(face|head|person|figure|object|added|pasted|replaced)\b", s, re.I)
            and not re.search(r"\b(text|caption|word|letter|title|label)\b", s, re.I)
        ]
        kinds = set(diff.kinds)
        if visual and kinds & {"face_changed", "element_added", "element_removed"}:
            c.edits = visual[:3]
        if c.id == "submitted":
            c.changes = changes[:4]
        self._receipt(
            "nemotron",
            "image diff",
            "Source photo compared with "
            + (c.match.domain if c.match and c.match.domain else c.id)
            + ": "
            + ("; ".join(changes) or "no visible change")[:300],
            latency_ms=diff.latency_ms,
        )

    def _wayback(self, workers: ThreadPoolExecutor, copies: list[_Copy]):
        """The Internet Archive's earliest capture of a copy's image (else its page): a dated
        record that it existed, often years before a crawler date."""
        if not self.job.deps.wayback:
            return
        todo = []
        for c in copies:
            if c.match is None or c in todo:
                continue
            todo.append(c)
            if len(todo) >= WAYBACK_MAX:
                break
        futures = {workers.submit(self._wayback_one, c): c for c in todo}
        wait(list(futures), timeout=self._left(FETCH_S + 1))

    def _wayback_one(self, c: _Copy):
        for url, what in ((c.match.image_url, "image"), (c.match.page_url, "page")):
            if not url or "img.tineye.com" in url or "/api/lookup/" in url:
                continue
            query = "https://archive.org/wayback/available?timestamp=19960101&url=" + quote(
                re.sub(r"^https?://", "", url), safe=""
            )
            data = self._json(query, cap_s=FETCH_S)
            snap = ((data or {}).get("archived_snapshots") or {}).get("closest") or {}
            stamp = snap.get("timestamp") or ""
            if not snap.get("available") or not re.fullmatch(r"\d{14}", stamp):
                continue
            when = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            rid = self._receipt(
                "fetch",
                "Wayback Machine",
                f"Internet Archive: earliest capture of the {what} on {when:%Y-%m-%d}",
                url=snap.get("url"),
            )
            if _bound(when, "wayback") < _bound(c.first_seen, c.time_source):
                c.first_seen, c.time_source, c.archived_url = when, "wayback", snap.get("url")
                c.receipt_id = c.receipt_id or rid
            elif c.archived_url is None:
                c.archived_url = snap.get("url")
            return

    # ------------------------------------------------------------ encyclopedia sources
    def _sources_after(self, read_future: Future, align, base: bytes):
        wait([read_future], timeout=self._left(READ_S))
        read = read_future.result() if read_future.done() else None
        name = read[0].template if read else ""
        with self.job.lock:
            urls = [m.page_url for m in self.trace.matches.values() if m.page_url]
        self._find_sources(name, urls, align, base)

    def _find_sources(self, name: str, page_urls: list[str], align, base: bytes):
        origin = import_module("sources.memeorigin")
        matcher = align.Matcher(base)
        kym_found = {u for u in map(origin.kym_entry_url, page_urls) if u}
        wiki_found = {t.replace(" ", "_") for t in map(origin.wikipedia_title, page_urls) if t}
        kym_url = next(iter(sorted(kym_found)), None)
        wiki_title = next(iter(sorted(wiki_found)), None)
        if name and not kym_url and self.job.deps.oxylabs is not None and self._active():
            try:
                self.job._spend(3.0)
                hits = self.job.deps.oxylabs.search(
                    f"{name} meme site:knowyourmeme.com",
                    max_results=5,
                    deadline=self.job._provider_deadline(8.0),
                )
                kym_url = next((u for u in (origin.kym_entry_url(h.url) for h in hits) if u), None)
                self._receipt(
                    "oxylabs",
                    "/search",
                    f"Know Your Meme searched for '{name}': "
                    + (f"entry {kym_url}" if kym_url else "no entry found"),
                )
            except Exception:
                self._receipt("oxylabs", "/search", "Know Your Meme search unavailable", ok=False)
        entries = []
        if kym_url and self.job.deps.oxylabs is not None and self._active():
            try:
                self.job._spend(3.0)
                md, meta = self.job.deps.oxylabs.scrape_markdown(
                    kym_url, deadline=self.job._provider_deadline(10.0)
                )
                # a photo's entry (/memes/<slug>) redirects to its own kind's path
                # (/memes/events/<slug>): cite the page that was read
                page = origin.kym_entry_url((meta or {}).get("url")) or kym_url
                entry = origin.parse_kym(md, page)
                rid = self._receipt(
                    "oxylabs",
                    "/scrape",
                    f"Know Your Meme entry read: {entry.name if entry else 'unreadable'}"
                    + (f", {len(entry.milestones)} dated sentences" if entry else ""),
                    url=kym_url,
                    ok=entry is not None,
                )
                if entry:
                    entries.append((entry, rid))
            except Exception:
                self._receipt(
                    "oxylabs", "/scrape", "Know Your Meme entry unavailable", url=kym_url, ok=False
                )
        title = wiki_title or name
        if title and self.job.deps.wikipedia and self._active():
            summary_url, extract_url = origin.wikipedia_urls(title)
            summary = self._json(summary_url)
            extract = self._json(extract_url) if summary else None
            entry = origin.parse_wikipedia(summary or {}, extract or {}) if summary else None
            rid = self._receipt(
                "fetch",
                "Wikipedia",
                f"Wikipedia article read: {entry.name}, {len(entry.milestones)} dated sentences"
                if entry
                else f"Wikipedia: no article '{title}'",
                url=entry.url if entry else summary_url,
                ok=entry is not None,
            )
            if entry:
                entries.append((entry, rid))
        for entry, rid in entries:
            verified = None
            if entry.image_url:
                data = self._get(entry.image_url, accept="image/*")
                try:
                    verified = matcher.locate(data) if data else None
                except Exception:
                    verified = None
            # the image search found this entry's own pages (the entry or one of its photos)
            found_by_search = (
                kym_url in kym_found
                if entry.source == "Know Your Meme"
                else entry.url.rsplit("/", 1)[-1] in wiki_found
            )
            if (verified is None or not verified.same_photo) and not found_by_search:
                self.notes.append(
                    f"{entry.source} has an entry named '{entry.name}', but its picture is not "
                    "this photograph, so its history is not used."
                )
                continue
            with self.lock:
                self.sources.append(
                    (
                        entry,
                        FamilySource(
                            source=entry.source,
                            name=entry.name,
                            url=entry.url,
                            image_url=entry.image_url,
                            summary=entry.summary,
                            added=entry.added,
                            inliers=verified.inliers if verified and verified.same_photo else None,
                            receipt_id=rid,
                        ),
                    )
                )

    # ------------------------------------------------------------ the tree
    def _member(self, c: _Copy) -> FamilyMember:
        m = c.match
        read = c.read
        return FamilyMember(
            id=c.id,
            image_url=m.image_url if m else None,
            thumb_url=m.thumb_url if m else None,
            page_url=m.page_url if m else None,
            domain=m.domain if m else None,
            engine=m.engine if m else None,
            first_seen=c.first_seen,
            time_source=c.time_source,
            archived_url=c.archived_url,
            inliers=c.placement.inliers if c.placement and c.id != "submitted" else None,
            coverage=c.placement.coverage if c.placement and c.id != "submitted" else None,
            relation=m.relation if m else None,
            text=[FamilyText(text=t.text, where=t.where) for t in (read.text if read else [])],
            frame=read.frame if read else "none",
            aimed_at=read.aimed_at if read else "",
            edits=c.edits or [],
            kind=c.kind if c.kind != "submitted" else "copy",
            small=_side(c.data) < SMALL_PX,
            picture=self._picture(c),
            receipt_id=c.receipt_id or (m.receipt_id if m else None),
        )

    def _picture(self, c: _Copy) -> str | None:
        """Keep a small JPEG of the copy for the tree (served by the API while the job lives)."""
        if c.id == "submitted":
            return None
        try:
            from PIL import Image, ImageOps

            with Image.open(io.BytesIO(c.data)) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((THUMB_SIDE, THUMB_SIDE))
                out = io.BytesIO()
                image.save(out, "JPEG", quality=84)
        except Exception:
            return None
        store = getattr(self.job, "family_images", None)
        if store is None:
            return None
        with self.job.lock:
            store[c.id] = out.getvalue()
        return f"/api/lookup/{self.job.result.id}/family/{quote(c.id, safe='')}"

    @staticmethod
    def _date(c: _Copy) -> str | None:
        return c.first_seen.date().isoformat() if c.first_seen else None

    def _assemble(self, root: _Copy | None, copies: list[_Copy], submitted: _Copy) -> Family:
        from sources.memeorigin import merge_milestones

        members = [c for c in copies if c.read is not None]
        nodes: list[FamilyNode] = []
        events: list[FamilyNode] = []
        root_date = self._date(root) if root else None

        # the trunk: the sources' dated sentences up to the source photo's own date
        with self.lock:
            sources = list(self.sources)
        milestones = merge_milestones([entry for entry, _ in sources])
        parent = None
        for i, m in enumerate(milestones):
            node = FamilyNode(
                id=f"m:{i + 1}",
                kind="milestone",
                label=m["quotes"][0]["text"],
                date=m["date"],
                time_source="source",
                quotes=[FamilyQuote(**q) for q in m["quotes"]],
                receipt_id=next(
                    (s.receipt_id for _, s in sources if s.source == m["quotes"][0]["source"]),
                    None,
                ),
            )
            if root_date is None or m["date"] <= root_date:
                node.parent = parent
                nodes.append(node)
                parent = node.id
            else:
                events.append(node)

        clean = [c for c in members if c.kind in ("source", "copy")]
        source = FamilyNode(
            id="source",
            parent=parent,
            kind="source",
            label="Source photo" if root else "Source photo (no clean copy found)",
            members=[
                c.id
                for c in sorted(
                    clean, key=lambda c: (c is not root, _bound(c.first_seen, c.time_source))
                )
            ],
            date=root_date,
            time_source=root.time_source if root else None,
            change="earliest copy found with nothing added" if root else None,
            receipt_id=root.receipt_id if root else None,
        )
        nodes.append(source)

        # branches and their variants (copies with the same text are one variant)
        variants: dict[str, list[list[_Copy]]] = {}
        for c in members:
            if c.kind in ("source", "copy"):
                continue
            groups = variants.setdefault(c.kind, [])
            caption = c.read.caption if c.read else ""
            group = next(
                (
                    g
                    for g in groups
                    if c.kind not in ("face_swap", "edit")
                    and _same_caption(g[0].read.caption, caption)
                ),
                None,
            )
            if group:
                group.append(c)
            else:
                groups.append([c])
        submitted_text = submitted.read.caption if submitted.read else ""
        sub_kind = submitted.kind
        if sub_kind in ("copy", "source") and submitted_text:
            sub_kind = "caption"
        branch_ids: dict[str, str] = {}

        def branch(kind: str, date: str | None) -> str:
            if kind not in branch_ids:
                node = FamilyNode(
                    id=f"b:{kind}",
                    parent="source",
                    kind="branch",
                    branch=kind,
                    label=BRANCH_LABEL[kind],
                    date=date,
                    change=BRANCH_CHANGE[kind],
                )
                nodes.append(node)
                branch_ids[kind] = node.id
            return branch_ids[kind]

        order = sorted(
            variants.items(),
            key=lambda kv: min(_bound(c.first_seen, c.time_source) for g in kv[1] for c in g),
        )
        variant_of_submission: str | None = None
        n = 0
        for kind, groups in order:
            groups.sort(key=lambda g: min(_bound(c.first_seen, c.time_source) for c in g))
            for g in groups:
                g.sort(key=lambda c: _bound(c.first_seen, c.time_source))
            bid = branch(kind, self._date(groups[0][0]))
            for g in groups:
                n += 1
                first = g[0]
                caption = first.read.caption if first.read else ""
                label = (
                    first.edits[0]
                    if kind in ("face_swap", "edit") and first.edits
                    else " / ".join(t.text for t in first.read.text if t.where != "watermark")[:80]
                    if first.read and first.read.text
                    else BRANCH_LABEL[kind]
                )
                node = FamilyNode(
                    id=f"v:{n}",
                    parent=bid,
                    kind="variant",
                    branch=kind,
                    label=label or BRANCH_LABEL[kind],
                    caption=caption or None,
                    members=[c.id for c in g],
                    date=self._date(first),
                    time_source=first.time_source,
                    aimed_at=next((c.read.aimed_at for c in g if c.read and c.read.aimed_at), None),
                    change=(
                        "; ".join(first.edits[:2])
                        if first.edits
                        else f"text: {' / '.join(repr(t.text) for t in first.read.text[:3])}"
                        if first.read and first.read.text
                        else BRANCH_CHANGE[kind]
                    ),
                    receipt_id=first.receipt_id,
                )
                nodes.append(node)
                if (
                    variant_of_submission is None
                    and kind == sub_kind
                    and submitted_text
                    and _same_caption(caption, submitted_text)
                ):
                    variant_of_submission = node.id

        # the submission: a copy of a known variant, or a new tip on its branch
        changes = submitted.changes or []
        if sub_kind in ("copy", "source") and not submitted.edits:
            sub_parent, new, change = "source", False, "the same photograph, nothing added"
        elif variant_of_submission:
            first = next(n for n in nodes if n.id == variant_of_submission)
            sub_parent, new = variant_of_submission, False
            change = "same text as this copy" + (f", seen by {first.date}" if first.date else "")
        else:
            sub_parent, new = branch(sub_kind, None), True
            if submitted.edits:
                change = "; ".join(submitted.edits[:2])
            elif changes:
                change = " ".join(changes[:2])
            else:
                change = (
                    "new text: " + " / ".join(repr(t.text) for t in submitted.read.text[:3])
                    if submitted.read and submitted.read.text
                    else BRANCH_CHANGE.get(sub_kind, "changed")
                )
        nodes.append(
            FamilyNode(
                id="submitted",
                parent=sub_parent,
                kind="submitted",
                branch=sub_kind if sub_kind not in ("copy", "source") else None,
                label=(
                    " / ".join(t.text for t in submitted.read.text if t.where != "watermark")[:80]
                    if submitted.read and submitted.read.text
                    else "Your picture"
                ),
                caption=submitted_text or None,
                members=["submitted"],
                aimed_at=(submitted.read.aimed_at or None) if submitted.read else None,
                change=change,
                new=new,
                receipt_id=submitted.receipt_id,
            )
        )
        if new and submitted_text:
            self.notes.append(
                "Your picture's text is on none of the copies we found: it is a new branch tip "
                "(our search is a sample, so it may exist elsewhere)."
            )
        by_id = {n.id: n for n in nodes}
        lineage, cur = [], "submitted"
        while cur:
            lineage.append(cur)
            cur = by_id[cur].parent
        submitted.kind = sub_kind if sub_kind not in ("copy", "source") else "copy"
        member_models = [self._member(c) for c in members] + [self._member(submitted)]
        template = next((s.name for _, s in sources), None)
        return Family(
            template=template,
            sources=[s for _, s in sources],
            members=member_models,
            nodes=nodes,
            lineage=list(reversed(lineage)),
            events=events,
        )
