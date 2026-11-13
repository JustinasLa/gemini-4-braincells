"""Database reads and writes for analyst/."""

from collections.abc import Collection

import psycopg
from psycopg.types.json import Jsonb

from analyst.context import ClusterContext, EvidenceItem
from analyst.verdict import Verdict


def load_context(
    conn: psycopg.Connection, cluster_id: int, *, max_posts: int = 8
) -> ClusterContext | None:
    c = conn.execute(
        """
        SELECT id, status, score, confidence, p_value, size, window_start, window_end,
               synthetic, updated_at, signals
        FROM clusters WHERE id = %s
        """,
        (cluster_id,),
    ).fetchone()
    if c is None:
        return None
    evidence = [
        EvidenceItem(
            id=r["id"],
            kind=r["kind"],
            summary=r["summary"] or "",
            detail=r["detail"] or {},
            source_url=r["source_url"],
            model=r["model"],
        )
        for r in conn.execute(
            """
            SELECT id, kind, summary, detail, source_url, model FROM evidence
            WHERE cluster_id = %s AND kind <> 'analyst'
            ORDER BY kind, id
            """,
            (cluster_id,),
        ).fetchall()
    ]
    posts = conn.execute(
        """
        SELECT s.text FROM cluster_posts cp
        JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
        WHERE cp.cluster_id = %s AND s.text IS NOT NULL AND s.deleted_at IS NULL
        ORDER BY s.seen_at
        LIMIT %s
        """,
        (cluster_id, max_posts),
    ).fetchall()
    return ClusterContext(
        id=c["id"],
        status=c["status"],
        score=c["score"],
        confidence=c["confidence"],
        p_value=c["p_value"],
        size=c["size"],
        window_start=c["window_start"],
        window_end=c["window_end"],
        synthetic=c["synthetic"],
        updated_at=c["updated_at"],
        signals=c["signals"] or {},
        evidence=evidence,
        sample_posts=[p["text"] for p in posts],
    )


def save_verdict(conn: psycopg.Connection, cluster_id: int, v: Verdict) -> int:
    """Append a new analyst row; readers take the latest. Old verdicts stay as history."""
    return conn.execute(
        """
        INSERT INTO evidence (cluster_id, kind, summary, detail, source_url, model)
        VALUES (%s, 'analyst', %s, %s, NULL, %s) RETURNING id
        """,
        (cluster_id, v.summary, Jsonb(v.model_dump(mode="json")), v.model),
    ).fetchone()["id"]


def clusters_needing_verdict(
    conn: psycopg.Connection,
    *,
    min_score: float,
    settle_s: float,
    refresh_s: float,
    limit: int,
    exclude: Collection[int] = (),
) -> list[int]:
    """Clusters that need a (new) verdict, skipping `exclude` (e.g. ones that just failed).

    First verdict: once the cluster is `settle_s` old (counted from creation: signals touches
    active clusters every pass), so sources and media can land first. Refresh: when something
    changed after the last verdict, at most every `refresh_s`.
    """
    rows = conn.execute(
        """
        WITH changed AS (
            SELECT c.id, c.created_at,
                   greatest(c.updated_at, max(e.created_at) FILTER (WHERE e.kind <> 'analyst'))
                       AS changed_at,
                   max(e.created_at) FILTER (WHERE e.kind = 'analyst') AS verdict_at
            FROM clusters c LEFT JOIN evidence e ON e.cluster_id = c.id
            WHERE c.score >= %(min_score)s AND NOT (c.id = ANY(%(exclude)s))
            GROUP BY c.id
            HAVING count(e.id) FILTER (WHERE e.kind <> 'analyst') > 0  -- nothing to cite yet
        )
        SELECT id FROM changed
        WHERE (verdict_at IS NULL AND created_at < now() - make_interval(secs => %(settle)s))
           OR (verdict_at < changed_at AND verdict_at < now() - make_interval(secs => %(refresh)s))
        ORDER BY verdict_at NULLS FIRST, changed_at DESC
        LIMIT %(limit)s
        """,
        {
            "min_score": min_score,
            "settle": settle_s,
            "refresh": refresh_s,
            "limit": limit,
            "exclude": list(exclude),
        },
    ).fetchall()
    return [r["id"] for r in rows]
