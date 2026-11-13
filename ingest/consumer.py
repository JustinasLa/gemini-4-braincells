"""Jetstream consumer: websocket reader thread -> queue -> normalise -> batched DB writes.

Resumes from the stored cursor (minus a small rewind; inserts are idempotent), fails over between
public Jetstream hosts with exponential backoff, and logs throughput and lag.
"""

import json
import logging
import queue
import random
import threading
import time
from urllib.parse import urlencode

import psycopg
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as ws_connect

from ingest.normalize import COLLECTIONS, normalize
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


class Reader(threading.Thread):
    """Reads raw messages from Jetstream into a queue. Owns reconnects and host failover."""

    def __init__(self, hosts: list[str], cursor_us: int | None, out: queue.Queue,
                 recorder: Recorder | None) -> None:  # fmt: skip
        super().__init__(name="jetstream-reader", daemon=True)
        self.hosts = hosts
        self.cursor_us = cursor_us
        self.out = out
        self.recorder = recorder
        self.host: str | None = None
        self.reconnects = 0
        self.stop_event = threading.Event()

    def url(self, host: str) -> str:
        params = [("wantedCollections", c) for c in COLLECTIONS]
        if self.cursor_us:
            params.append(("cursor", str(self.cursor_us - REWIND_US)))
        return f"wss://{host}/subscribe?{urlencode(params)}"

    def run(self) -> None:
        backoff = 1.0
        i = 0
        while not self.stop_event.is_set():
            host = self.hosts[i % len(self.hosts)]
            try:
                url = self.url(host)
                log.info("connecting to %s (cursor=%s)", host, self.cursor_us)
                with ws_connect(url, max_size=2**22, open_timeout=15, close_timeout=5) as ws:
                    self.host = host
                    got_any = False
                    while not self.stop_event.is_set():
                        raw = ws.recv(timeout=30)
                        if isinstance(raw, bytes):
                            raw = raw.decode()
                        # time_us is all we need here; full parse happens on the writer side.
                        ev = json.loads(raw)
                        t = ev.get("time_us")
                        if isinstance(t, int):
                            self.cursor_us = t
                            if self.recorder:
                                self.recorder.write(raw, t)
                        self.out.put((raw, ev))
                        if not got_any:
                            got_any = True
                            backoff = 1.0
            except (OSError, TimeoutError, WebSocketException, json.JSONDecodeError) as exc:
                if self.stop_event.is_set():
                    break
                self.reconnects += 1
                i += 1  # fail over to the next host
                sleep = min(backoff, 30.0) * (0.5 + random.random())
                log.warning("jetstream %s: %s: %s; next host in %.1fs",
                            host, type(exc).__name__, exc, sleep)  # fmt: skip
                time.sleep(sleep)
                backoff = min(backoff * 2, 30.0)
        if self.recorder:
            self.recorder.close()
        self.out.put(STOP)


def load_cursor(conn: psycopg.Connection, stream: str) -> int | None:
    row = conn.execute(
        "SELECT cursor_time_us FROM ingest_state WHERE stream = %s", (stream,)
    ).fetchone()
    return row[0] if row else None


def write_loop(source: queue.Queue, writer: Writer, *, host_fn=lambda: None,
               batch_size: int = 2000, flush_s: float = 1.0, stats_s: float = 30.0,
               maintain_fn=None, lag_fn=None,
               max_events: int | None = None) -> int:  # fmt: skip
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
            row = normalize(ev)
            if row is not None and not writer.dedupe(row):
                batch.add(row, ev["time_us"])
                k = type(row).__name__
                window_counts[k] = window_counts.get(k, 0) + 1
            elif isinstance(ev.get("time_us"), int):
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
            log.info(
                "events/s=%.0f lag_s=%s queue=%d rows=%d dupes=%d %s",
                eps, f"{lag:.1f}" if lag is not None else "-", source.qsize(),
                writer.rows_written, writer.dupes_dropped,
                " ".join(f"{k}={v}" for k, v in sorted(window_counts.items())),
            )  # fmt: skip
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


def run_live(database_url: str, hosts: list[str], record_dir: str | None,
             retention_hours: int, likes_retention_hours: int | None = None,
             record_keep_hours: int = 24, max_events: int | None = None) -> None:  # fmt: skip
    with psycopg.connect(database_url, autocommit=True) as conn:
        writer = Writer(conn, stream="jetstream")
        cursor = load_cursor(conn, "jetstream")
        q: queue.Queue = queue.Queue(maxsize=200_000)
        recorder = Recorder(record_dir) if record_dir else None
        reader = Reader(hosts, cursor, q, recorder)
        reader.start()

        def lag() -> float | None:
            return time.time() - reader.cursor_us / 1e6 if reader.cursor_us else None

        def maintain() -> None:
            writer.maintain(retention_hours, likes_retention_hours)
            if record_dir:
                prune_recordings(record_dir, record_keep_hours)

        try:
            n = write_loop(q, writer, host_fn=lambda: reader.host, lag_fn=lag,
                           maintain_fn=maintain, max_events=max_events)  # fmt: skip
        finally:
            reader.stop_event.set()
            reader.join(timeout=35)
        log.info("stopped after %d events (reconnects=%d)", n, reader.reconnects)


def run_replay(database_url: str, paths: list, speed: float = 0.0,
               max_events: int | None = None, stream: str = "replay") -> int:  # fmt: skip
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
