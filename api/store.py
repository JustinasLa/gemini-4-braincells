"""All SQL the API runs, in one place. Reads the tables signals/, sources/ and analyst/ write."""

import psycopg

from api.schemas import (
    ClusterDetail,
    ClusterSummary,
    EvalRun,
    Evidence,
    Member,
    Post,
    SignalBrief,
    VerdictBrief,
)

_SUMMARY_COLS = """
    c.id, c.status, c.score, c.confidence, c.p_value, c.size, c.families_fired, c.window_kind,
    c.window_start, c.window_end, c.campaign_id, c.synthetic, c.updated_at, c.signals,
    v.detail AS verdict_detail, v.created_at AS verdict_at
"""
_LATEST_VERDICT = """
    LEFT JOIN LATERAL (
        SELECT detail, created_at FROM evidence
        WHERE cluster_id = c.id AND kind = 'analyst'
        ORDER BY created_at DESC, id DESC LIMIT 1
    ) v ON true
"""


def bsky_post_url(uri: str) -> str | None:
    """at://did/app.bsky.feed.post/rkey -> https://bsky.app/profile/did/post/rkey"""
    parts = uri.removeprefix("at://").split("/") if uri.startswith("at://") else []
    if len(parts) == 3 and parts[1] == "app.bsky.feed.post":
        return f"https://bsky.app/profile/{parts[0]}/post/{parts[2]}"
    return None


def _summary(row: dict, *, n_signals: int = 3) -> ClusterSummary:
    signals = (row.get("signals") or {}).get("signals") or []
    v = row.get("verdict_detail")
    return ClusterSummary(
        **{k: row[k] for k in ClusterSummary.model_fields if k in row and k != "verdict"},
        top_signals=[SignalBrief(**s) for s in signals if s.get("polarity") == "for"][:n_signals],
        verdict=(
            VerdictBrief(
                status=v["status"],
                confidence=v.get("confidence", 0.0),
                summary=v.get("summary"),
                model=v.get("model"),
                created_at=row["verdict_at"],
            )
            if v
            else None
        ),
    )


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Store:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def list_clusters(
        self,
        *,
        status: str | None,
        min_score: float,
        limit: int,
        include_synthetic: bool,
    ) -> list[ClusterSummary]:
        rows = self.conn.execute(
            f"""
            SELECT {_SUMMARY_COLS} FROM clusters c {_LATEST_VERDICT}
            WHERE c.score >= %(min_score)s
              AND (%(status)s::text IS NULL OR c.status = %(status)s)
              AND (%(synthetic)s OR NOT c.synthetic)
            ORDER BY c.window_end DESC, c.id DESC
            LIMIT %(limit)s
            """,
            {
                "min_score": min_score,
                "status": status,
                "synthetic": include_synthetic,
                "limit": limit,
            },
        ).fetchall()
        return [_summary(r) for r in rows]

    def get_cluster(
        self, cluster_id: int, *, max_members: int = 200, max_posts: int = 25
    ) -> ClusterDetail | None:
        row = self.conn.execute(
            f"SELECT {_SUMMARY_COLS} FROM clusters c {_LATEST_VERDICT} WHERE c.id = %s",
            (cluster_id,),
        ).fetchone()
        if row is None:
            return None
        members = self.conn.execute(
            """
            SELECT m.did, m.role, a.handle, a.created_at AS account_created_at, a.followers
            FROM cluster_members m LEFT JOIN accounts a ON a.did = m.did
            WHERE m.cluster_id = %s
            ORDER BY (m.role = 'core') DESC, m.did
            LIMIT %s
            """,
            (cluster_id, max_members),
        ).fetchall()
        posts = self.conn.execute(
            """
            SELECT s.uri, s.did, s.text, s.created_at, s.seen_at, s.embed_kind,
                   coalesce(s.embed_urls, '{}') AS embed_urls,
                   coalesce(s.media_hashes, '{}') AS media_hashes
            FROM cluster_posts cp JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
            WHERE cp.cluster_id = %s AND s.deleted_at IS NULL
            ORDER BY s.seen_at
            LIMIT %s
            """,
            (cluster_id, max_posts),
        ).fetchall()
        evidence = self.conn.execute(
            """
            SELECT id, kind, summary, detail, source_url, model, created_at FROM evidence
            WHERE cluster_id = %s AND kind <> 'analyst'
            ORDER BY kind, id
            """,
            (cluster_id,),
        ).fetchall()
        summary = _summary(row)
        return ClusterDetail(
            **summary.model_dump(),
            signals=row["signals"] or {},
            members=[
                Member(**m, profile_url=f"https://bsky.app/profile/{m['did']}") for m in members
            ],
            posts=[Post(**p, url=bsky_post_url(p["uri"])) for p in posts],
            evidence=[Evidence(**e) for e in evidence],
            analyst=row["verdict_detail"],
        )

    def latest_metrics(self) -> list[EvalRun]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT ON (dataset, split)
                   id, dataset, split, precision, recall, f1, fpr, n, details, created_at
            FROM eval_runs
            ORDER BY dataset, split, created_at DESC, id DESC
            """
        ).fetchall()
        return [EvalRun(**r) for r in rows]

    def _summaries_where(self, condition: str, params: dict, limit: int) -> list[ClusterSummary]:
        rows = self.conn.execute(
            f"""
            SELECT {_SUMMARY_COLS} FROM clusters c {_LATEST_VERDICT}
            WHERE c.id IN ({condition})
            ORDER BY c.window_end DESC LIMIT %(limit)s
            """,
            {**params, "limit": limit},
        ).fetchall()
        return [_summary(r) for r in rows]

    def clusters_for_handle(self, handle: str, limit: int = 10) -> list[ClusterSummary]:
        return self._summaries_where(
            """SELECT m.cluster_id FROM cluster_members m JOIN accounts a ON a.did = m.did
               WHERE lower(a.handle) = lower(%(handle)s) OR a.did = %(handle)s""",
            {"handle": handle},
            limit,
        )

    def clusters_for_url(self, urls: list[str], limit: int = 10) -> list[ClusterSummary]:
        return self._summaries_where(
            """SELECT cp.cluster_id FROM cluster_posts cp
               JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
               WHERE s.embed_urls && %(urls)s::text[]""",
            {"urls": urls},
            limit,
        )

    def clusters_for_text(self, phrase: str, limit: int = 10) -> list[ClusterSummary]:
        return self._summaries_where(
            """SELECT cp.cluster_id FROM cluster_posts cp
               JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
               WHERE s.text ILIKE %(pattern)s""",
            {"pattern": _like(phrase)},
            limit,
        )
