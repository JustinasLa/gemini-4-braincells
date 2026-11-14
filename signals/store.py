"""Write detector output: clusters, members, posts (+ snapshots that outlive retention),
evidence(kind='signal') rows, and stable campaign ids.

One cluster row = one episode of a campaign in one window kind. Consecutive windows that find
(mostly) the same accounts update that row instead of inserting a new one, so the feed doesn't
repeat itself every minute. `campaign_id` links episodes of the same accounts over time.
Signal evidence rows are upserted by signal name, so their ids stay stable for analyst citations.
"""

import json
from datetime import datetime, timedelta

import psycopg

from signals.config import DetectorConfig
from signals.score import ClusterScore

SAME_EPISODE_JACCARD = 0.5
SAME_CAMPAIGN_JACCARD = 0.3
CAMPAIGN_LOOKBACK = timedelta(hours=6)


def bsky_url(uri: str) -> str | None:
    """at://did/app.bsky.feed.post/rkey -> https://bsky.app/profile/did/post/rkey"""
    if not uri.startswith("at://"):
        return None
    parts = uri[5:].split("/")
    if len(parts) == 3 and parts[1] == "app.bsky.feed.post":
        return f"https://bsky.app/profile/{parts[0]}/post/{parts[2]}"
    return None


# clusters.p_value is REAL (float4): anything below ~1.2e-38 underflows and Postgres rejects
# the row ("value out of range: underflow"), which used to fail a whole 1-hour pass. The column
# stores at most this resolution; the exact p stays in clusters.signals (null_model.combined).
P_VALUE_FLOOR = 1e-30


def _real_p(p: float | None) -> float | None:
    return None if p is None else max(float(p), P_VALUE_FLOOR)


def worth_storing(c: ClusterScore, cfg: DetectorConfig, min_store_score: float) -> bool:
    """Store every coordinated cluster, and any other cluster where at least one family looked
    suspicious (those are the abstentions and organic look-alikes worth showing). Clusters where
    nothing looked suspicious aren't stored."""
    return c.status == "coordinated" or max(c.family_scores.values()) >= min_store_score


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def _signals_json(
    c: ClusterScore, cfg: DetectorConfig, evidence_ids: dict[str, int], window: dict | None = None
) -> dict:
    return {
        "window": window or {},
        "null_distribution": c.p_detail.get("null_distribution"),
        "timeline": c.timeline,
        "member_graph": c.member_graph,
        "detector_version": cfg.version,
        "score": c.score,
        "family_scores": c.family_scores,
        "families_fired": c.families_fired,
        "p_value": c.p_value,
        "null_model": c.p_detail,
        "organic_guard_score": c.guard_score,
        "signals": [
            {
                "family": s.family,
                "name": s.name,
                "score": s.score,
                "polarity": s.polarity,
                "summary": s.summary,
                "evidence_id": evidence_ids.get(s.name),
            }
            for s in sorted(c.signals, key=lambda s: (s.polarity != "for", -s.score, s.name))
        ],
        "status_rule": {
            "coordinated": f">= {cfg.min_families_coordinated} families >= {cfg.family_fire}, "
            f"p <= {cfg.max_p_coordinated}, score >= {cfg.min_score_coordinated}, "
            f"organic guard < {cfg.organic_guard_strong}",
            "likely_organic": f"organic guard >= {cfg.organic_guard_strong} and <= 1 family fired",
        },
    }


def store_clusters(
    conn: psycopg.Connection,
    clusters: list[ClusterScore],
    window_kind: str,
    start: datetime,
    end: datetime,
    cfg: DetectorConfig,
    synthetic: bool = False,
) -> list[int]:
    ids = []
    for c in clusters:
        with conn.transaction():
            ids.append(_store_one(conn, c, window_kind, start, end, cfg, synthetic))
    return ids


