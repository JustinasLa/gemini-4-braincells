"""Batched, idempotent Postgres writes for normalised rows.

One flush = one transaction: accounts, raw rows, deletes, account events and the stream cursor
commit together, so after a crash we resume from a cursor that matches what's stored.
Inserts use ON CONFLICT DO NOTHING, so replaying an overlap is safe.
"""

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime

import psycopg

from ingest.normalize import AccountEvent, Action, Delete, Post, Row, us_to_dt

log = logging.getLogger(__name__)

_POST_SQL = """
INSERT INTO posts (uri, cid, did, text, langs, created_at, seen_at, reply_to, reply_root,
                   quote_uri, embed_kind, embed_urls, media_hashes, mentions, tags, text_hash)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""
_ACTION_SQL = {
    "reposts": "INSERT INTO reposts (uri, did, subject_uri, subject_did, created_at, seen_at) "
    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
    "likes": "INSERT INTO likes (uri, did, subject_uri, subject_did, created_at, seen_at) "
    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
    "follows": "INSERT INTO follows (uri, did, subject_did, created_at, seen_at) "
    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
}
_DELETE_SQL = """
UPDATE {table} t SET deleted_at = d.at
FROM unnest(%s::text[], %s::timestamptz[]) AS d(uri, at)
WHERE t.uri = d.uri AND t.deleted_at IS NULL
"""


def _hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


class RecentKeys:
    """Bounded set of recently written URIs. Catches duplicates that ON CONFLICT can't: the
    same record replayed by another Jetstream host has another time_us, so another seen_at."""

    def __init__(self, capacity: int = 400_000) -> None:
        self.capacity = capacity
        self._d: OrderedDict[str, None] = OrderedDict()

    def seen(self, key: str) -> bool:
        if key in self._d:
            return True
        self._d[key] = None
        if len(self._d) > self.capacity:
            self._d.popitem(last=False)
        return False


@dataclass
class Batch:
    posts: list[Post] = field(default_factory=list)
    actions: dict[str, list[Action]] = field(
        default_factory=lambda: {"reposts": [], "likes": [], "follows": []}
    )
    deletes: dict[str, list[Delete]] = field(default_factory=dict)
    account_events: list[AccountEvent] = field(default_factory=list)
    last_time_us: int | None = None
    n: int = 0

    def add(self, row: Row, time_us: int) -> None:
        self.n += 1
        self.last_time_us = time_us
        if isinstance(row, Post):
            self.posts.append(row)
        elif isinstance(row, Action):
            self.actions[row.table].append(row)
        elif isinstance(row, Delete):
            self.deletes.setdefault(row.table, []).append(row)
        elif isinstance(row, AccountEvent):
            self.account_events.append(row)

    def hours(self) -> set[datetime]:
        out = {_hour(p.seen_at) for p in self.posts}
        for rows in self.actions.values():
            out.update(_hour(a.seen_at) for a in rows)
        return out

    def dids(self) -> list[str]:
        s = {p.did for p in self.posts}
        for rows in self.actions.values():
            s.update(a.did for a in rows)
        s.update(e.did for e in self.account_events)
        return sorted(s)


class Writer:
    def __init__(self, conn: psycopg.Connection, stream: str = "jetstream") -> None:
        self.conn = conn
        self.stream = stream
        self.recent = RecentKeys()
        self._hours_ready: set[datetime] = set()
        self.rows_written = 0
        self.dupes_dropped = 0
        self.rows_skipped = 0

    def dedupe(self, row: Row) -> bool:
        """True if the row should be dropped as a cross-host duplicate."""
        if isinstance(row, (Post, Action)):
            if self.recent.seen(row.uri):
                self.dupes_dropped += 1
                return True
        return False

    def flush(self, batch: Batch, host: str | None, events_per_s: float | None) -> float:
        """Write the batch in one transaction. Returns seconds taken. If the batch fails because
        of its content (one malformed record must never stop ingest), write it again row by row
        and skip only the rows Postgres rejects."""
        try:
            return self._flush(batch, host, events_per_s, safe=False)
        except psycopg.Error as exc:
            if self.conn.broken or self.conn.closed:
                raise  # the database is gone: let the process fail and restart
            log.warning(
                "batch write failed (%s: %s); retrying row by row",
                type(exc).__name__,
                str(exc).splitlines()[0][:200],
            )
            return self._flush(batch, host, events_per_s, safe=True)

    def _many(self, cur: psycopg.Cursor, sql: str, params: list, safe: bool, what: str) -> None:
        if not safe:
            cur.executemany(sql, params)
            return
        for row in params:
            try:
                with self.conn.transaction():  # savepoint: a bad row rolls back alone
                    cur.execute(sql, row)
            except psycopg.Error as exc:
                if self.conn.broken or self.conn.closed:
                    raise
                self.rows_skipped += 1
                log.warning(
                    "skipped %s %s: %s",
                    what,
                    row[0] if row else "?",
                    str(exc).splitlines()[0][:200],
                )

    def _flush(
        self, batch: Batch, host: str | None, events_per_s: float | None, safe: bool
    ) -> float:
        if batch.n == 0:
            return 0.0
        t0 = time.monotonic()
        new_hours = sorted(batch.hours() - self._hours_ready)
        with self.conn.transaction():
            cur = self.conn.cursor()
            for h in new_hours:
                cur.execute("SELECT noise_ensure_partitions(%s, %s)", (h, h))
            dids = batch.dids()
            if dids:
                cur.execute(
                    "INSERT INTO accounts (did) SELECT unnest(%s::text[]) ON CONFLICT DO NOTHING",
                    (dids,),
                )
            if batch.posts:
                self._many(
                    cur,
                    _POST_SQL,
                    [
                        (
                            p.uri,
                            p.cid,
                            p.did,
                            p.text,
                            p.langs,
                            p.created_at,
                            p.seen_at,
                            p.reply_to,
                            p.reply_root,
                            p.quote_uri,
                            p.embed_kind,
                            p.embed_urls or None,
                            p.media_hashes or None,
                            p.mentions or None,
                            p.tags or None,
                            p.text_hash,
                        )
                        for p in batch.posts
                    ],
                    safe,
                    "post",
                )
            for table, rows in batch.actions.items():
                if not rows:
                    continue
                if table == "follows":
                    params = [(a.uri, a.did, a.subject_did, a.created_at, a.seen_at) for a in rows]
                else:
                    params = [
                        (a.uri, a.did, a.subject_uri, a.subject_did, a.created_at, a.seen_at)
                        for a in rows
                    ]
                self._many(cur, _ACTION_SQL[table], params, safe, table)
            for table, rows in batch.deletes.items():
                cur.execute(
                    _DELETE_SQL.format(table=table),
                    ([d.uri for d in rows], [d.seen_at for d in rows]),
                )
            if batch.account_events:
                self._account_events(cur, batch.account_events)
            if batch.last_time_us is not None:
                cur.execute(
                    """
                    INSERT INTO ingest_state (stream, cursor_time_us, host, last_event_at,
                                              events_per_s, updated_at)
                    VALUES (%s, %s, %s, %s, %s, now())
                    ON CONFLICT (stream) DO UPDATE SET
                        cursor_time_us = EXCLUDED.cursor_time_us, host = EXCLUDED.host,
                        last_event_at = EXCLUDED.last_event_at,
                        events_per_s = EXCLUDED.events_per_s, updated_at = now()
                    """,
                    (
                        self.stream,
                        batch.last_time_us,
                        host,
                        us_to_dt(batch.last_time_us),
                        events_per_s,
                    ),
                )
        # Only after commit: a rolled-back flush also rolled back its CREATE TABLE.
        self._hours_ready.update(new_hours)
        self.rows_written += batch.n
        return time.monotonic() - t0

    @staticmethod
    def _account_events(cur: psycopg.Cursor, events: list[AccountEvent]) -> None:
        cur.executemany(
            """
            INSERT INTO account_events (did, kind, active, status, handle, seq, time_us, event_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """,
            [
                (e.did, e.kind, e.active, e.status, e.handle, e.seq, e.time_us, e.event_time)
                for e in events
            ],
        )
        for e in events:
            at = e.event_time or us_to_dt(e.time_us)
            if e.kind == "account" and e.status:
                cur.execute(
                    "UPDATE accounts SET status = %s, status_at = %s WHERE did = %s "
                    "AND (status_at IS NULL OR status_at <= %s)",
                    (e.status, at, e.did, at),
                )
            elif e.kind == "identity" and e.handle:
                cur.execute(
                    "UPDATE accounts SET handle = %s WHERE did = %s",
                    (e.handle, e.did),
                )

    def maintain(self, retention_hours: int, likes_retention_hours: int | None = None) -> list[str]:
        """Create partitions for the next hours; drop raw partitions past retention.
        Likes (2/3 of the volume) can be kept for less time than the other tables."""
        likes_h = likes_retention_hours or retention_hours
        with self.conn.transaction():
            self.conn.execute(
                "SELECT noise_ensure_partitions("
                "now() - interval '1 hour', now() + interval '3 hours')"
            )
            dropped = [
                r[0]
                for r in self.conn.execute(
                    "SELECT noise_drop_old_partitions("
                    "%s, now(), ARRAY['posts', 'reposts', 'follows'])",
                    (retention_hours,),
                ).fetchall()
            ]
            dropped += [
                r[0]
                for r in self.conn.execute(
                    "SELECT noise_drop_old_partitions(%s, now(), ARRAY['likes'])", (likes_h,)
                ).fetchall()
            ]
        if dropped:
            log.info("retention dropped %d partitions: %s", len(dropped), ", ".join(dropped))
        return dropped
