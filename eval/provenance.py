"""Read-only audit of detector provenance; never invent versions for historic rows."""

import argparse
import json


def _has_version(version, versions) -> bool:
    def known(value) -> bool:
        return isinstance(value, str) and bool(value.strip()) and value.strip() != "null"

    if known(version):
        return True
    # Hand labels store a version-to-count map; moderation evals store a list.
    if isinstance(versions, dict):
        return bool(versions) and all(
            known(key) and type(count) is int and count > 0 for key, count in versions.items()
        )
    return isinstance(versions, list) and bool(versions) and all(known(v) for v in versions)


def audit_versions(conn) -> dict:
    clusters = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE detector_version IS NULL "
        "OR btrim(detector_version) = ''), count(*) FILTER ("
        "WHERE signals->>'detector_version' IS DISTINCT FROM detector_version) FROM clusters"
    ).fetchone()
    runs = missing = 0
    for version, versions in conn.execute(
        "SELECT details->'detector_version', details->'detector_versions' FROM eval_runs"
    ):
        runs += 1
        missing += not _has_version(version, versions)
    return {
        "clusters": clusters[0],
        "clusters_missing_version": clusters[1],
        "clusters_signal_version_mismatch": clusters[2],
        "eval_runs": runs,
        "eval_runs_missing_version": missing,
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