def _store_one(
    conn,
    c: ClusterScore,
    window_kind: str,
    start: datetime,
    end: datetime,
    cfg: DetectorConfig,
    synthetic: bool,
) -> int:
    members = set(c.members)
    window_len = end - start
    # 1. Same episode? (any window kind, window ending at most one window length before this
    # one starts, mostly the same accounts)
    recent = conn.execute(
        """
        SELECT c.id, c.campaign_id, array_agg(m.did), c.window_kind, c.score FROM clusters c
        JOIN cluster_members m ON m.cluster_id = c.id
        WHERE c.detector_version = %s AND c.synthetic = %s AND c.window_end >= %s
        GROUP BY c.id, c.campaign_id, c.window_kind, c.score ORDER BY c.id
        """,
        (cfg.version, synthetic, start - window_len),
    ).fetchall()
    best = max(recent, key=lambda r: _jaccard(members, set(r[2])), default=None)
    cluster_id = campaign_id = None
    if best and _jaccard(members, set(best[2])) >= SAME_EPISODE_JACCARD:
        cluster_id, campaign_id = best[0], best[1]
        if best[3] != window_kind and c.score < best[4]:
            # Another window kind already holds a stronger view of this episode: keep it, just
            # extend the time range so the feed shows the episode is still active.
            conn.execute(
                "UPDATE clusters SET window_end = greatest(window_end, %s), updated_at = now() "
                "WHERE id = %s",
                (end, cluster_id),
            )
            return cluster_id
    # 2. Otherwise, same campaign as an earlier cluster (any window kind)?
    if campaign_id is None:
        older = conn.execute(
            """
            SELECT c.campaign_id, array_agg(m.did) FROM clusters c
            JOIN cluster_members m ON m.cluster_id = c.id
            WHERE c.campaign_id IS NOT NULL AND c.synthetic = %s AND c.window_end >= %s
              AND m.did = ANY(%s)
            GROUP BY c.id, c.campaign_id
            """,
            (synthetic, end - CAMPAIGN_LOOKBACK, sorted(members)),
        ).fetchall()
        prev = max(older, key=lambda r: len(members & set(r[1])), default=None)
        if prev and len(members & set(prev[1])) / len(members) >= SAME_CAMPAIGN_JACCARD:
            campaign_id = prev[0]
    if campaign_id is None:
        campaign_id = conn.execute(
            "INSERT INTO campaigns (first_seen, last_seen, max_score, status) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (start, end, c.score, c.status),
        ).fetchone()[0]

    row = (
        end,
        c.score,
        c.confidence,
        c.status,
        _real_p(c.p_value),
        len(c.families_fired),
        cfg.version,
        window_kind,
        len(c.members),
        campaign_id,
    )
    if cluster_id is None:
        cluster_id = conn.execute(
            """
            INSERT INTO clusters (window_start, window_end, score, confidence, status, p_value,
                families_fired, detector_version, window_kind, size, campaign_id, synthetic)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (start, *row, synthetic),
        ).fetchone()[0]
    else:
        conn.execute(
            """
            UPDATE clusters SET window_start = least(window_start, %s), window_end = %s,
                score = %s, confidence = %s, status = %s,
                p_value = %s, families_fired = %s, detector_version = %s, window_kind = %s,
                size = %s, campaign_id = %s, updated_at = now()
            WHERE id = %s
            """,
            (start, *row, cluster_id),
        )

    core = set(c.core)
    conn.execute("DELETE FROM cluster_members WHERE cluster_id = %s", (cluster_id,))
    conn.cursor().executemany(
        "INSERT INTO cluster_members (cluster_id, did, role) VALUES (%s, %s, %s)",
        [(cluster_id, d, "core" if d in core else "peripheral") for d in sorted(members)],
    )
    if c.post_uris:
        conn.execute(
            "INSERT INTO cluster_posts (cluster_id, post_uri) SELECT %s, unnest(%s::text[]) "
            "ON CONFLICT DO NOTHING",
            (cluster_id, c.post_uris),
        )
        conn.execute(
            """
            INSERT INTO cluster_posts_snapshot (uri, did, text, langs, created_at, seen_at,
                reply_to, reply_root, quote_uri, embed_kind, embed_urls, media_hashes, text_hash,
                deleted_at)
            SELECT DISTINCT ON (uri) uri, did, text, langs, created_at, seen_at, reply_to,
                reply_root, quote_uri, embed_kind, embed_urls, media_hashes, text_hash, deleted_at
            FROM posts WHERE uri = ANY(%s) ORDER BY uri, seen_at
            ON CONFLICT (uri) DO UPDATE SET deleted_at = EXCLUDED.deleted_at,
                snapshot_at = now()
            """,
            (c.post_uris,),
        )

    # Evidence: one row per signal, upserted by name so ids stay stable.
    existing = {
        r[1]: r[0]
        for r in conn.execute(
            "SELECT id, detail->>'signal' FROM evidence WHERE cluster_id = %s AND kind = 'signal'",
            (cluster_id,),
        )
    }
    evidence_ids: dict[str, int] = {}
    for s in c.signals:
        detail = {
            "signal": s.name,
            "family": s.family,
            "score": s.score,
            "polarity": s.polarity,
            **s.detail,
            "post_uris": s.uris[:50],
            "post_urls": [u for u in (bsky_url(x) for x in s.uris[:50]) if u],
            "dids": s.dids[:100],
            "profile_urls": [f"https://bsky.app/profile/{d}" for d in s.dids[:100]],
        }
        source = next((u for u in (bsky_url(x) for x in s.uris) if u), None)
        if s.name in existing:
            conn.execute(
                "UPDATE evidence SET summary = %s, detail = %s, source_url = %s, "
                "model = %s, created_at = now() WHERE id = %s",
                (
                    s.summary,
                    json.dumps(detail),
                    source,
                    f"detector:{cfg.version}",
                    existing[s.name],
                ),
            )
            evidence_ids[s.name] = existing[s.name]
        else:
            evidence_ids[s.name] = conn.execute(
                "INSERT INTO evidence (cluster_id, kind, summary, detail, source_url, model) "
                "VALUES (%s, 'signal', %s, %s, %s, %s) RETURNING id",
                (cluster_id, s.summary, json.dumps(detail), source, f"detector:{cfg.version}"),
            ).fetchone()[0]
    gone = [i for name, i in existing.items() if name not in evidence_ids]
    if gone:
        conn.execute("DELETE FROM evidence WHERE id = ANY(%s)", (gone,))

    conn.execute(
        "UPDATE clusters SET signals = %s WHERE id = %s",
        (
            json.dumps(
                _signals_json(
                    c,
                    cfg,
                    evidence_ids,
                    {"kind": window_kind, "start": start.isoformat(), "end": end.isoformat()},
                )
            ),
            cluster_id,
        ),
    )
    conn.execute(
        """
        UPDATE campaigns SET last_seen = greatest(last_seen, %s), latest_cluster_id = %s,
            max_score = greatest(max_score, %s), status = %s, updated_at = now()
        WHERE id = %s
        """,
        (end, cluster_id, c.score, c.status, campaign_id),
    )
    return cluster_id
