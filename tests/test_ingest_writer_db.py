"""Writer against a real Postgres (skipped without TEST_DATABASE_URL): one bad row in a batch
must not stop ingest; the rest of the batch and the cursor are written."""

import os
import secrets
from datetime import UTC, datetime

import psycopg
import pytest

from ingest.normalize import Post
from ingest.writer import Batch, Writer

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="needs TEST_DATABASE_URL (a scratch database)")


def test_one_oversized_row_is_skipped_not_fatal():
    from common.migrate import migrate

    migrate(URL)
    now = datetime(2026, 11, 14, 5, 51, tzinfo=UTC)
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute("DELETE FROM posts WHERE did LIKE 'did:plc:wtest%'")
        w = Writer(conn, stream="test")
        b = Batch()
        good = Post(
            uri="at://did:plc:wtestgood/app.bsky.feed.post/1",
            cid=None,
            did="did:plc:wtestgood",
            text="fine",
            langs=None,
            created_at=now,
            seen_at=now,
        )
        bad = Post(
            uri="at://did:plc:wtestbad/app.bsky.feed.post/1",
            cid=None,
            did="did:plc:wtestbad",
            text="x",
            langs=None,
            created_at=now,
            seen_at=now,
            embed_urls=["https://e.com/" + secrets.token_hex(3000)],  # incompressible
        )  # bypasses the normaliser's cap
        b.add(good, 1794635460000000)
        b.add(bad, 1794635460000001)
        w.flush(b, "test-host", None)
        uris = {r[0] for r in conn.execute("SELECT uri FROM posts WHERE did LIKE 'did:plc:wtest%'")}
        assert uris == {good.uri}
        assert w.rows_skipped == 1
        cur = conn.execute("SELECT cursor_time_us FROM ingest_state WHERE stream = 'test'")
        assert cur.fetchone()[0] == 1794635460000001
