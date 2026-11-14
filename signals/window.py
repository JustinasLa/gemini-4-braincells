"""Window data: everything the detector needs for one time window, loaded from Postgres or built
in memory (tests, synthetic campaigns). The detector itself never touches the DB."""

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import psycopg

from signals.config import DetectorConfig


@dataclass(slots=True)
class WPost:
    uri: str
    did: str
    t: float  # seen_at, epoch seconds
    text: str | None = None
    text_hash: str | None = None
    embed_urls: list[str] = field(default_factory=list)
    media_hashes: list[str] = field(default_factory=list)
    reply_to: str | None = None
    reply_root: str | None = None
    quote_uri: str | None = None
    created_t: float | None = None  # client createdAt, epoch seconds
    deleted: bool = False
    langs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class WAction:
    """A like, repost or follow. subject is a post URI (like/repost) or a DID (follow)."""

    kind: str  # like | repost | follow
    uri: str
    did: str
    t: float
    subject: str
    subject_did: str | None = None
    deleted: bool = False


@dataclass
class WindowData:
    start: float
    end: float
    posts: list[WPost] = field(default_factory=list)
    actions: list[WAction] = field(default_factory=list)
    # Per-second counts of ALL events of each kind in the window (not just loaded ones): the
    # empirical background used by the timing null model. kind -> array of len(end - start).
    background: dict[str, np.ndarray] = field(default_factory=dict)
    # Distinct accounts per kind over the whole stream in the window (for popularity / base rates).
    active_accounts: int = 0
    _index: "WindowIndex | None" = field(default=None, repr=False, compare=False)

    def index(self) -> "WindowIndex":
        """Per-account / per-URI lookups, built once per window (scoring runs per cluster and
        must not rescan every post and action each time). Rebuilt if the lists changed size."""
        key = (len(self.posts), len(self.actions))
        if self._index is None or self._index.key != key:
            posts_by_did: dict[str, list[WPost]] = {}
            for p in self.posts:
                posts_by_did.setdefault(p.did, []).append(p)
            actions_by_did: dict[str, list[WAction]] = {}
            for a in self.actions:
                actions_by_did.setdefault(a.did, []).append(a)
            self._index = WindowIndex(key, posts_by_did, actions_by_did)
        return self._index

    def background_for(self, kind: str) -> np.ndarray:
        bg = self.background.get(kind)
        n = max(1, int(round(self.end - self.start)))
        if bg is None or len(bg) == 0 or bg.sum() == 0:
            return np.ones(n)
        return bg

    @staticmethod
    def background_from_times(start: float, end: float, times_by_kind: dict[str, list[float]]):
        n = max(1, int(round(end - start)))
        out = {}
        for kind, times in times_by_kind.items():
            idx = np.clip((np.asarray(times, dtype=float) - start).astype(int), 0, n - 1)
            out[kind] = np.bincount(idx, minlength=n).astype(float)
        return out


@dataclass
class WindowIndex:
    key: tuple[int, int]
    posts_by_did: dict[str, list[WPost]]
    actions_by_did: dict[str, list[WAction]]


# Plain range scans, no ORDER BY / DISTINCT ON: on an hour of likes (~1.3M rows) the planner
# misjudged the join and sorted 190 MB on disk (27 s). Duplicates (the same record from two
# Jetstream hosts, different seen_at) are dropped in Python, keeping the earliest copy, and the
# per-subject account counts are done in Python too. Same rows out, same order, as before.
_POSTS_SQL = """
SELECT uri, did, extract(epoch FROM seen_at)::float8 AS t, text, text_hash,
       embed_urls, media_hashes, reply_to, reply_root, quote_uri,
       extract(epoch FROM created_at)::float8 AS created_t, deleted_at IS NOT NULL AS deleted, langs
FROM posts WHERE seen_at >= %(s)s AND seen_at < %(e)s
"""

