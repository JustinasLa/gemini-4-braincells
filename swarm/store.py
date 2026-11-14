"""SQL for meme swarms: the thumbnail hash cache and the `meme_swarms` table (migration 005).

The worker (swarm/live.py) writes; the API (api/routes/swarms.py) and the lookup read. Every read
treats a missing table as "nothing stored yet": the API returns an empty wall before the migration
has run instead of a 500.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from swarm.model import SwarmCard, SwarmDetail, SwarmPost, SwarmReport, SwarmWall
from swarm.picture import PictureHash, full_url, thumb_url

log = logging.getLogger(__name__)

HASH_RETRY = timedelta(hours=6)  # a thumbnail that failed is tried again after this
MAX_POSTS = 30


def stable_id(earliest_cid: str) -> str:
    """Short id of a group from its earliest file: 12 hex chars of sha256(cid)."""
    return hashlib.sha256(earliest_cid.encode()).hexdigest()[:12]


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
    """`picture_hashes`. Failures are remembered for HASH_RETRY, then fetched again."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def get(self, cids: list[str]) -> dict[str, PictureHash | None]:
        if not cids:
            return {}
        with self.conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """
                SELECT cid, phash, dhash, detail, width, height, tiny FROM picture_hashes
                WHERE cid = ANY(%s) AND (phash IS NOT NULL OR fetched_at > %s)
                """,
                (cids, datetime.now(UTC) - HASH_RETRY),
            ).fetchall()
        return {r["cid"]: _hash_from_row(r) for r in rows}

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
    """One picture group as the worker writes it."""

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
    vision_done: bool = False
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


def headline(report: SwarmReport) -> str:
    """The strongest signal that points the way of the verdict, in its own words."""
    want = {"swarm": "for", "organic": "against"}.get(report.verdict)
    for s in report.signals:
        if want is None or s.polarity == want:
            return s.summary
    return report.why_unclear or (report.signals[0].summary if report.signals else "")


def _card_fields(row: dict) -> dict[str, Any]:
    report = SwarmReport.model_validate(row["report"])
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


_ROW_COLS = """id, files, phash, dhash, rep_did, rep_cid, verdict, score, n_posts, n_accounts,
               first_at, last_at, report, posts, safe, kind, caption, window_h, updated_at"""
_CARD_COLS = """id, rep_did, rep_cid, verdict, score, n_posts, n_accounts, first_at, last_at,
                report, kind, window_h, updated_at"""


