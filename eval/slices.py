"""Recorded Jetstream slices -> detector windows, in memory (no database).

The live recorder (`ingest --record`) keeps hourly gzip files for 24 h only, so the hours we
evaluate on are copied to SLICE_ROOT (shared, outside git). `iter_windows` reads them with the
ingest normaliser and builds the same `WindowData` that `signals.window.load_window` builds from
Postgres: posts in the window, likes/reposts/follows whose subject got 2..max_key_accounts
distinct accounts in the window, per-second background counts of every event, and the number of
distinct posting accounts.

Splits are whole UTC hours. Never tune detector settings or eval rules on `heldout` hours.
"""

import logging
import shutil
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ingest.normalize import Action, Delete, Post, normalize
from ingest.recorder import read_events, recorded_files
from signals.config import DEFAULT, DetectorConfig
from signals.window import WAction, WindowData, WPost

log = logging.getLogger(__name__)

RECORD_ROOT = Path("/srv/junction-espoo/data/jetstream")
SLICE_ROOT = Path("/srv/junction-espoo/data/eval-slices/jetstream")

# UTC hours (YYYYMMDDHH). Hour 19 starts at 19:41 (recording began then); only full windows count.
SPLITS: dict[str, list[str]] = {
    "dev": ["2026111319", "2026111320"],
    "heldout": ["2026111321", "2026111322"],
}

MAX_GAP_S = 60.0  # a window with a longer hole in the recording is skipped, not evaluated
REORDER_S = 120.0  # events can arrive this much out of order (cursor rewind, host failover)


def file_hour(path: Path) -> str:
    """'<root>/20261113/19-1794598897.jsonl.gz' -> '2026111319'."""
    return f"{path.parent.name}{path.name[:2]}"


def hour_files(root: Path, hours: Iterable[str]) -> list[Path]:
    wanted = set(hours)
    return [p for p in recorded_files(root) if file_hour(p) in wanted]


def copy_hours(src: Path, dst: Path, hours: Iterable[str]) -> list[Path]:
    """Copy the recorded files of these hours from src to dst (same layout). Existing files of the
    same name are overwritten only if the source is bigger (the hour was still being written)."""
    out = []
    for p in hour_files(src, hours):
        target = dst / p.parent.name / p.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_size < p.stat().st_size:
            shutil.copy2(p, target)
        out.append(target)
    return out


@dataclass
class _Bucket:
    """Raw events of one window, before the load_window-equivalent filtering."""

    posts: dict[str, WPost] = field(default_factory=dict)
    actions: dict[str, list[tuple]] = field(
        default_factory=lambda: {"like": [], "repost": [], "follow": []}
    )
    deletes: set[str] = field(default_factory=set)
    times: dict[str, list[float]] = field(
        default_factory=lambda: {"post": [], "like": [], "repost": [], "follow": []}
    )
    first_t: float = float("inf")
    last_t: float = float("-inf")
    max_gap: float = 0.0
    _prev_t: float | None = None

    def tick(self, t: float) -> None:
        if self._prev_t is not None and t > self._prev_t:
            self.max_gap = max(self.max_gap, t - self._prev_t)
        self._prev_t = t if self._prev_t is None else max(self._prev_t, t)
        self.first_t = min(self.first_t, t)
        self.last_t = max(self.last_t, t)


_KIND = {"likes": "like", "reposts": "repost", "follows": "follow"}


def _to_window(b: _Bucket, start: float, end: float, cfg: DetectorConfig) -> WindowData:
    wd = WindowData(start=start, end=end)
    for uri in sorted(b.posts):
        p = b.posts[uri]
        p.deleted = uri in b.deletes
        wd.posts.append(p)
    lo, hi = cfg.min_key_accounts, cfg.max_key_accounts
    for kind in ("like", "repost", "follow"):
        rows = b.actions[kind]
        # (uri, did, t, subject_uri_or_did, subject_did); key = subject_uri, or subject_did (follow)
        by_subject: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            by_subject[r[3]].add(r[1])
        keep = {s for s, dids in by_subject.items() if lo <= len(dids) <= hi}
        for r in sorted((r for r in rows if r[3] in keep), key=lambda r: r[0]):
            wd.actions.append(WAction(kind, r[0], r[1], r[2], r[3], r[4], r[0] in b.deletes))
    wd.background = WindowData.background_from_times(start, end, b.times)
    wd.active_accounts = len({p.did for p in wd.posts})
    return wd