_ACTIONS_SQL = """
SELECT uri, did, extract(epoch FROM seen_at)::float8 AS t, subject_uri, subject_did,
       deleted_at IS NOT NULL AS deleted
FROM {table} WHERE seen_at >= %(s)s AND seen_at < %(e)s
"""

_FOLLOWS_SQL = """
SELECT uri, did, extract(epoch FROM seen_at)::float8 AS t, subject_did, subject_did,
       deleted_at IS NOT NULL AS deleted
FROM follows WHERE seen_at >= %(s)s AND seen_at < %(e)s
"""


def _earliest(rows) -> dict:
    """uri -> the earliest row (index 2 is seen_at) among duplicates."""
    best: dict = {}
    for r in rows:
        old = best.get(r[0])
        if old is None or r[2] < old[2]:
            best[r[0]] = r
    return best


def _shared_subjects(rows: dict, lo: int, hi: int) -> list:
    """Rows whose subject (index 3) was hit by lo..hi distinct accounts, ordered by uri.
    Only these can create edges: one account is no co-action, and viral subjects (> hi) can't
    tell coordination from popularity."""
    accounts: dict[str, set] = {}
    for r in rows.values():
        accounts.setdefault(r[3], set()).add(r[1])
    keep = {subj for subj, a in accounts.items() if lo <= len(a) <= hi}
    return sorted((r for r in rows.values() if r[3] in keep), key=lambda r: r[0])


_BACKGROUND_SQL = """
SELECT floor(extract(epoch FROM seen_at) - %(s_epoch)s)::int AS sec, count(*)
FROM {table} WHERE seen_at >= %(s)s AND seen_at < %(e)s GROUP BY 1
"""


def load_window(
    conn: psycopg.Connection, start: datetime, end: datetime, cfg: DetectorConfig
) -> WindowData:
    p = {
        "s": start,
        "e": end,
        "lo": cfg.min_key_accounts,
        "hi": cfg.max_key_accounts,
        "s_epoch": start.timestamp(),
    }
    wd = WindowData(start=start.timestamp(), end=end.timestamp())
    cur = conn.cursor()
    posts = _earliest(cur.execute(_POSTS_SQL, p))
    for r in sorted(posts.values(), key=lambda r: r[0]):
        wd.posts.append(
            WPost(
                uri=r[0],
                did=r[1],
                t=r[2],
                text=r[3],
                text_hash=r[4],
                embed_urls=r[5] or [],
                media_hashes=r[6] or [],
                reply_to=r[7],
                reply_root=r[8],
                quote_uri=r[9],
                created_t=r[10],
                deleted=r[11],
                langs=r[12] or [],
            )
        )
    lo, hi = cfg.min_key_accounts, cfg.max_key_accounts
    for table, kind in (("likes", "like"), ("reposts", "repost")):
        rows = _earliest(cur.execute(_ACTIONS_SQL.format(table=table), p))
        for r in _shared_subjects(rows, lo, hi):
            wd.actions.append(WAction(kind, r[0], r[1], r[2], r[3], r[4], r[5]))
    rows = _earliest(cur.execute(_FOLLOWS_SQL, p))
    for r in _shared_subjects(rows, lo, hi):
        wd.actions.append(WAction("follow", r[0], r[1], r[2], r[3], r[4], r[5]))
    n = max(1, int(round(wd.end - wd.start)))
    for table, kind in (
        ("posts", "post"),
        ("likes", "like"),
        ("reposts", "repost"),
        ("follows", "follow"),
    ):
        bg = np.zeros(n)
        for sec, cnt in cur.execute(_BACKGROUND_SQL.format(table=table), p):
            if 0 <= sec < n:
                bg[sec] = cnt
        wd.background[kind] = bg
    row = cur.execute(
        "SELECT count(DISTINCT did) FROM posts WHERE seen_at >= %(s)s AND seen_at < %(e)s", p
    ).fetchone()
    wd.active_accounts = row[0] if row else 0
    return wd
