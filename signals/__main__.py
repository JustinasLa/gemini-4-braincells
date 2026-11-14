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
from signals.baseline import load_rates, sample_and_enrich
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
    if end is None:
        # Never look past what ingest has written: when ingest falls behind (e.g. a CPU-starved
        # host), "now - 15 s" would cut a window whose last minutes aren't in the database yet.
        end = datetime.now(UTC) - LAG_GUARD
        row = conn.execute(
            "SELECT last_event_at FROM ingest_state WHERE stream = 'jetstream'"
        ).fetchone()
        if row and row[0] and row[0] < end:
            end = row[0]
    end = end.replace(microsecond=0)
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

    rates = None
    try:
        if not dry_run:
            sample_and_enrich(conn, wd, end, cfg.baseline_per_pass)
        rates = load_rates(conn, end, cfg)
    except Exception:
        log.exception("baseline sampling failed; identity test off for this pass")
    res = detect(wd, account_info, cfg, mh_cache, embed=embed, base_rates=rates)
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
    b = sub.add_parser("baseline", help="sample and enrich N random active accounts now")
    b.add_argument("--n", type=int, default=300)
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
    # One cache per window kind: they hold different posts, and sharing one would make each
    # kind evict the other's posts from the incremental LSH index on every pass.
    mh = {kind: MinHashCache(cfg.minhash_perm) for kind in cfg.windows}
    embed = None
    if not args.no_embeddings and os.environ.get("GEMINI_API_KEY"):
        embed = GeminiEmbedder()
        log.info("Gemini embeddings on (%s)", embed.model)
    else:
        log.info("Gemini embeddings off")
    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        if args.cmd == "baseline":
            end = datetime.now(UTC).replace(microsecond=0) - LAG_GUARD
            wd = load_window(conn, end - timedelta(minutes=10), end, cfg)
            n = sample_and_enrich(conn, wd, end, args.n)
            print(
                json.dumps(
                    {
                        "enriched": n,
                        "rates": (load_rates(conn, end, cfg) or None)
                        and load_rates(conn, end, cfg).as_dict(),
                    }
                )
            )
            return
        if args.cmd == "once":
            print(
                json.dumps(
                    run_once(
                        conn, args.window, cfg, mh[args.window], dry_run=args.dry_run, embed=embed
                    )
                )
            )
            return
        last = {k: 0.0 for k in cfg.windows}
        every = {"10m": 60, "1h": 300}
        while True:
            for kind in cfg.windows:
                if time.monotonic() - last[kind] >= every.get(kind, 300):
                    last[kind] = time.monotonic()
                    try:
                        log.info(
                            "pass %s", json.dumps(run_once(conn, kind, cfg, mh[kind], embed=embed))
                        )
                    except Exception:
                        log.exception("detection pass %s failed", kind)
            time.sleep(5)


if __name__ == "__main__":
    main()
