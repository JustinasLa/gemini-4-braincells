"""Jetstream consumer: websocket reader thread -> queue -> normalise -> batched DB writes.

Resumes from the stored cursor (minus a small rewind; inserts are idempotent), fails over between
public Jetstream hosts with exponential backoff, and logs throughput and lag.
"""

import json
import logging
import os
import queue
import random
import threading
import time
from urllib.parse import urlencode

import psycopg
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as ws_connect

from ingest.normalize import COLLECTIONS, normalize, valid_time_us
from ingest.recorder import Recorder, prune_recordings, read_events
from ingest.writer import Batch, Writer

log = logging.getLogger(__name__)

DEFAULT_HOSTS = (
    "jetstream2.us-east.bsky.network",
    "jetstream1.us-east.bsky.network",
    "jetstream2.us-west.bsky.network",
    "jetstream1.us-west.bsky.network",
)
REWIND_US = 5_000_000  # resume 5 s before the stored cursor
STOP = object()


class HostLagging(Exception):
    """Raised inside the reader to drop a host whose stream runs far behind real time."""


class LagWatchdog:
    """True once the stream has been more than `max_lag_s` behind real time for `for_s` seconds
    while our own queue is nearly empty, i.e. the upstream host is late, not us."""

    def __init__(self, max_lag_s: float = 60.0, for_s: float = 120.0, max_queue: int = 5000):
        self.max_lag_s, self.for_s, self.max_queue = max_lag_s, for_s, max_queue
        self.since: float | None = None

    def update(self, lag_s: float | None, queue_size: int, now: float) -> bool:
        if lag_s is None or lag_s < self.max_lag_s or queue_size > self.max_queue:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        if now - self.since >= self.for_s:
            self.since = None
            return True
        return False


class Reader(threading.Thread):
    """Reads raw messages from Jetstream into a queue. Owns reconnects and host failover."""

    def __init__(
        self, hosts: list[str], cursor_us: int | None, out: queue.Queue, recorder: Recorder | None
    ) -> None:
        super().__init__(name="jetstream-reader", daemon=True)
        self.hosts = hosts
        self.cursor_us = cursor_us
        self.out = out
        self.recorder = recorder
        self.host: str | None = None
        self.reconnects = 0
        self.bad_frames = 0
        self.stop_event = threading.Event()
        self.failover_event = threading.Event()

    def request_failover(self) -> None:
        """Ask the reader to drop the current host and move to the next one."""
        self.failover_event.set()

    def url(self, host: str) -> str:
        params = [("wantedCollections", c) for c in COLLECTIONS]
        if self.cursor_us:
            params.append(("cursor", str(self.cursor_us - REWIND_US)))
        return f"wss://{host}/subscribe?{urlencode(params)}"

    def run(self) -> None:
        try:
            self._run()
        finally:  # even if the thread dies, so the writer stops instead of waiting forever
            if self.recorder:
                self.recorder.close()
            self.out.put(STOP)

    def _run(self) -> None:
        backoff = 1.0
        i = 0
        while not self.stop_event.is_set():
            host = self.hosts[i % len(self.hosts)]
            try:
                url = self.url(host)
                log.info("connecting to %s (cursor=%s)", host, self.cursor_us)
                with ws_connect(url, max_size=2**22, open_timeout=15, close_timeout=5) as ws:
                    self.host = host
                    self.failover_event.clear()  # a request made while disconnected is stale
                    got_any = False
                    while not self.stop_event.is_set():
                        if self.failover_event.is_set():
                            self.failover_event.clear()
                            raise HostLagging(f"{host} lags behind real time")
                        raw = ws.recv(timeout=30)
                        if isinstance(raw, bytes):
                            raw = raw.decode()
                        # time_us is all we need here; full parse happens on the writer side.
                        ev = json.loads(raw)
                        if not isinstance(
                            ev, dict
                        ):  # Jetstream frames are objects; skip, don't die
                            self.bad_frames += 1
                            log.warning("skipped a non-object frame: %r", raw[:200])
                            continue
                        t = ev.get("time_us")
                        if valid_time_us(t):
                            self.cursor_us = t
                            if self.recorder:
                                self.recorder.write(raw, t)
                        self.out.put((raw, ev))
                        if not got_any:
                            got_any = True
                            backoff = 1.0
            except (
                OSError,
                TimeoutError,
                WebSocketException,
                json.JSONDecodeError,
                HostLagging,
            ) as exc:
                if self.stop_event.is_set():
                    break
                self.reconnects += 1
                i += 1  # fail over to the next host
                sleep = min(backoff, 30.0) * (0.5 + random.random())
                log.warning(
                    "jetstream %s: %s: %s; next host in %.1fs", host, type(exc).__name__, exc, sleep
                )
                time.sleep(sleep)
                backoff = min(backoff * 2, 30.0)


def load_cursor(conn: psycopg.Connection, stream: str) -> int | None:
    row = conn.execute(
        "SELECT cursor_time_us FROM ingest_state WHERE stream = %s", (stream,)
    ).fetchone()
    return row[0] if row else None