class SwarmStore:
    """Reads and writes `meme_swarms`. `conn` may be autocommit or not."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def _rows(self, sql: str, params: Any = None) -> list[dict]:
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                return cur.execute(sql, params).fetchall()
        except errors.UndefinedTable:
            # migration 005 not applied yet: nothing stored
            if not self.conn.autocommit:
                self.conn.rollback()
            return []

    # reads (API, lookup) ----------------------------------------------------------------------

    def wall(self, *, limit: int = 24, verdict: str = "all", max_age_h: float = 24) -> SwarmWall:
        """Safe groups only: swarm verdicts first by score, then the rest by score."""
        rows = self._rows(
            f"""
            SELECT {_CARD_COLS} FROM meme_swarms
            WHERE safe AND phash IS NOT NULL
              AND (%(verdict)s = 'all' OR verdict = %(verdict)s)
              AND updated_at > now() - make_interval(secs => %(age)s)
            ORDER BY (verdict = 'swarm') DESC, score DESC, n_accounts DESC, id
            LIMIT %(limit)s
            """,
            {"verdict": verdict, "limit": limit, "age": max_age_h * 3600},
        )
        return SwarmWall(
            items=[card_from_row(r) for r in rows],
            as_of=max((r["updated_at"] for r in rows), default=None),
            window_h=rows[0]["window_h"] if rows else None,
        )

    def detail(self, group_id: str, *, include_unsafe: bool = False) -> SwarmDetail | None:
        rows = self._rows(
            f"SELECT {_ROW_COLS} FROM meme_swarms WHERE id = %s AND (safe OR %s)",
            (group_id, include_unsafe),
        )
        return detail_from_row(rows[0]) if rows else None

    def find_by_cids(self, cids: list[str], *, include_unsafe: bool = False) -> SwarmDetail | None:
        if not cids:
            return None
        rows = self._rows(
            f"""
            SELECT {_ROW_COLS} FROM meme_swarms
            WHERE files && %s::text[] AND (safe OR %s)
            ORDER BY n_accounts DESC, updated_at DESC LIMIT 1
            """,
            (list(cids), include_unsafe),
        )
        return detail_from_row(rows[0]) if rows else None

    def group_hashes(
        self, *, include_unsafe: bool = False
    ) -> list[tuple[str, str, str, PictureHash]]:
        """(group id, did, cid, hash) for every hashed file of every stored group."""
        rows = self._rows(
            """
            SELECT s.id, h.did, h.cid, h.phash, h.dhash, h.detail, h.width, h.height, h.tiny
            FROM meme_swarms s JOIN picture_hashes h ON h.cid = ANY(s.files)
            WHERE h.phash IS NOT NULL AND (s.safe OR %s)
            """,
            (include_unsafe,),
        )
        out = []
        for r in rows:
            h = _hash_from_row(r)
            if h is not None:
                out.append((r["id"], r["did"], r["cid"], h))
        return out

    # writes (worker) --------------------------------------------------------------------------

    def prior(self, files: Iterable[str]) -> dict[str, list[PriorState]]:
        """{file cid: stored groups containing it}, oldest group first."""
        files = list(set(files))
        if not files:
            return {}
        rows = self._rows(
            """
            SELECT id, files, safe, unsafe_reason, kind, caption, vision_at, created_at
            FROM meme_swarms WHERE files && %s::text[] ORDER BY created_at, id
            """,
            (files,),
        )
        wanted = set(files)
        out: dict[str, list[PriorState]] = {}
        for r in rows:
            state = PriorState(
                r["id"],
                r["safe"],
                r["unsafe_reason"],
                r["kind"],
                r["caption"],
                r["vision_at"],
                r["created_at"],
            )
            for f in r["files"]:
                if f in wanted:
                    out.setdefault(f, []).append(state)
        return out

    def upsert(self, groups: list[StoredGroup], retired: Iterable[str] = ()) -> int:
        """Write this pass's groups; `retired` ids were merged into another group this pass."""
        now = datetime.now(UTC)
        with self.conn.transaction():
            retired = [r for r in retired if r not in {g.id for g in groups}]
            if retired:
                self.conn.execute("DELETE FROM meme_swarms WHERE id = ANY(%s)", (retired,))
            for g in groups:
                r = g.report
                self.conn.execute(
                    """
                    INSERT INTO meme_swarms (id, files, phash, dhash, rep_did, rep_cid, verdict,
                        score, n_posts, n_accounts, first_at, last_at, report, posts, safe,
                        unsafe_reason, labels_checked_at, kind, caption, vision_at, window_h,
                        updated_at)
                    VALUES (%(id)s, %(files)s, %(phash)s, %(dhash)s, %(rep_did)s, %(rep_cid)s,
                        %(verdict)s, %(score)s, %(n_posts)s, %(n_accounts)s, %(first_at)s,
                        %(last_at)s, %(report)s, %(posts)s, %(safe)s, %(reason)s,
                        CASE WHEN %(checked)s THEN %(now)s END, %(kind)s, %(caption)s,
                        CASE WHEN %(vision)s THEN %(now)s END, %(window_h)s, %(now)s)
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
                        kind = coalesce(EXCLUDED.kind, meme_swarms.kind),
                        caption = coalesce(EXCLUDED.caption, meme_swarms.caption),
                        vision_at = coalesce(EXCLUDED.vision_at, meme_swarms.vision_at),
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
                        "vision": g.vision_done,
                        "window_h": g.window_h,
                        "now": now,
                    },
                )
        return len(groups)

    def drop_stale(self, hours: float) -> int:
        """Delete groups no pass has seen for `hours`."""
        cur = self.conn.execute(
            "DELETE FROM meme_swarms WHERE updated_at < now() - make_interval(secs => %s)",
            (hours * 3600,),
        )
        return cur.rowcount or 0
