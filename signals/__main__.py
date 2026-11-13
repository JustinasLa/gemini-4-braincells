"""Signals worker and one-off runs.

python -m signals run                  # loop: 10m window every minute, 1h window every 5 min
python -m signals once --window 10m    # one detection pass, prints stats (writes to DB)
python -m signals once --window 10m --dry-run
"""

import argparse
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta

import psycopg

from common.config import get_settings
from signals.accounts import load_account_info
from signals.config import DEFAULT, DetectorConfig
from signals.detect import detect
from signals.enrich import enrich
from signals.keys import MinHashCache
from signals.semantic import GeminiEmbedder
from signals.store import store_clusters, worth_storing
from signals.window import load_window

log = logging.getLogger("signals")
LAG_GUARD = timedelta(seconds=15)  # don't look at the last seconds: ingest may still be writing
MIN_STORE_SCORE = float(os.environ.get("SIGNALS_MIN_STORE_SCORE", "0.5"))


def run_once(
    conn: psycopg.Connection,
    window_kind: str,
    cfg: DetectorConfig,
    mh_cache: MinHashCache | None = None,
    end: datetime | None = None,
    dry_run: bool = False,
    embed=None,
    enrich_budget: int = 400,
) -> dict:
    length = timedelta(seconds=cfg.windows[window_kind])
    end = (end or datetime.now(UTC) - LAG_GUARD).replace(microsecond=0)
    start = end - length
    t0 = time.monotonic()
    wd = load_window(conn, start, end, cfg)
    t_load = time.monotonic() - t0

    def account_info(dids: list[str]):
        if not dry_run:
            try:
                enrich(conn, dids, budget=enrich_budget)
            except Exception:
                log.exception("enrichment failed; continuing without it")
        return load_account_info(conn, dids, now=end)

    res = detect(wd, account_info, cfg, mh_cache, embed=embed)
    keep = [c for c in res.clusters if worth_storing(c, cfg, MIN_STORE_SCORE)]
    ids = [] if dry_run else store_clusters(conn, keep, window_kind, start, end, cfg)
    stats = {
        **res.stats,
        "window": window_kind,
        "end": end.isoformat(),
        "load_s": round(t_load, 2),
        "stored": len(ids),
        "total_s": round(time.monotonic() - t0, 2),
    }
    return stats


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m signals")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    o = sub.add_parser("once")
    o.add_argument("--window", default="10m", choices=list(DEFAULT.windows))
    o.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--no-embeddings",
        action="store_true",
        help="don't use Gemini embeddings even if GEMINI_API_KEY is set",
    )
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpx2", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = DEFAULT
    mh = MinHashCache(cfg.minhash_perm)
    embed = None
    if not args.no_embeddings and os.environ.get("GEMINI_API_KEY"):
        embed = GeminiEmbedder()
        log.info("Gemini embeddings on (%s)", embed.model)
    else:
        log.info("Gemini embeddings off")
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        if args.cmd == "once":
            print(
                json.dumps(run_once(conn, args.window, cfg, mh, dry_run=args.dry_run, embed=embed))
            )
            return
        last = {k: 0.0 for k in cfg.windows}
        every = {"10m": 60, "1h": 300}
        while True:
            for kind in cfg.windows:
                if time.monotonic() - last[kind] >= every.get(kind, 300):
                    last[kind] = time.monotonic()
                    try:
                        log.info("pass %s", json.dumps(run_once(conn, kind, cfg, mh, embed=embed)))
                    except Exception:
                        log.exception("detection pass %s failed", kind)
            time.sleep(5)


if __name__ == "__main__":
    main()
