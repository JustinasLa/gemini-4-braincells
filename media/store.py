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
    """The newest stored analysis of this exact blob (any cluster) from the current prompt."""
    row = conn.execute(
        """
        SELECT detail FROM evidence
        WHERE kind = 'media'
          AND detail->>'media_hash' = %s
          AND detail->>'prompt_version' = %s
          AND coalesce(detail->>'status', 'complete') = 'complete'
          AND jsonb_typeof(detail) = 'object'
          AND jsonb_typeof(detail->'description') = 'string'
          AND nullif(btrim(detail->>'description'), '') IS NOT NULL
          AND jsonb_typeof(detail->'extracted_text') = 'array'
          AND jsonb_typeof(detail->'reuse_cues') = 'array'
          AND jsonb_typeof(detail->'manipulation_cues') = 'array'
          AND jsonb_typeof(detail->'confidence') = 'number'
          AND jsonb_typeof(detail->'latency_ms') = 'number'
          AND jsonb_typeof(detail->'model') = 'string'
        ORDER BY id DESC LIMIT 1
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


def _comparable(detail: dict[str, Any]) -> dict[str, Any]:
    """Detail minus fields that differ between a fresh call and a cache hit."""
    skip = {"latency_ms", "usage", "cached", "analyzed_at", "finish_reason", "source_uri"}
    return {k: v for k, v in detail.items() if k not in skip}
