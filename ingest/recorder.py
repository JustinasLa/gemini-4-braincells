"""Record raw Jetstream messages to hourly gzip JSONL files, and read them back for replay.

Layout: <dir>/<YYYYMMDD>/<HH>-<process start epoch>.jsonl.gz (UTC hour of the event's time_us).
Files are gzip-flushed every few seconds, so a file cut short by a crash is still readable up
to the last flush; the reader stops cleanly at a truncated tail.
"""

import gzip
import json
import logging
import time
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


class Recorder:
    def __init__(self, directory: str | Path, flush_every_s: float = 5.0) -> None:
        self.dir = Path(directory)
        self.start = int(time.time())
        self.flush_every_s = flush_every_s
        self._hour: str | None = None
        self._fh: gzip.GzipFile | None = None
        self._last_flush = time.monotonic()
        self.lines = 0

    def write(self, raw: str, time_us: int) -> None:
        hour = datetime.fromtimestamp(time_us / 1e6, tz=UTC).strftime("%Y%m%d/%H")
        if hour != self._hour:
            self._open(hour)
        assert self._fh is not None
        self._fh.write(raw.encode() + b"\n")
        self.lines += 1
        if time.monotonic() - self._last_flush > self.flush_every_s:
            self._fh.flush()
            self._last_flush = time.monotonic()

    def _open(self, hour: str) -> None:
        self.close()
        day, hh = hour.split("/")
        path = self.dir / day / f"{hh}-{self.start}.jsonl.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = gzip.open(path, "ab", compresslevel=5)
        self._hour = hour
        log.info("recording to %s", path)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def prune_recordings(directory: str | Path, keep_hours: int) -> list[Path]:
    """Delete recorded hour files older than keep_hours (by the hour in the file name)."""
    cutoff = time.time() - keep_hours * 3600
    removed = []
    for path in recorded_files(directory):
        try:
            hour = datetime.strptime(f"{path.parent.name}{path.name[:2]}", "%Y%m%d%H")
        except ValueError:
            continue
        if hour.replace(tzinfo=UTC).timestamp() + 3600 < cutoff:
            path.unlink(missing_ok=True)
            removed.append(path)
    for day in Path(directory).glob("*"):
        if day.is_dir() and not any(day.iterdir()):
            day.rmdir()
    if removed:
        log.info("pruned %d recording files older than %dh", len(removed), keep_hours)
    return removed


def recorded_files(directory: str | Path) -> list[Path]:
    return sorted(Path(directory).glob("*/*.jsonl.gz"))


def read_events(paths: list[Path]) -> Iterator[tuple[str, dict]]:
    """Yield (raw line, parsed event) from recorded files in order; skip corrupt lines/tails."""
    for path in paths:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    try:
                        yield line, json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except (EOFError, gzip.BadGzipFile, zlib.error) as exc:
            log.warning("stopped reading %s at a truncated/corrupt tail: %s", path, exc)
