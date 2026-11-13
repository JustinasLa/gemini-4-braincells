"""Write eval results to `eval_runs` (read by GET /api/metrics: latest row per dataset + split)."""

import psycopg
from psycopg.types.json import Jsonb

SPLITS = ("dev", "heldout")


def write_eval_run(
    database_url: str,
    dataset: str,
    split: str,
    *,
    precision: float | None = None,
    recall: float | None = None,
    f1: float | None = None,
    fpr: float | None = None,
    n: int | None = None,
    details: dict | None = None,
) -> int:
    """Insert one eval_runs row and return its id. Metrics that a dataset can't measure stay
    NULL (never 0): the dashboard must not show a number nobody measured."""
    if split not in SPLITS:
        raise ValueError(f"split must be one of {SPLITS}, got {split!r}")
    with psycopg.connect(database_url, autocommit=True) as conn:
        row = conn.execute(
            """
            INSERT INTO eval_runs (dataset, split, precision, recall, f1, fpr, n, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (dataset, split, precision, recall, f1, fpr, n, Jsonb(details or {})),
        ).fetchone()
    return row[0]
