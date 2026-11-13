"""Read-only audit of detector provenance; never invent versions for historic rows."""

import argparse
import json


def audit_versions(conn) -> dict:
    clusters = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE detector_version IS NULL "
        "OR btrim(detector_version) = ''), count(*) FILTER ("
        "WHERE signals->>'detector_version' IS DISTINCT FROM detector_version) FROM clusters"
    ).fetchone()
    runs = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE details->>'detector_version' IS NULL "
        "OR btrim(details->>'detector_version') = '') FROM eval_runs"
    ).fetchone()
    return {
        "clusters": clusters[0],
        "clusters_missing_version": clusters[1],
        "clusters_signal_version_mismatch": clusters[2],
        "eval_runs": runs[0],
        "eval_runs_missing_version": runs[1],
    }


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database-url")


def main_args(args) -> int:
    import psycopg

    from common.config import get_settings

    with psycopg.connect(
        args.database_url or get_settings().database_url, connect_timeout=5
    ) as conn:
        result = audit_versions(conn)
    print(json.dumps(result, sort_keys=True))
    return int(
        any(
            result[key]
            for key in (
                "clusters_missing_version",
                "clusters_signal_version_mismatch",
                "eval_runs_missing_version",
            )
        )
    )