def write_loop(
    source: queue.Queue,
    writer: Writer,
    *,
    host_fn=lambda: None,
    batch_size: int = 2000,
    flush_s: float = 1.0,
    stats_s: float = 30.0,
    maintain_fn=None,
    lag_fn=None,
    on_stats=None,
    max_events: int | None = None,
) -> int:
    """Drain the queue into the DB until STOP (or max_events). Returns events processed."""
    batch = Batch()
    last_flush = last_stats = time.monotonic()
    last_maint = 0.0
    total = window_n = 0
    window_counts: dict[str, int] = {}
    eps = None
    while True:
        try:
            item = source.get(timeout=flush_s)
        except queue.Empty:
            item = None
        if item is STOP:
            writer.flush(batch, host_fn(), eps)
            return total
        if item is not None:
            _raw, ev = item
            total += 1
            window_n += 1
            try:
                row = normalize(ev)
            except Exception:  # one malformed event must never stop ingest (or crash-loop it)
                log.exception("skipped an event the normaliser can't read: %r", _raw[:300])
                row = None
            if row is not None and not writer.dedupe(row):
                batch.add(row, ev["time_us"])
                k = type(row).__name__
                window_counts[k] = window_counts.get(k, 0) + 1
            elif valid_time_us(ev.get("time_us")):  # never resume from a malformed cursor
                batch.last_time_us = ev["time_us"]
        now = time.monotonic()
        if batch.n >= batch_size or (now - last_flush >= flush_s and batch.n):
            dt = writer.flush(batch, host_fn(), eps)
            if dt > 2:
                log.warning("slow flush: %d rows in %.1fs", batch.n, dt)
            batch = Batch()
            last_flush = now
        if now - last_stats >= stats_s:
            eps = window_n / (now - last_stats)
            lag = lag_fn() if lag_fn else None
            if on_stats:
                on_stats(lag, source.qsize())
            log.info(
                "events/s=%.0f lag_s=%s queue=%d rows=%d dupes=%d %s",
                eps,
                f"{lag:.1f}" if lag is not None else "-",
                source.qsize(),
                writer.rows_written,
                writer.dupes_dropped,
                " ".join(f"{k}={v}" for k, v in sorted(window_counts.items())),
            )
            window_n = 0
            window_counts = {}
            last_stats = now
        if maintain_fn and now - last_maint > 600:
            try:
                maintain_fn()
            except Exception:  # maintenance must never stop ingest
                log.exception("maintenance failed")
            last_maint = now
        if max_events is not None and total >= max_events:
            writer.flush(batch, host_fn(), eps)
            return total


def run_live(
    database_url: str,
    hosts: list[str],
    record_dir: str | None,
    retention_hours: int,
    likes_retention_hours: int | None = None,
    record_keep_hours: int = 24,
    max_events: int | None = None,
) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        writer = Writer(conn, stream="jetstream")
        cursor = load_cursor(conn, "jetstream")
        q: queue.Queue = queue.Queue(maxsize=200_000)
        recorder = Recorder(record_dir) if record_dir else None
        reader = Reader(hosts, cursor, q, recorder)
        reader.start()

        def lag() -> float | None:
            return time.time() - reader.cursor_us / 1e6 if reader.cursor_us else None

        watchdog = LagWatchdog(
            max_lag_s=float(os.environ.get("INGEST_FAILOVER_LAG_S", "60")),
            for_s=float(os.environ.get("INGEST_FAILOVER_AFTER_S", "120")),
        )

        def watch(lag_s: float | None, queue_size: int) -> None:
            if watchdog.update(lag_s, queue_size, time.monotonic()):
                log.warning(
                    "stream %.1fs behind for %.0fs on %s; failing over",
                    lag_s,
                    watchdog.for_s,
                    reader.host,
                )
                reader.request_failover()

        def maintain() -> None:
            writer.maintain(retention_hours, likes_retention_hours)
            if record_dir:
                prune_recordings(record_dir, record_keep_hours)

        try:
            n = write_loop(
                q,
                writer,
                host_fn=lambda: reader.host,
                lag_fn=lag,
                on_stats=watch,
                maintain_fn=maintain,
                max_events=max_events,
            )
        finally:
            reader.stop_event.set()
            reader.join(timeout=35)
        log.info("stopped after %d events (reconnects=%d)", n, reader.reconnects)


def run_replay(
    database_url: str,
    paths: list,
    speed: float = 0.0,
    max_events: int | None = None,
    stream: str = "replay",
) -> int:
    """Feed recorded files through the same normaliser and writer. speed=0: as fast as possible;
    speed=1: real time; speed=10: 10x."""
    q: queue.Queue = queue.Queue(maxsize=50_000)

    def produce() -> None:
        first_us = start = None
        for raw, ev in read_events(paths):
            t = ev.get("time_us")
            if speed > 0 and isinstance(t, int):
                if first_us is None:
                    first_us, start = t, time.monotonic()
                wait = (t - first_us) / 1e6 / speed - (time.monotonic() - start)
                if wait > 0:
                    time.sleep(wait)
            q.put((raw, ev))
        q.put(STOP)

    threading.Thread(target=produce, name="replay-reader", daemon=True).start()
    with psycopg.connect(database_url, autocommit=True) as conn:
        writer = Writer(conn, stream=stream)
        n = write_loop(q, writer, host_fn=lambda: "replay", max_events=max_events)
    log.info("replayed %d events into %s", n, stream)
    return n
