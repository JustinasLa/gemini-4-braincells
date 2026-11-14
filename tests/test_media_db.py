"""media/ SQL against a real Postgres. Skipped unless TEST_DATABASE_URL is set (disposable DB:
the schema is dropped and recreated). See tests/test_backend_db.py for how to point it at one."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from media import store
from media.nemotron import PROMPT_VERSION, MediaAnalysis
from media.worker import (
    TEMPLATE_KEY,
    MediaFailure,
    MediaRun,
    load_cluster_media,
    persist_evidence,
    process_cluster_media,
)

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")
ROOT = Path(__file__).resolve().parent.parent
T0 = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        for f in [ROOT / "db/schema.sql", *sorted((ROOT / "db/migrations").glob("*.sql"))]:
            c.execute(f.read_text())
        yield c


def _cluster(c, status, score, posts, updated_at=T0):
    cid = c.execute(
        """INSERT INTO clusters (window_start, window_end, score, confidence, status, signals,
               size, window_kind, updated_at)
           VALUES (%s, %s, %s, 0.9, %s, %s, 3, '10m', %s) RETURNING id""",
        (T0, T0 + timedelta(minutes=10), score, status, Jsonb({}), updated_at),
    ).fetchone()["id"]
    for i, (did, hashes, deleted) in enumerate(posts):
        uri = f"at://{did}/app.bsky.feed.post/c{cid}p{i}"
        c.execute(
            """INSERT INTO cluster_posts_snapshot (uri, did, text, seen_at, embed_kind,
                   media_hashes, deleted_at) VALUES (%s, %s, 'hi', %s, 'images', %s, %s)""",
            (uri, did, T0 + timedelta(seconds=i), hashes, T0 if deleted else None),
        )
        c.execute("INSERT INTO cluster_posts (cluster_id, post_uri) VALUES (%s, %s)", (cid, uri))
    return cid


class Client:
    def __init__(self, texts):
        self.texts, self.calls = texts, []

    def analyze(self, media, *, context=None):
        self.calls.append(media.media_hash)
        return MediaAnalysis(
            media_hash=media.media_hash,
            description=f"Image {media.media_hash}",
            extracted_text=self.texts.get(media.media_hash, []),
            reuse_cues=[],
            manipulation_cues=[],
            confidence=0.9,
            model="nemotron-3.5-super-vl",
            latency_ms=5,
        )


class Fetcher:
    def fetch(self, *, did, cid, pds_host=None, embed_kind=None):
        class B:
            data, mime_type, url, media_kind = b"x", "image/jpeg", f"https://cdn/{cid}", "image"

        return B()


def test_candidate_clusters_filters_and_orders(conn):
    coordinated = _cluster(conn, "coordinated", 0.9, [("did:a", ["m1"], False)])
    insufficient = _cluster(
        conn, "insufficient", 0.8, [("did:b", ["m2"], False)], updated_at=T0 + timedelta(minutes=5)
    )
    _cluster(conn, "likely_organic", 0.95, [("did:c", ["m3"], False)])  # status not wanted
    _cluster(conn, "coordinated", 0.2, [("did:d", ["m4"], False)])  # score too low
    _cluster(conn, "coordinated", 0.9, [("did:e", None, False)])  # no media
    _cluster(conn, "coordinated", 0.9, [("did:f", ["m5"], True)])  # media only on deleted post
    rows = store.candidate_clusters(
        conn, statuses=["coordinated", "insufficient"], min_score=0.5, limit=10
    )
    assert [r["id"] for r in rows] == [coordinated, insufficient]


def test_persist_upserts_by_media_hash_and_cache_spans_clusters(conn):
    ad = "LAW FIRMS USE VIDEO MARKETING Around 24% of law firms use video {} VIDEO CO."
    texts = {"t1": [ad.format("CHICAGO")], "t2": [ad.format("AUSTIN")]}
    cid = _cluster(
        conn,
        "coordinated",
        0.9,
        [("did:p", ["t1"], False), ("did:q", ["t2"], False), ("did:r", ["t1"], False)],
    )
    client = Client(texts)
    run = process_cluster_media(
        load_cluster_media(conn, cid),
        client,
        Fetcher(),
        cache_lookup=lambda h: store.cached_analysis(conn, h),
    )
    assert persist_evidence(conn, cid, run) == 3  # two images + the OCR template row
    rows = conn.execute(
        "SELECT id, model, source_url, detail FROM evidence"
        " WHERE cluster_id = %s AND kind = 'media' ORDER BY id",
        (cid,),
    ).fetchall()
    assert [r["detail"]["media_hash"] for r in rows] == ["t1", "t2", TEMPLATE_KEY]
    assert rows[0]["detail"]["n_accounts"] == 2
    assert rows[0]["model"] == "nemotron-3.5-super-vl"
    assert rows[0]["source_url"].startswith("https://bsky.app/profile/did:p/post/")
    assert rows[0]["detail"]["prompt_version"] == PROMPT_VERSION

    # Same cluster again: served from the stored rows, nothing changes, ids stay.
    again = process_cluster_media(
        load_cluster_media(conn, cid),
        client,
        Fetcher(),
        cache_lookup=lambda h: store.cached_analysis(conn, h),
    )
    assert persist_evidence(conn, cid, again) == 0
    assert client.calls == ["t1", "t2"]

    # Another cluster reusing t1: no model call, its own row with its own post counts.
    other = _cluster(conn, "insufficient", 0.7, [("did:z", ["t1"], False)])
    run3 = process_cluster_media(
        load_cluster_media(conn, other),
        client,
        Fetcher(),
        cache_lookup=lambda h: store.cached_analysis(conn, h),
    )
    assert persist_evidence(conn, other, run3) == 1
    assert client.calls == ["t1", "t2"]
    row = store.stored_media(conn, other)["t1"]
    assert row["detail"]["n_accounts"] == 1 and row["detail"]["cached"] is True

    # The cluster grows: the existing row is updated in place (same id).
    conn.execute(
        "INSERT INTO cluster_posts_snapshot (uri, did, text, seen_at, embed_kind, media_hashes)"
        " VALUES ('at://did:s/app.bsky.feed.post/new', 'did:s', 'hi', %s, 'images', '{t1}')",
        (T0,),
    )
    conn.execute(
        "INSERT INTO cluster_posts VALUES (%s, 'at://did:s/app.bsky.feed.post/new')", (cid,)
    )
    grown = process_cluster_media(
        load_cluster_media(conn, cid),
        client,
        Fetcher(),
        cache_lookup=lambda h: store.cached_analysis(conn, h),
    )
    assert persist_evidence(conn, cid, grown) == 2  # t1 and the template row (new account)
    after = store.stored_media(conn, cid)["t1"]
    assert after["id"] == rows[0]["id"] and after["detail"]["n_accounts"] == 3


def test_failed_evidence_is_persisted_then_updated_in_place(conn):
    cluster_id = _cluster(conn, "coordinated", 0.9, [("did:retry", ["retry-cid"], False)])
    uri = f"at://did:retry/app.bsky.feed.post/c{cluster_id}p0"
    failure = MediaFailure(
        media_hash="retry-cid",
        did="did:retry",
        error="provider response should not be stored",
        stage="fetch",
        error_code="BlobFetchError",
        media_kind="images",
        post_uris=(uri,),
        dids=("did:retry",),
    )
    assert persist_evidence(conn, cluster_id, MediaRun(failures=[failure])) == 1
    failed = store.stored_media(conn, cluster_id)["retry-cid"]
    assert failed["detail"]["status"] == "failed"
    assert failed["detail"]["error_code"] == "BlobFetchError"
    assert "error" not in failed["detail"]
    assert failed["source_url"] == "https://bsky.app/profile/did:retry/post/retry"
    assert store.cached_analysis(conn, "retry-cid") is None

    run = process_cluster_media(
        load_cluster_media(conn, cluster_id), Client({}), Fetcher(),
        cache_lookup=lambda media_hash: store.cached_analysis(conn, media_hash),
    )
    assert persist_evidence(conn, cluster_id, run) == 1
    complete = store.stored_media(conn, cluster_id)["retry-cid"]
    assert complete["id"] == failed["id"]
    assert complete["detail"]["status"] == "complete"
    assert complete["model"] == "nemotron-3.5-super-vl"
    assert store.cached_analysis(conn, "retry-cid")["status"] == "complete"
    assert conn.execute("SELECT status FROM clusters WHERE id = %s", (cluster_id,)).fetchone()[
        "status"
    ] == "coordinated"


def test_done_clusters_do_not_use_up_the_batch(conn):
    statuses = ["coordinated", "insufficient"]
    first = store.candidate_clusters(conn, statuses=statuses, min_score=0.5, limit=1)[0]
    done = {first["id"]: first["updated_at"]}
    second = store.candidate_clusters(conn, statuses=statuses, min_score=0.5, limit=1, done=done)
    assert second and second[0]["id"] != first["id"]
    # Once signals updates the done cluster, it is a candidate again.
    conn.execute("UPDATE clusters SET updated_at = now() WHERE id = %s", (first["id"],))
    again = store.candidate_clusters(conn, statuses=statuses, min_score=0.5, limit=1, done=done)
    assert again[0]["id"] == first["id"]
