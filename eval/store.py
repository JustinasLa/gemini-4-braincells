"""The one writer for `eval_runs` (db/schema.sql). `/api/metrics` serves the latest row per
(dataset, split), and the dashboard shows it, so every number there comes from a row this wrote."""

import json

import psycopg


def write_eval_run(
    dataset: str,
    split: str,
    *,
    n: int,
    details: dict,
    precision: float | None = None,
    recall: float | None = None,
    f1: float | None = None,
    fpr: float | None = None,
    database_url: str | None = None,
    conn: psycopg.Connection | None = None,
) -> int:
    """Insert one eval run and return its id. Pass an open `conn`, or a `database_url`
    (default: DATABASE_URL from the environment / .env)."""
    sql = """
        INSERT INTO eval_runs (dataset, split, precision, recall, f1, fpr, n, details)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb) RETURNING id
    """
    args = (dataset, split, precision, recall, f1, fpr, n, json.dumps(details))
    if conn is not None:
        return conn.execute(sql, args).fetchone()[0]
    if database_url is None:
        from common.config import get_settings

        database_url = get_settings().database_url
    with psycopg.connect(database_url, connect_timeout=5) as c:
        rid = c.execute(sql, args).fetchone()[0]
        c.commit()
        return rid
