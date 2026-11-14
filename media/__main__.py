"""CLI for media/.

python -m media model                         show the served model (id, root) and check it is VL
python -m media cluster 42 [--dry-run]        read one cluster's media and store the evidence
python -m media worker [--interval 60]        keep reading media in candidate clusters
"""

import argparse
import json
import logging
import sys
import time

from common.config import get_settings
from common.db import connect
from media import store
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
    conn, client, fetcher, cluster_id: int, *, max_media: int, backoff=None, dry_run: bool = False
):
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
    changed = 0 if dry_run else persist_evidence(conn, cluster_id, run)
    if not dry_run:
        conn.commit()
    log.info(
        "cluster %s: %d media posts, %d findings (%d cached), %d failed, %d screenshots read as "
        "posts, %d screenshot reads failed, %d over cap, %d rows changed",
        cluster_id,
        len(posts),
        len(run.findings),
        sum(f.cached for f in run.findings),
        len(run.failures),
        sum(f.screenshot_status == "post" for f in run.findings),
        run.screenshot_failures,
        run.skipped,
        changed,
    )
    return run


def run_worker(client, settings, *, interval_s: float, batch: int) -> None:
    statuses = [s.strip() for s in settings.media_statuses.split(",") if s.strip()]
    progress = ClusterProgress()
    backoff = FailureBackoff()
    with BlobFetcher() as fetcher:
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
            with connect() as conn, BlobFetcher() as fetcher:
                run = read_cluster(
                    conn,
                    client,
                    fetcher,
                    args.cluster_id,
                    max_media=settings.media_max_per_cluster,
                    dry_run=args.dry_run,
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