@dataclass
class WindowInfo:
    start: float
    end: float
    skipped: str | None = None  # reason, when the window is not evaluated
    events: int = 0
    max_gap_s: float = 0.0

    @property
    def label(self) -> str:
        return datetime.fromtimestamp(self.start, UTC).strftime("%Y-%m-%dT%H:%MZ")


def iter_windows(
    files: list[Path], length: float = 600.0, cfg: DetectorConfig = DEFAULT
) -> Iterator[tuple[WindowInfo, WindowData | None]]:
    """Tile the recording into clock-aligned windows of `length` seconds and yield
    (info, WindowData) for each one, in time order. Windows the recording doesn't fully cover
    (start/end of the slice, or a hole longer than MAX_GAP_S) are yielded with data None and a
    `skipped` reason, so callers can report them instead of silently dropping them."""
    buckets: dict[int, _Bucket] = {}
    seen: set[str] = set()  # dedup across failover hosts (the writer does the same)
    emitted: set[int] = set()
    late = 0
    max_t = float("-inf")

    def finish(k: int) -> tuple[WindowInfo, WindowData | None]:
        b = buckets.pop(k)
        emitted.add(k)
        start, end = k * length, (k + 1) * length
        info = WindowInfo(start, end, events=sum(len(v) for v in b.times.values()))
        info.max_gap_s = round(b.max_gap, 1)
        if b.first_t - start > MAX_GAP_S:
            info.skipped = "recording starts inside this window"
        elif end - b.last_t > MAX_GAP_S:
            info.skipped = "recording ends inside this window"
        elif b.max_gap > MAX_GAP_S:
            info.skipped = f"hole of {b.max_gap:.0f}s in the recording"
        if info.skipped:
            return info, None
        return info, _to_window(b, start, end, cfg)

    for _raw, ev in read_events(files):
        row = normalize(ev)
        if not isinstance(row, Post | Action | Delete):
            continue
        t = row.seen_at.timestamp()
        max_t = max(max_t, t)
        k = int(t // length)
        if k in emitted:
            late += 1
            continue
        b = buckets.get(k)
        if b is None:
            b = buckets[k] = _Bucket()
        b.tick(t)
        if isinstance(row, Delete):
            b.deletes.add(row.uri)
            continue
        if row.uri in seen:
            continue
        seen.add(row.uri)
        if isinstance(row, Post):
            b.times["post"].append(t)
            b.posts[row.uri] = WPost(
                uri=row.uri,
                did=row.did,
                t=t,
                text=row.text,
                text_hash=row.text_hash,
                embed_urls=list(row.embed_urls),
                media_hashes=list(row.media_hashes),
                reply_to=row.reply_to,
                reply_root=row.reply_root,
                quote_uri=row.quote_uri,
                created_t=row.created_at.timestamp() if row.created_at else None,
                langs=list(row.langs or []),
            )
        else:
            kind = _KIND[row.table]
            b.times[kind].append(t)
            subject = row.subject_did if kind == "follow" else row.subject_uri
            b.actions[kind].append((row.uri, row.did, t, subject, row.subject_did))
        # Windows that ended more than REORDER_S ago are complete.
        for done in sorted(x for x in buckets if (x + 1) * length + REORDER_S < max_t):
            yield finish(done)
    for k in sorted(buckets):
        yield finish(k)
    if late:
        log.info("dropped %d events that arrived after their window was closed", late)
