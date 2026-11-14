"""Meme swarm worker and one-off runs.

python -m swarm once [--window 24h] [--min-accounts 5] [--limit N]   # one pass, writes meme_swarms
python -m swarm once --dry-run [--database-url URL]                  # one pass, writes NOTHING
python -m swarm run [--every 5m]                                     # loop

Nothing new reaches the public wall without a working vision reader (a Nemotron key) unless
--wall-without-vision (or SWARM_WALL_WITHOUT_VISION=1) is given. The connection runs with
statement_timeout 30 s and lock_timeout 3 s, so a pass never sits long in front of or behind
ingest's partition DROP.

--dry-run opens the connection with default_transaction_read_only=on, keeps thumbnail hashes and
account enrichment in memory, and prints one line per group (verdict, score, families, accounts,
posts, safe, first post text, thumbnail URL) followed by the pass stats as JSON.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

import psycopg

from common.config import get_settings
from swarm.live import LiveConfig, describe, nemotron_reader, run_pass
from swarm.store import DbHashCache, MemoryHashCache

log = logging.getLogger("swarm")


def duration_h(text: str) -> float:
    """'24h' / '90m' / '2d' / '6' (hours) -> hours."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", text)
    if not m:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r}")
    n, unit = float(m.group(1)), m.group(2) or "h"
    return n * {"s": 1 / 3600, "m": 1 / 60, "h": 1, "d": 24}[unit]


STATEMENT_TIMEOUT = "30s"
LOCK_TIMEOUT = "3s"


def open_connection(url: str, *, read_only: bool) -> psycopg.Connection:
    options = f"-c statement_timeout={STATEMENT_TIMEOUT} -c lock_timeout={LOCK_TIMEOUT}"
    if read_only:
        options += " -c default_transaction_read_only=on"
    return psycopg.connect(url, autocommit=True, connect_timeout=10, options=options)


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m swarm")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("once", "run"):
        s = sub.add_parser(name)
        s.add_argument("--window", type=duration_h, default=24.0)
        s.add_argument("--min-accounts", type=int, default=5)
        s.add_argument("--file-min-accounts", type=int, default=3)
        s.add_argument("--limit", type=int, default=None, help="score at most N groups")
        s.add_argument("--database-url", default=None)
        s.add_argument("--enrich-budget", type=int, default=400)
        s.add_argument("--hash-budget", type=int, default=2000)
        s.add_argument("--vision-budget", type=int, default=20)
        s.add_argument(
            "--no-labels",
            action="store_true",
            help="skip the AppView label check (every group is then held off the wall)",
        )
        s.add_argument(
            "--wall-without-vision",
            action="store_true",
            default=env_flag("SWARM_WALL_WITHOUT_VISION"),
            help="let groups reach the wall without a vision read (default off)",
        )
        if name == "once":
            s.add_argument("--dry-run", action="store_true")
        else:
            s.add_argument("--every", type=duration_h, default=5 / 60)
    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = LiveConfig(
        window_h=args.window,
        min_accounts=args.min_accounts,
        file_min_accounts=min(args.file_min_accounts, args.min_accounts),
        max_groups=args.limit,
        enrich_budget=args.enrich_budget,
        hash_budget=args.hash_budget,
        vision_budget=args.vision_budget,
        wall_without_vision=args.wall_without_vision,
    )
    dry_run = getattr(args, "dry_run", False)
    url = args.database_url or get_settings().database_url
    vision = None
    if cfg.vision_budget > 0:
        try:
            vision = nemotron_reader()
        except Exception:
            log.exception("vision reader unavailable")
    log.info(
        "vision %s; wall %s",
        "on" if vision else "off",
        "without vision allowed" if cfg.wall_without_vision else "needs a vision read",
    )
    extra = {"labels": None} if args.no_labels else {}  # default: the public AppView

    if args.cmd == "once":
        with open_connection(url, read_only=dry_run) as conn:
            res = run_pass(
                conn,
                cfg,
                dry_run=dry_run,
                vision=vision,
                hash_cache=MemoryHashCache() if dry_run else DbHashCache(conn),
                **extra,
            )
        if dry_run:
            groups = sorted(
                res.groups, key=lambda g: (-(g.report.score if g.report else 0), -len(g.dids))
            )
            for g in groups:
                print(describe(g))
        print(json.dumps(res.stats, default=str))
        return

    # run: one connection for the worker's life, reopened after a Postgres restart
    from signals.__main__ import live_connection

    def connect() -> psycopg.Connection:
        return open_connection(url, read_only=False)

    conn: psycopg.Connection | None = None
    every_s = max(30.0, args.every * 3600)
    while True:
        t0 = time.monotonic()
        try:
            conn = connect() if conn is None else live_connection(conn, connect)
            res = run_pass(conn, cfg, vision=vision, hash_cache=DbHashCache(conn), **extra)
            log.info("pass %s", json.dumps(res.stats, default=str))
        except Exception:
            log.exception("swarm pass failed")
        time.sleep(max(5.0, every_s - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
