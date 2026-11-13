"""Cluster media orchestration: blob fetch -> Nemotron -> evidence rows."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Protocol

from media.blobs import BlobFetcher, BlobFetchError
from media.nemotron import MediaAnalysis, MediaInput, NemotronError

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ClusterMedia:
    cluster_id: int
    post_uri: str
    did: str
    media_hash: str
    pds_host: str | None


@dataclass(frozen=True, slots=True)
class MediaFailure:
    media_hash: str
    did: str
    error: str


@dataclass
class MediaRun:
    analyses: list[MediaAnalysis] = field(default_factory=list)
    failures: list[MediaFailure] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return len(self.analyses) + len(self.failures)

    def evidence_rows(self, cluster_id: int) -> list[dict]:
        return [
            {
                "cluster_id": cluster_id,
                "summary": f"Nemotron: {analysis.description}",
                "detail": analysis.evidence_detail(),
                "source_url": analysis.source_uri,
                "model": analysis.model,
            }
            for analysis in self.analyses
        ]


class MediaClient(Protocol):
    def analyze(self, media: MediaInput, *, context: str | None = None) -> MediaAnalysis: ...


def process_cluster_media(
    posts: list[ClusterMedia],
    client: MediaClient,
    fetcher: BlobFetcher,
    *,
    context: str | None = None,
    max_media: int = 20,
) -> MediaRun:
    """Analyze unique cluster media, retaining explicit failures for observability.

    The same CID can occur in many posts; it is sent to Nemotron once and its cached result
    can then be attached to all relevant clusters. ``max_media`` bounds API usage per pass.
    """
    if max_media < 1:
        raise ValueError("max_media must be positive")
    run = MediaRun()
    seen: set[str] = set()
    for post in posts:
        if post.media_hash in seen:
            continue
        seen.add(post.media_hash)
        if len(seen) > max_media:
            break
        try:
            blob = fetcher.fetch(pds_host=post.pds_host, did=post.did, cid=post.media_hash)
            analysis = client.analyze(
                MediaInput(
                    media_hash=post.media_hash,
                    mime_type=blob.mime_type,
                    data=blob.data,
                    source_uri=blob.url,
                ),
                context=context,
            )
        except (BlobFetchError, NemotronError) as exc:
            failure = MediaFailure(post.media_hash, post.did, str(exc))
            run.failures.append(failure)
            log.warning(
                "media analysis failed cluster_id=%d hash=%s error=%s",
                post.cluster_id,
                post.media_hash,
                failure.error,
            )
            continue
        run.analyses.append(analysis)
    return run


def persist_evidence(conn, cluster_id: int, run: MediaRun) -> int:
    """Insert successful analyses as idempotent ``kind='media'`` evidence rows."""
    inserted = 0
    for row in run.evidence_rows(cluster_id):
        result = conn.execute(
            """
            INSERT INTO evidence (cluster_id, kind, summary, detail, source_url, model)
            SELECT %(cluster_id)s, 'media', %(summary)s, %(detail)s::jsonb,
                   %(source_url)s, %(model)s
            WHERE NOT EXISTS (
                SELECT 1 FROM evidence
                WHERE cluster_id = %(cluster_id)s AND kind = 'media'
                  AND detail->>'media_hash' = %(media_hash)s
            )
            """,
            {
                **row,
                "detail": json.dumps(row["detail"]),
                "media_hash": row["detail"]["media_hash"],
            },
        )
        inserted += result.rowcount
    return inserted