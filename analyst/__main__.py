"""CLI for analyst/.

python -m analyst prompt 42                 print the prompt for cluster 42 (no model call)
python -m analyst cluster 42 [--dry-run]    write (or just print) a verdict for cluster 42
python -m analyst worker [--interval 30]    keep verdicts fresh as clusters and evidence change
"""

import argparse
import logging
import sys
import time

from analyst import store
from analyst.gemini import GeminiVerdictModel, VerdictModel
from analyst.prompt import SYSTEM, build_prompt
from analyst.service import analyze
from common.db import connect

log = logging.getLogger("analyst")


def run_cluster(conn, model: VerdictModel, cluster_id: int, *, dry_run: bool = False):
    ctx = store.load_context(conn, cluster_id)
    if ctx is None:
        log.warning("cluster %s not found", cluster_id)
        return None
    verdict = analyze(ctx, model)
    if not dry_run:
        store.save_verdict(conn, cluster_id, verdict)
        conn.commit()
    log.info(
        "cluster %s: detector=%s analyst=%s confidence=%.2f adjustments=%d",
        cluster_id,
        ctx.status,
        verdict.status,
        verdict.confidence,
        len(verdict.adjustments),
    )
    return verdict


def run_worker(
    model: VerdictModel,
    *,
    interval_s: float,
    min_score: float,
    settle_s: float,
    refresh_s: float,
    batch: int,
) -> None:
    while True:
        try:
            with connect() as conn:
                for cluster_id in store.clusters_needing_verdict(
                    conn, min_score=min_score, settle_s=settle_s, refresh_s=refresh_s, limit=batch
                ):
                    run_cluster(conn, model, cluster_id)
        except Exception:
            log.exception("analyst worker pass failed; retrying next interval")
        time.sleep(interval_s)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m analyst")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prompt")
    s.add_argument("cluster_id", type=int)
    s = sub.add_parser("cluster")
    s.add_argument("cluster_id", type=int)
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("worker")
    s.add_argument("--interval", type=float, default=30)
    s.add_argument("--min-score", type=float, default=0.5)
    s.add_argument("--settle", type=float, default=90, help="seconds before a first verdict")
    s.add_argument("--refresh", type=float, default=600, help="min seconds between verdicts")
    s.add_argument("--batch", type=int, default=5)
    args = p.parse_args()

    if args.cmd == "prompt":
        with connect() as conn:
            ctx = store.load_context(conn, args.cluster_id)
        if ctx is None:
            sys.exit(f"cluster {args.cluster_id} not found")
        print(SYSTEM, "\n\n", build_prompt(ctx))
        return

    model = GeminiVerdictModel.from_settings()
    if args.cmd == "cluster":
        with connect() as conn:
            verdict = run_cluster(conn, model, args.cluster_id, dry_run=args.dry_run)
        if verdict:
            print(verdict.model_dump_json(indent=2))
    else:
        run_worker(
            model,
            interval_s=args.interval,
            min_score=args.min_score,
            settle_s=args.settle,
            refresh_s=args.refresh,
            batch=args.batch,
        )


if __name__ == "__main__":
    main()
