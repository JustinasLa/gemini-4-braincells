"""Database reads and writes for sources/. Reads tables that signals/ fills (signals/README.md)."""

from collections.abc import Iterable

import psycopg
from psycopg.types.json import Jsonb

from sources.models import ClusterPost, SourceEvidence


def load_cluster_posts(
    conn: psycopg.Connection, cluster_id: int, limit: int = 1000
) -> list[ClusterPost]:
    rows = conn.execute(
        """
        SELECT s.did, s.text, coalesce(s.embed_urls, '{}') AS embed_urls
        FROM cluster_posts cp
        JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
        WHERE cp.cluster_id = %s AND s.deleted_at IS NULL
        LIMIT %s
        """,
        (cluster_id, limit),
    ).fetchall()
    return [ClusterPost(did=r["did"], text=r["text"], embed_urls=r["embed_urls"]) for r in rows]


def stored_keys(conn: psycopg.Connection, cluster_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT detail->>'key' AS key FROM evidence WHERE cluster_id = %s AND kind = 'source'",
        (cluster_id,),
    ).fetchall()
    return {r["key"] for r in rows if r["key"]}


def save_evidence(
    conn: psycopg.Connection, cluster_id: int, items: Iterable[SourceEvidence]
) -> list[int]:
    """Upsert by detail.key so evidence ids stay stable for analyst citations."""
    ids = []
    for ev in items:
        row = conn.execute(
            """
            UPDATE evidence SET summary = %s, detail = %s, source_url = %s, model = %s,
                created_at = now()
            WHERE cluster_id = %s AND kind = 'source' AND detail->>'key' = %s
            RETURNING id
            """,
            (ev.summary, Jsonb(ev.detail), ev.source_url, ev.model, cluster_id, ev.detail["key"]),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                INSERT INTO evidence (cluster_id, kind, summary, detail, source_url, model)
                VALUES (%s, 'source', %s, %s, %s, %s) RETURNING id
                """,
                (cluster_id, ev.summary, Jsonb(ev.detail), ev.source_url, ev.model),
            ).fetchone()
        ids.append(row["id"])
    return ids


def clusters_to_check(
    conn: psycopg.Connection, *, min_score: float, limit: int
) -> list[tuple[int, object]]:
    """[(cluster_id, updated_at)] for the most recently changed clusters worth a source check."""
    rows = conn.execute(
        """
        SELECT id, updated_at FROM clusters
        WHERE score >= %s
        ORDER BY updated_at DESC
        LIMIT %s
        """,
        (min_score, limit),
    ).fetchall()
    return [(r["id"], r["updated_at"]) for r in rows]
