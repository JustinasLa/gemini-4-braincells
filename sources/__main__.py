"""CLI for sources/.

python -m sources search '"exact phrase"'     one live Oxylabs search, printed as JSON
python -m sources scrape https://example.com  one live scrape with fact extraction
python -m sources cluster 42 [--dry-run]      check one cluster and store its source evidence
python -m sources worker [--interval 60]      keep checking recently changed clusters
"""

import argparse
import json
import logging
import sys
import time

from common.db import connect
from sources import store
from sources.checker import SourceChecker
from sources.oxylabs import OxylabsClient

log = logging.getLogger("sources")


def check_cluster(conn, checker: SourceChecker, cluster_id: int, *, dry_run: bool = False) -> list:
    posts = store.load_cluster_posts(conn, cluster_id)
    items = checker.check(posts, stored=store.stored_details(conn, cluster_id))
    if not dry_run and items:
        store.save_evidence(conn, cluster_id, items)
        conn.commit()
    log.info("cluster %s: %d posts, %d new source evidence", cluster_id, len(posts), len(items))
    return items


def run_worker(checker: SourceChecker, *, interval_s: float, min_score: float, batch: int) -> None:
    while True:
        try:
            with connect() as conn:
                for cluster_id, _updated_at in store.clusters_to_check(
                    conn, min_score=min_score, limit=batch
                ):
                    # Stored evidence skips completed checks; the checker's failure TTL backs
                    # off missing ones. An unchanged cluster can still need a failed call retried.
                    check_cluster(conn, checker, cluster_id)
        except Exception:
            log.exception("sources worker pass failed; retrying next interval")
        time.sleep(interval_s)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m sources")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search")
    s.add_argument("query")
    s = sub.add_parser("scrape")
    s.add_argument("url")
    s = sub.add_parser("cluster")
    s.add_argument("cluster_id", type=int)
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("worker")
    s.add_argument("--interval", type=float, default=60)
    s.add_argument("--min-score", type=float, default=0.5)
    s.add_argument("--batch", type=int, default=10)
    args = p.parse_args()

    try:
        client = OxylabsClient.from_settings()
    except ValueError as exc:
        if args.cmd != "worker":
            sys.exit(str(exc))
        log.warning("%s; sources worker idle until the key is configured and it restarts", exc)
        while True:
            time.sleep(3600)

    with client:
        if args.cmd == "search":
            print(
                json.dumps([h.model_dump(mode="json") for h in client.search(args.query)], indent=2)
            )
        elif args.cmd == "scrape":
            print(client.scrape_facts(args.url).model_dump_json(indent=2))
        elif args.cmd == "cluster":
            with connect() as conn:
                items = check_cluster(
                    conn, SourceChecker(client), args.cluster_id, dry_run=args.dry_run
                )
            print(json.dumps([i.model_dump(mode="json") for i in items], indent=2))
        else:
            run_worker(
                SourceChecker(client),
                interval_s=args.interval,
                min_score=args.min_score,
                batch=args.batch,
            )


if __name__ == "__main__":
    main()
