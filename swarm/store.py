"""SQL for meme swarms: the per-file table (`picture_hashes`: thumbnail hashes and sticky unsafe
marks) and the `meme_swarms` table (migration 005).

The worker (swarm/live.py) writes; the API (api/routes/swarms.py) and the lookup read. Every read
treats a missing table as "nothing stored yet": the API returns an empty wall before the migration
has run instead of a 500. Public reads (wall, detail, picture, find_by_*) only return groups that
are safe and that a pass updated within `fresh_min` minutes (SWARM_FRESH_MIN, default 20): a group
no pass looks at any more is no longer re-checked, so it is no longer shown.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from swarm.model import SwarmCard, SwarmDetail, SwarmPost, SwarmReport, SwarmWall
from swarm.picture import GROUP_DHASH, GROUP_PHASH, PictureHash, full_url, hamming, thumb_url

log = logging.getLogger(__name__)

HASH_RETRY = timedelta(hours=6)  # a thumbnail that failed is tried again after this
HASH_KEEP_H = 48.0  # hash rows of files no stored group uses are pruned after this
MAX_POSTS = 30
CHUNK = 2000  # values per ANY(%s) array
# Signals of these families describe accounts (fresh, empty, templated, boosting): their examples
# would point at individual accounts, so they are never stored or served.
PRIVATE_FAMILIES = frozenset({"accounts", "boost"})


def default_fresh_min() -> float:
    try:
        return float(os.environ.get("SWARM_FRESH_MIN", "20"))
    except ValueError:
        return 20.0


def stable_id(earliest_cid: str) -> str:
    """Short id of a group from its earliest file: 12 hex chars of sha256(cid)."""
    return hashlib.sha256(earliest_cid.encode()).hexdigest()[:12]


def public_report(report: SwarmReport) -> SwarmReport:
    """The report without examples on account and boost signals (a verdict is about the group,
    never one account)."""
    if not any(s.examples and s.family in PRIVATE_FAMILIES for s in report.signals):
        return report
    signals = [
        s.model_copy(update={"examples": []}) if s.family in PRIVATE_FAMILIES else s
        for s in report.signals
    ]
    return report.model_copy(update={"signals": signals})


def _chunks(items: list, n: int = CHUNK):
    for i in range(0, len(items), n):
        yield items[i : i + n]


# --- thumbnail hash cache ------------------------------------------------------------------------


class MemoryHashCache:
    """Hashes held in memory only (dry runs, tests)."""

    def __init__(self) -> None:
        self.items: dict[str, PictureHash | None] = {}

    def get(self, cids: list[str]) -> dict[str, PictureHash | None]:
        return {c: self.items[c] for c in cids if c in self.items}

    def put(self, found: dict[str, tuple[str, PictureHash | None]]) -> None:
        for cid, (_did, h) in found.items():
            self.items[cid] = h


class DbHashCache:
    """`picture_hashes`. Failures are remembered for HASH_RETRY, then fetched again. A row that
    only carries an unsafe mark (no hash, no error) is not a cached result."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def get(self, cids: list[str]) -> dict[str, PictureHash | None]:
        out: dict[str, PictureHash | None] = {}
        for part in _chunks(list(cids)):
            with self.conn.cursor(row_factory=dict_row) as cur:
                rows = cur.execute(
                    """
                    SELECT cid, phash, dhash, detail, width, height, tiny FROM picture_hashes
                    WHERE cid = ANY(%s)
                      AND (phash IS NOT NULL OR (error IS NOT NULL AND fetched_at > %s))
                    """,
                    (part, datetime.now(UTC) - HASH_RETRY),
                ).fetchall()
            out.update({r["cid"]: _hash_from_row(r) for r in rows})
        return out

    def put(self, found: dict[str, tuple[str, PictureHash | None]]) -> None:
        if not found:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO picture_hashes (cid, did, phash, dhash, detail, width, height, tiny,
                                            error, fetched_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (cid) DO UPDATE SET
                    did = EXCLUDED.did, phash = EXCLUDED.phash, dhash = EXCLUDED.dhash,
                    detail = EXCLUDED.detail, width = EXCLUDED.width, height = EXCLUDED.height,
                    tiny = EXCLUDED.tiny, error = EXCLUDED.error, fetched_at = now()
                """,
                [
                    (
                        cid,
                        did,
                        h.phash if h else None,
                        h.dhash if h else None,
                        h.detail if h else None,
                        h.width if h else None,
                        h.height if h else None,
                        h.tiny if h else None,
                        None if h else "thumbnail unavailable",
                    )
                    for cid, (did, h) in found.items()
                ],
            )


def _hash_from_row(r: dict) -> PictureHash | None:
    if not r.get("phash") or not r.get("dhash"):
        return None
    return PictureHash(
        r["phash"],
        r["dhash"],
        float(r.get("detail") or 0.0),
        r.get("width") or 1,
        r.get("height") or 1,
        bytes(r.get("tiny") or b""),
    )


# --- groups --------------------------------------------------------------------------------------


@dataclass
class StoredGroup:
    """One picture group as the worker writes it. kind, caption, vision_* are written as given:
    the pass carries an earlier read forward only while it is a read of the current rep_cid."""

    id: str
    files: list[str]
    rep_did: str
    rep_cid: str
    report: SwarmReport
    posts: list[SwarmPost] = field(default_factory=list)
    phash: str | None = None
    dhash: str | None = None
    safe: bool = False
    unsafe_reason: str | None = None
    labels_checked: bool = False
    kind: str | None = None
    caption: str | None = None
    vision_at: datetime | None = None
    vision_cid: str | None = None
    window_h: float | None = None


@dataclass(frozen=True)
class PriorState:
    """What a stored group already knows that a pass shouldn't recompute or forget."""

    id: str
    safe: bool
    unsafe_reason: str | None
    kind: str | None
    caption: str | None
    vision_at: datetime | None
    created_at: datetime | None
    vision_cid: str | None = None


def headline(report: SwarmReport) -> str:
    """The strongest signal that points the way of the verdict, in its own words."""
    want = {"swarm": "for", "organic": "against"}.get(report.verdict)
    for s in report.signals:
        if want is None or s.polarity == want:
            return s.summary
    return report.why_unclear or (report.signals[0].summary if report.signals else "")


def _card_fields(row: dict) -> tuple[dict[str, Any], SwarmReport]:
    report = public_report(SwarmReport.model_validate(row["report"]))
    return dict(
        id=row["id"],
        thumb=thumb_url(row["rep_did"], row["rep_cid"]),
        verdict=row["verdict"],
        score=float(row["score"]),
        n_posts=row["n_posts"],
        n_accounts=row["n_accounts"],
        first_at=row.get("first_at"),
        last_at=row.get("last_at"),
        families=report.families,
        headline=headline(report),
        kind=row.get("kind"),
    ), report


def card_from_row(row: dict) -> SwarmCard:
    fields, _ = _card_fields(row)
    return SwarmCard(**fields)


def detail_from_row(row: dict) -> SwarmDetail:
    fields, report = _card_fields(row)
    return SwarmDetail(
        **fields,
        picture=full_url(row["rep_did"], row["rep_cid"]),
        report=report,
        posts=[SwarmPost.model_validate(p) for p in (row.get("posts") or [])][:MAX_POSTS],
        caption=row.get("caption"),
    )


def near_duplicate(a: tuple[str, str], b: tuple[str, str]) -> bool:
    """Two wall tiles whose pictures hash alike (the same ad stored as two groups)."""
    return hamming(a[0], b[0]) <= GROUP_PHASH and hamming(a[1], b[1]) <= GROUP_DHASH


_ROW_COLS = """id, files, phash, dhash, rep_did, rep_cid, verdict, score, n_posts, n_accounts,
               first_at, last_at, report, posts, safe, kind, caption, window_h, updated_at"""
_CARD_COLS = """id, phash, dhash, rep_did, rep_cid, verdict, score, n_posts, n_accounts, first_at,
                last_at, report, kind, window_h, updated_at"""
WALL_ORDER = (
    "coalesce(kind = 'meme', false) DESC, (verdict = 'swarm') DESC, score DESC, n_accounts DESC, id"
)


class SwarmStore:
    """Reads and writes `meme_swarms` and the unsafe marks in `picture_hashes`.
    `conn` may be autocommit or not."""

    def __init__(self, conn: psycopg.Connection, *, fresh_min: float | None = None) -> None:
        self.conn = conn
        self.fresh_s = 60.0 * (default_fresh_min() if fresh_min is None else fresh_min)

    def _rows(self, sql: str, params: Any = None) -> list[dict]:
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                return cur.execute(sql, params).fetchall()
        except errors.UndefinedTable:
            # migration 005 not applied yet: nothing stored
            if not self.conn.autocommit:
                self.conn.rollback()
            return []

    # reads (API, lookup): safe and fresh only ---------------------------------------------------

    def wall(self, *, limit: int = 24, verdict: str = "all") -> SwarmWall:
        """Safe, fresh groups with a hashed picture: memes first, then swarm verdicts, by score.
        A tile whose picture hashes like one already listed is skipped."""
        rows = self._rows(
            f"""
            SELECT {_CARD_COLS} FROM meme_swarms
            WHERE safe AND phash IS NOT NULL AND dhash IS NOT NULL
              AND (%(verdict)s = 'all' OR verdict = %(verdict)s)
              AND updated_at > now() - make_interval(secs => %(fresh)s)
            ORDER BY {WALL_ORDER}
            LIMIT %(n)s
            """,
            {"verdict": verdict, "n": min(400, limit * 4), "fresh": self.fresh_s},
        )
        kept: list[dict] = []
        for r in rows:
            h = (r["phash"], r["dhash"])
            if any(near_duplicate(h, (k["phash"], k["dhash"])) for k in kept):
                continue
            kept.append(r)
            if len(kept) >= limit:
                break
        return SwarmWall(
            items=[card_from_row(r) for r in kept],
            as_of=max((r["updated_at"] for r in kept), default=None),
            window_h=kept[0]["window_h"] if kept else None,
        )

    def detail(self, group_id: str, *, include_unsafe: bool = False) -> SwarmDetail | None:
        rows = self._rows(
            f"""
            SELECT {_ROW_COLS} FROM meme_swarms
            WHERE id = %s AND (safe OR %s) AND updated_at > now() - make_interval(secs => %s)
            """,
            (group_id, include_unsafe, self.fresh_s),
        )
        return detail_from_row(rows[0]) if rows else None

    def picture_source(self, group_id: str) -> tuple[str, str] | None:
        """(rep_did, rep_cid) of a safe, fresh group: what GET /api/swarms/{id}/picture serves."""
        rows = self._rows(
            """
            SELECT rep_did, rep_cid FROM meme_swarms
            WHERE id = %s AND safe AND updated_at > now() - make_interval(secs => %s)
            """,
            (group_id, self.fresh_s),
        )
        return (rows[0]["rep_did"], rows[0]["rep_cid"]) if rows else None

    def find_by_cids(self, cids: list[str], *, include_unsafe: bool = False) -> SwarmDetail | None:
        if not cids:
            return None
        rows = self._rows(
            f"""
            SELECT {_ROW_COLS} FROM meme_swarms
            WHERE files && %s::text[] AND (safe OR %s)
              AND updated_at > now() - make_interval(secs => %s)
            ORDER BY n_accounts DESC, updated_at DESC LIMIT 1
            """,
            (list(cids), include_unsafe, self.fresh_s),
        )
        return detail_from_row(rows[0]) if rows else None

    def group_hashes(
        self, *, include_unsafe: bool = False
    ) -> list[tuple[str, str, str, PictureHash]]:
        """(group id, did, cid, hash) for every hashed file of every fresh stored group."""
        rows = self._rows(
            """
            SELECT s.id, h.did, h.cid, h.phash, h.dhash, h.detail, h.width, h.height, h.tiny
            FROM meme_swarms s JOIN picture_hashes h ON h.cid = ANY(s.files)
            WHERE h.phash IS NOT NULL AND (s.safe OR %s)
              AND s.updated_at > now() - make_interval(secs => %s)
            """,
            (include_unsafe, self.fresh_s),
        )
        out = []
        for r in rows:
            h = _hash_from_row(r)
            if h is not None:
                out.append((r["id"], r["did"], r["cid"], h))
        return out

    # worker reads -------------------------------------------------------------------------------

    def prior(self, files: Iterable[str]) -> dict[str, list[PriorState]]:
        """{file cid: stored groups containing it}, oldest group first (any age, any safety)."""
        files = list(set(files))
        out: dict[str, list[PriorState]] = {}
        wanted = set(files)
        for part in _chunks(files):
            rows = self._rows(
                """
                SELECT id, files, safe, unsafe_reason, kind, caption, vision_at, vision_cid,
                       created_at
                FROM meme_swarms WHERE files && %s::text[] ORDER BY created_at, id
                """,
                (part,),
            )
            for r in rows:
                state = PriorState(
                    r["id"],
                    r["safe"],
                    r["unsafe_reason"],
                    r["kind"],
                    r["caption"],
                    r["vision_at"],
                    r["created_at"],
                    r["vision_cid"],
                )
                for f in r["files"]:
                    if f in wanted and state not in out.get(f, []):
                        out.setdefault(f, []).append(state)
        for states in out.values():
            states.sort(key=lambda s: (s.created_at or datetime.max.replace(tzinfo=UTC), s.id))
        return out

    def unsafe_files(self, cids: Iterable[str]) -> dict[str, str]:
        """{file cid: reason} for files ever judged unsafe (sticky, per file)."""
        out: dict[str, str] = {}
        for part in _chunks(list(set(cids))):
            rows = self._rows(
                "SELECT cid, unsafe_reason FROM picture_hashes "
                "WHERE cid = ANY(%s) AND unsafe_reason IS NOT NULL",
                (part,),
            )
            out.update({r["cid"]: r["unsafe_reason"] for r in rows})
        return out

    # writes (worker) --------------------------------------------------------------------------

    def upsert(
        self,
        groups: list[StoredGroup],
        retired: Iterable[str] = (),
        unsafe: dict[str, tuple[str, str]] | None = None,
    ) -> int:
        """Write this pass's groups; `retired` ids were merged into another group this pass;
        `unsafe` {file cid: (did, reason)} marks files unsafe for good, in the same transaction."""
        now = datetime.now(UTC)
        with self.conn.transaction():
            if unsafe:
                with self.conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO picture_hashes (cid, did, unsafe_reason, unsafe_at, fetched_at)
                        VALUES (%s, %s, %s, now(), now())
                        ON CONFLICT (cid) DO UPDATE SET
                            unsafe_reason = coalesce(picture_hashes.unsafe_reason,
                                                     EXCLUDED.unsafe_reason),
                            unsafe_at = coalesce(picture_hashes.unsafe_at, EXCLUDED.unsafe_at)
                        """,
                        [(cid, did, reason) for cid, (did, reason) in unsafe.items()],
                    )
            ids = {g.id for g in groups}
            retired = [r for r in retired if r not in ids]
            if retired:
                self.conn.execute("DELETE FROM meme_swarms WHERE id = ANY(%s)", (retired,))
            for g in groups:
                r = public_report(g.report)
                self.conn.execute(
                    """
                    INSERT INTO meme_swarms (id, files, phash, dhash, rep_did, rep_cid, verdict,
                        score, n_posts, n_accounts, first_at, last_at, report, posts, safe,
                        unsafe_reason, labels_checked_at, kind, caption, vision_at, vision_cid,
                        window_h, updated_at)
                    VALUES (%(id)s, %(files)s, %(phash)s, %(dhash)s, %(rep_did)s, %(rep_cid)s,
                        %(verdict)s, %(score)s, %(n_posts)s, %(n_accounts)s, %(first_at)s,
                        %(last_at)s, %(report)s, %(posts)s, %(safe)s, %(reason)s,
                        CASE WHEN %(checked)s THEN %(now)s END, %(kind)s, %(caption)s,
                        %(vision_at)s, %(vision_cid)s, %(window_h)s, %(now)s)
                    ON CONFLICT (id) DO UPDATE SET
                        files = EXCLUDED.files, phash = EXCLUDED.phash, dhash = EXCLUDED.dhash,
                        rep_did = EXCLUDED.rep_did, rep_cid = EXCLUDED.rep_cid,
                        verdict = EXCLUDED.verdict, score = EXCLUDED.score,
                        n_posts = EXCLUDED.n_posts, n_accounts = EXCLUDED.n_accounts,
                        first_at = EXCLUDED.first_at, last_at = EXCLUDED.last_at,
                        report = EXCLUDED.report, posts = EXCLUDED.posts,
                        -- once unsafe, a group stays unsafe (labels can vanish with a deletion)
                        safe = EXCLUDED.safe AND meme_swarms.unsafe_reason IS NULL,
                        unsafe_reason = coalesce(meme_swarms.unsafe_reason, EXCLUDED.unsafe_reason),
                        labels_checked_at = coalesce(EXCLUDED.labels_checked_at,
                                                     meme_swarms.labels_checked_at),
                        kind = EXCLUDED.kind, caption = EXCLUDED.caption,
                        vision_at = EXCLUDED.vision_at, vision_cid = EXCLUDED.vision_cid,
                        window_h = EXCLUDED.window_h, updated_at = EXCLUDED.updated_at
                    """,
                    {
                        "id": g.id,
                        "files": g.files,
                        "phash": g.phash,
                        "dhash": g.dhash,
                        "rep_did": g.rep_did,
                        "rep_cid": g.rep_cid,
                        "verdict": r.verdict,
                        "score": r.score,
                        "n_posts": r.n_posts,
                        "n_accounts": r.n_accounts,
                        "first_at": r.first_at,
                        "last_at": r.last_at,
                        "report": Jsonb(r.model_dump(mode="json")),
                        "posts": Jsonb([p.model_dump(mode="json") for p in g.posts[:MAX_POSTS]]),
                        "safe": g.safe,
                        "reason": g.unsafe_reason,
                        "checked": g.labels_checked,
                        "kind": g.kind,
                        "caption": g.caption,
                        "vision_at": g.vision_at,
                        "vision_cid": g.vision_cid,
                        "window_h": g.window_h,
                        "now": now,
                    },
                )
        return len(groups)

    def drop_stale(self, hours: float) -> int:
        """Delete groups no pass has seen for `hours` (their files' unsafe marks stay)."""
        cur = self.conn.execute(
            "DELETE FROM meme_swarms WHERE updated_at < now() - make_interval(secs => %s)",
            (hours * 3600,),
        )
        return cur.rowcount or 0

    def prune_hashes(self, hours: float = HASH_KEEP_H, batch: int = 5000) -> int:
        """Delete hash rows older than `hours` that no stored group uses and that carry no unsafe
        mark (a file still posted is simply hashed again)."""
        cur = self.conn.execute(
            """
            DELETE FROM picture_hashes WHERE cid IN (
                SELECT h.cid FROM picture_hashes h
                WHERE h.fetched_at < now() - make_interval(secs => %s)
                  AND h.unsafe_reason IS NULL
                  AND NOT EXISTS (SELECT 1 FROM meme_swarms s WHERE s.files @> ARRAY[h.cid])
                LIMIT %s
            )
            """,
            (hours * 3600, batch),
        )
        return cur.rowcount or 0
