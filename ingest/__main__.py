"""Ingest CLI.

python -m ingest run [--record DIR] [--retention-hours 48]
python -m ingest replay DIR_OR_FILES... [--speed 0] [--database-url URL]
python -m ingest maintain [--retention-hours 48]
"""

import argparse
import logging
import os
from pathlib import Path

import psycopg

from common.config import get_settings
from ingest.consumer import DEFAULT_HOSTS, run_live, run_replay
from ingest.recorder import recorded_files
from ingest.writer import Writer


def _hosts() -> list[str]:
    hosts = list(DEFAULT_HOSTS)
    env = os.environ.get("JETSTREAM_HOSTS")
    if env:
        hosts = [h.strip() for h in env.split(",") if h.strip()]
    url = get_settings().jetstream_url
    if url:  # JETSTREAM_URL's host goes first
        primary = url.split("://", 1)[-1].split("/", 1)[0]
        hosts = [primary] + [h for h in hosts if h != primary]
    return hosts


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m ingest")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="consume live Jetstream into Postgres")
    r.add_argument("--record", default=os.environ.get("INGEST_RECORD_DIR"),
                   help="also tee raw events to hourly gzip files in this directory")  # fmt: skip
    r.add_argument("--retention-hours", type=int,
                   default=int(os.environ.get("INGEST_RETENTION_HOURS", "48")))  # fmt: skip
    r.add_argument("--likes-retention-hours", type=int,
                   default=int(os.environ.get("INGEST_LIKES_RETENTION_HOURS", "0")) or None,
                   help="keep likes shorter than other raw tables")  # fmt: skip
    r.add_argument("--record-keep-hours", type=int,
                   default=int(os.environ.get("INGEST_RECORD_KEEP_HOURS", "24")))  # fmt: skip
    r.add_argument("--max-events", type=int, default=None)
    rp = sub.add_parser("replay", help="replay recorded files into a (separate) database")
    rp.add_argument("paths", nargs="+", help="recording directory or .jsonl.gz files")
    rp.add_argument("--speed", type=float, default=0.0, help="0 = as fast as possible")
    rp.add_argument("--database-url", default=None)
    rp.add_argument("--max-events", type=int, default=None)
    m = sub.add_parser("maintain", help="create partitions, apply retention")
    m.add_argument("--retention-hours", type=int,
                   default=int(os.environ.get("INGEST_RETENTION_HOURS", "48")))  # fmt: skip
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db = get_settings().database_url
    if args.cmd == "run":
        run_live(
            db,
            _hosts(),
            args.record,
            args.retention_hours,
            args.likes_retention_hours,
            args.record_keep_hours,
            args.max_events,
        )
    elif args.cmd == "replay":
        files: list[Path] = []
        for x in args.paths:
            path = Path(x)
            files.extend(recorded_files(path) if path.is_dir() else [path])
        run_replay(args.database_url or db, files, args.speed, args.max_events)
    elif args.cmd == "maintain":
        with psycopg.connect(db, autocommit=True) as conn:
            print(Writer(conn).maintain(args.retention_hours))


if __name__ == "__main__":
    main()
