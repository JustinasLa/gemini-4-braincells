"""CLI for media/.

python -m media model                         show the served model (id, root) and check it is VL
python -m media cluster 42 [--dry-run]        read one cluster's media and store the evidence
python -m media worker [--interval 60]        keep reading media in candidate clusters
"""

import argparse
import contextlib
import json
import logging
import sys
import time

from common.config import get_settings
from common.db import connect
from media import screenshot_receipts, store
from media.blobs import BlobFetcher
from media.nemotron import NemotronClient, NotVisionModelError
from media.worker import (
    ClusterProgress,
    FailureBackoff,
    load_cluster_media,
    persist_evidence,
    process_cluster_media,
)

log = logging.getLogger("media")


def read_cluster(
    conn,
    client,
    fetcher,
    cluster_id: int,
    *,
    max_media: int,
    backoff=None,
    dry_run: bool = False,
    receipts=None,
):
    """`receipts` (a screenshot_receipts.DepsFactory, None = off) checks screenshots of posts
    against the real post; dry_run still runs every check but stores nothing."""
    posts = load_cluster_media(conn, cluster_id)
    run = process_cluster_media(
        posts,
        client,
        fetcher,
        max_media=max_media,
        cache_lookup=lambda h: store.cached_analysis(conn, h),
        skip=backoff,
    )
    if backoff is not None:
        for failure in run.failures:
            backoff.failed(failure.media_hash)
        for f in run.findings:
            if f.screenshot_status == "failed":  # analysis stored; read the screenshot later
                backoff.failed(f.group.media_hash)
    shots, existing = [], {}
    if receipts is not None and run.findings:
        try:
            existing = store.stored_media(conn, cluster_id)
            shots = screenshot_receipts.receipt_rows(
                run.findings,
                receipts,
                existing=existing,
                created_at=screenshot_receipts.post_times(conn, run.findings),
            )
        except Exception:  # never costs the cluster its media evidence
            log.exception("cluster %s: screenshot receipts failed", cluster_id)
    changed = 0 if dry_run else persist_evidence(conn, cluster_id, run)
    if shots and not dry_run:
        changed += screenshot_receipts.persist_receipt_rows(conn, cluster_id, shots, existing)
    for row in shots:
        log.info("cluster %s: %s", cluster_id, row["summary"])
    if not dry_run:
        conn.commit()
    log.info(
        "cluster %s: %d media posts, %d findings (%d cached), %d failed, %d screenshots read as "
        "posts, %d screenshot reads failed, %d over cap, %d screenshot receipts, %d rows changed",
        cluster_id,
        len(posts),
        len(run.findings),
        sum(f.cached for f in run.findings),
        len(run.failures),
        sum(f.screenshot_status == "post" for f in run.findings),
        run.screenshot_failures,
        run.skipped,
        len(shots),
        changed,
    )
    return run


def _receipt_clients(settings):
    """Shared clients for screenshot receipts; a null context when MEDIA_SCREENSHOT_RECEIPTS=0."""
    if not screenshot_receipts.enabled():
        return contextlib.nullcontext(None)
    return screenshot_receipts.ReceiptClients.from_settings(settings)


def run_worker(client, settings, *, interval_s: float, batch: int) -> None:
    statuses = [s.strip() for s in settings.media_statuses.split(",") if s.strip()]
    progress = ClusterProgress(
        recheck_s=screenshot_receipts.RECHECK_S if screenshot_receipts.enabled() else None
    )
    backoff = FailureBackoff()
    with BlobFetcher() as fetcher, _receipt_clients(settings) as receipts:
        while True:
            try:
                with connect() as conn:
                    for c in store.candidate_clusters(
                        conn,
                        statuses=statuses,
                        min_score=settings.media_min_score,
                        limit=batch,
                        done=progress.done(),
                    ):
                        run = read_cluster(
                            conn,
                            client,
                            fetcher,
                            c["id"],
                            max_media=settings.media_max_per_cluster,
                            backoff=backoff,
                            receipts=receipts,
                        )
                        progress.record(c["id"], c["updated_at"], run)
            except NotVisionModelError:
                raise
            except Exception:
                log.exception("media worker pass failed; retrying next interval")
            time.sleep(interval_s)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="python -m media")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("model")
    s = sub.add_parser("cluster")
    s.add_argument("cluster_id", type=int)
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("worker")
    s.add_argument("--interval", type=float, default=60)
    s.add_argument("--batch", type=int, default=25)
    args = p.parse_args()

    settings = get_settings()
    try:
        client = NemotronClient.from_settings(settings)
    except ValueError as exc:
        if args.cmd != "worker":
            sys.exit(str(exc))
        log.warning("%s; media worker idle until a key is configured and it restarts", exc)
        while True:
            time.sleep(3600)

    try:
        info = client.describe_model()
        if args.cmd == "model":
            print(json.dumps(info, indent=2))
        elif args.cmd == "cluster":
            with connect() as conn, BlobFetcher() as fetcher, _receipt_clients(settings) as rc:
                run = read_cluster(
                    conn,
                    client,
                    fetcher,
                    args.cluster_id,
                    max_media=settings.media_max_per_cluster,
                    dry_run=args.dry_run,
                    receipts=rc,
                )
            print(
                json.dumps(
                    [f.detail() | {"summary": f.summary()} for f in run.findings],
                    indent=2,
                    default=str,
                )
            )
        else:
            run_worker(client, settings, interval_s=args.interval, batch=args.batch)
    except NotVisionModelError as exc:
        log.critical("refusing to store media findings: %s", exc)
        sys.exit(2)


if __name__ == "__main__":
    main()
