"""Database reads and writes for media/. Reads tables that signals/ fills (signals/README.md)."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from media.nemotron import PROMPT_VERSION


def candidate_clusters(
    conn: psycopg.Connection,
    *,
    statuses: list[str],
    min_score: float,
    limit: int,
    done: dict[int, Any] | None = None,
) -> list[dict[str, Any]]:
    """Clusters worth reading media for: detector status in `statuses` (in priority order),
    score >= min_score, and at least one live post with media. Most recently changed first.
    `done` ({cluster_id: updated_at} already fully read) is excluded before the limit, so read
    clusters don't use up the batch while their updated_at is unchanged."""
    done = done or {}
    return conn.execute(
        """
        SELECT c.id, c.status, c.score, c.updated_at FROM clusters c
        WHERE c.status = ANY(%(statuses)s) AND c.score >= %(min_score)s
          AND EXISTS (
            SELECT 1 FROM cluster_posts cp
            JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
            WHERE cp.cluster_id = c.id AND s.deleted_at IS NULL
              AND cardinality(s.media_hashes) > 0
          )
          AND NOT EXISTS (
            SELECT 1 FROM unnest(%(done_ids)s::bigint[], %(done_at)s::timestamptz[]) AS d(id, at)
            WHERE d.id = c.id AND d.at = c.updated_at
          )
        ORDER BY array_position(%(statuses)s, c.status), c.updated_at DESC, c.id DESC
        LIMIT %(limit)s
        """,
        {
            "statuses": statuses,
            "min_score": min_score,
            "limit": limit,
            "done_ids": list(done),
            "done_at": list(done.values()),
        },
    ).fetchall()


def cluster_media_posts(conn: psycopg.Connection, cluster_id: int) -> list[dict[str, Any]]:
    """One row per (post, media hash) in the cluster."""
    return conn.execute(
        """
        SELECT s.uri AS post_uri, s.did, s.embed_kind, s.text, h.media_hash, a.pds_host
        FROM cluster_posts cp
        JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
        CROSS JOIN LATERAL unnest(s.media_hashes) AS h(media_hash)
        LEFT JOIN accounts a ON a.did = s.did
        WHERE cp.cluster_id = %s AND s.deleted_at IS NULL
        ORDER BY s.seen_at, s.uri
        """,
        (cluster_id,),
    ).fetchall()


def stored_media(conn: psycopg.Connection, cluster_id: int) -> dict[str, dict[str, Any]]:
    """{media_hash: {id, detail}} of the cluster's media evidence."""
    rows = conn.execute(
        "SELECT id, detail FROM evidence WHERE cluster_id = %s AND kind = 'media'",
        (cluster_id,),
    ).fetchall()
    stored = {}
    for row in rows:
        detail = row.get("detail")
        media_hash = detail.get("media_hash") if isinstance(detail, dict) else None
        if isinstance(media_hash, str) and media_hash.strip():
            stored[media_hash] = row
    return stored


def cached_analysis(conn: psycopg.Connection, media_hash: str) -> dict[str, Any] | None:
    """The newest stored analysis of this exact blob (any cluster) from the current prompt.

    A row whose screenshot read failed comes last: rows are updated in place (ids don't move), so
    a newer-id failure must not hide an older row that has since been read."""
    row = conn.execute(
        """
        SELECT detail FROM evidence
        WHERE kind = 'media' AND detail->>'media_hash' = %s AND detail->>'prompt_version' = %s
        ORDER BY coalesce(detail->>'screenshot_status', '') = 'failed', id DESC LIMIT 1
        """,
        (media_hash, PROMPT_VERSION),
    ).fetchone()
    return row["detail"] if row else None


def save_media_evidence(
    conn: psycopg.Connection,
    cluster_id: int,
    *,
    summary: str,
    detail: dict[str, Any],
    source_url: str | None,
    model: str | None,
    existing: dict[str, Any] | None,
) -> tuple[int, bool]:
    """Upsert by media_hash so ids stay stable for analyst citations. Returns (id, changed).

    Unchanged rows are left alone (no created_at bump), so a re-run doesn't trigger new verdicts.
    """
    if existing is not None:
        if _comparable(existing["detail"]) == _comparable(detail):
            return existing["id"], False
        conn.execute(
            """
            UPDATE evidence SET summary = %s, detail = %s, source_url = %s, model = %s,
                created_at = now()
            WHERE id = %s
            """,
            (summary, Jsonb(detail), source_url, model, existing["id"]),
        )
        return existing["id"], True
    row = conn.execute(
        """
        INSERT INTO evidence (cluster_id, kind, summary, detail, source_url, model)
        VALUES (%s, 'media', %s, %s, %s, %s) RETURNING id
        """,
        (cluster_id, summary, Jsonb(detail), source_url, model),
    ).fetchone()
    return row["id"], True


def refresh_media_detail(
    conn: psycopg.Connection, evidence_id: int, detail: dict[str, Any]
) -> None:
    """Store a detail whose comparable part is unchanged (new timings, counts, checked_at)
    without bumping created_at, so it doesn't count as new evidence for the analyst."""
    conn.execute("UPDATE evidence SET detail = %s WHERE id = %s", (Jsonb(detail), evidence_id))


def post_created_at(conn: psycopg.Connection, uris: list[str]) -> dict[str, Any]:
    """{uri: created_at} of snapshot posts (client clock; None when unknown)."""
    rows = conn.execute(
        "SELECT uri, created_at FROM cluster_posts_snapshot WHERE uri = ANY(%s)",
        (list(dict.fromkeys(uris)),),
    ).fetchall()
    return {r["uri"]: r["created_at"] for r in rows}


# screenshot_receipt rows (media/screenshot_receipts.py): what a re-check may change without
# the answer changing. Counts drift as people like and repost; timings and receipt ids vary.
_RECEIPT_VOLATILE = {"checked_at", "fetches"}
_RECEIPT_INNER_VOLATILE = {"counts_now", "latency_ms", "receipt_ids", "candidates_checked"}


def _comparable(detail: dict[str, Any]) -> dict[str, Any]:
    """Detail minus fields that differ between a fresh call and a cache hit."""
    skip = {"latency_ms", "usage", "cached", "analyzed_at", "finish_reason", "source_uri"}
    out = {k: v for k, v in detail.items() if k not in skip}
    if isinstance(out.get("screenshot"), dict):
        out["screenshot"] = {k: v for k, v in out["screenshot"].items() if k != "latency_ms"}
    if detail.get("type") == "screenshot_receipt":
        out = {k: v for k, v in out.items() if k not in _RECEIPT_VOLATILE}
        if isinstance(out.get("receipt"), dict):
            out["receipt"] = {
                k: v for k, v in out["receipt"].items() if k not in _RECEIPT_INNER_VOLATILE
            }
    return out
