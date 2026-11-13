"""CLI for analyst/.

python -m analyst prompt 42                 print the prompt for cluster 42 (no model call)
python -m analyst cluster 42 [--dry-run]    write (or just print) a verdict for cluster 42
python -m analyst worker [--interval 30]    keep verdicts fresh as clusters and evidence change

Verdicts for candidate clusters use Gemini function calling into sources/ (analyst/agent.py);
--no-tools falls back to the single-shot verdict. --dry-run writes nothing (tool evidence too).
"""

import argparse
import logging
import sys
import time

from analyst import store
from analyst.gemini import GeminiVerdictModel, VerdictModel
from analyst.prompt import SYSTEM, build_prompt
from analyst.runner import Analyst
from analyst.service import analyze
from common.config import get_settings
from common.db import connect

log = logging.getLogger("analyst")


def run_cluster(conn, model: VerdictModel | Analyst, cluster_id: int, *, dry_run: bool = False):
    ctx = store.load_context(conn, cluster_id)
    if ctx is None:
        log.warning("cluster %s not found", cluster_id)
        return None
    if isinstance(model, Analyst):
        verdict = model.verdict(conn, ctx, dry_run=dry_run)
    else:
        verdict = analyze(ctx, model)
    if not dry_run:
        store.save_verdict(conn, cluster_id, verdict)
        conn.commit()
    log.info(
        "cluster %s: detector=%s llm=%s agrees=%s confidence=%.2f adjustments=%d tool_calls=%d "
        "cost_usd=%s model=%s",
        cluster_id,
        ctx.status,
        verdict.llm_assessment,
        verdict.agrees_with_detector,
        verdict.confidence,
        len(verdict.adjustments),
        len(verdict.tool_calls),
        verdict.cost_usd,
        verdict.model,
    )
    return verdict


def run_worker(
    model: VerdictModel | Analyst,
    *,
    interval_s: float,
    min_score: float,
    settle_s: float,
    refresh_s: float,
    batch: int,
    failure_backoff_s: float = 600,
) -> None:
    failed: dict[int, float] = {}  # cluster_id -> monotonic time it may be retried
    while True:
        now = time.monotonic()
        failed = {cid: t for cid, t in failed.items() if t > now}
        try:
            with connect() as conn:
                for cluster_id in store.clusters_needing_verdict(
                    conn,
                    min_score=min_score,
                    settle_s=settle_s,
                    refresh_s=refresh_s,
                    limit=batch,
                    exclude=failed.keys(),
                ):
                    try:
                        run_cluster(conn, model, cluster_id)
                    except Exception:
                        conn.rollback()
                        failed[cluster_id] = time.monotonic() + failure_backoff_s
                        log.exception("verdict for cluster %s failed; retry later", cluster_id)
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
    s.add_argument("--no-tools", action="store_true", help="single-shot verdict, no tool calls")
    s = sub.add_parser("worker")
    s.add_argument("--no-tools", action="store_true", help="single-shot verdicts, no tool calls")
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

    try:
        model = _make_model(no_tools=args.no_tools)
    except ValueError as exc:
        if args.cmd != "worker":
            sys.exit(str(exc))
        log.warning("%s; analyst worker idle until the key is configured and it restarts", exc)
        while True:
            time.sleep(3600)
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


def _make_model(*, no_tools: bool) -> VerdictModel | Analyst:
    if no_tools or not get_settings().analyst_tools:
        return GeminiVerdictModel.from_settings()
    web = None
    try:
        from sources.oxylabs import OxylabsClient

        web = OxylabsClient.from_settings()
    except ValueError as exc:
        log.warning("%s; analyst runs with DB lookups only (no web tools)", exc)
    return Analyst.from_settings(web)


if __name__ == "__main__":
    main()
