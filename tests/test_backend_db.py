"""sources/, analyst/ and api/ SQL against a real Postgres. Skipped unless TEST_DATABASE_URL is set.

The database must be disposable: the test applies db/schema.sql and db/migrations/*.sql, plus
any extra SQL files listed in TEST_EXTRA_SQL (colon-separated), then writes rows.

  TEST_DATABASE_URL=postgresql:///noise_test?host=/tmp/pg uv run pytest tests/test_backend_db.py
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)


def _sql_files() -> list[Path]:
    files = [ROOT / "db/schema.sql", *sorted((ROOT / "db/migrations").glob("*.sql"))]
    files += [Path(p) for p in os.environ.get("TEST_EXTRA_SQL", "").split(":") if p]
    return [f for f in files if f.exists()]


@pytest.fixture(scope="module")
def conn():
    with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        for f in _sql_files():
            c.execute(f.read_text())
        if c.execute("SELECT to_regclass('cluster_posts_snapshot') AS t").fetchone()["t"] is None:
            pytest.skip("schema has no cluster_posts_snapshot (signals migration not applied)")
        _seed(c)
        yield c


def _seed(c: psycopg.Connection) -> None:
    dids = [f"did:plc:bot{i}" for i in range(3)]
    for i, d in enumerate(dids):
        c.execute(
            "INSERT INTO accounts (did, handle, created_at, followers) VALUES (%s, %s, %s, %s)",
            (d, f"bot{i}.bsky.social", T0 - timedelta(days=1), 3),
        )
    cid = c.execute(
        """INSERT INTO clusters (window_start, window_end, score, confidence, status, signals,
               p_value, size, families_fired, window_kind, synthetic, updated_at)
           VALUES (%s, %s, 0.95, 0.9, 'coordinated', %s, 0.002, 3, 2, '10m', false, %s)
           RETURNING id""",
        (T0, T0 + timedelta(minutes=10), Jsonb({}), T0),
    ).fetchone()["id"]
    sig = c.execute(
        """INSERT INTO evidence (cluster_id, kind, summary, detail, source_url, model, created_at)
           VALUES (%s, 'signal', '3 of 3 accounts posted the same text', %s, NULL,
                   'detector:v1', %s)
           RETURNING id""",
        (
            cid,
            Jsonb(
                {"signal": "duplicate_text", "family": "content", "polarity": "for", "score": 1.0}
            ),
            T0,
        ),
    ).fetchone()["id"]
    c.execute(
        "UPDATE clusters SET signals = %s WHERE id = %s",
        (
            Jsonb(
                {
                    "families_fired": ["content", "timing"],
                    "signals": [
                        {
                            "family": "content",
                            "name": "duplicate_text",
                            "score": 1.0,
                            "polarity": "for",
                            "summary": "3 of 3 accounts posted the same text",
                            "evidence_id": sig,
                        },
                        {
                            "family": "guard",
                            "name": "news",
                            "score": 0.2,
                            "polarity": "against",
                            "summary": "g",
                        },
                    ],
                }
            ),
            cid,
        ),
    )
    for i, d in enumerate(dids):
        uri = f"at://{d}/app.bsky.feed.post/p{i}"
        c.execute(
            "INSERT INTO cluster_members (cluster_id, did, role) VALUES (%s, %s, 'core')", (cid, d)
        )
        c.execute("INSERT INTO cluster_posts (cluster_id, post_uri) VALUES (%s, %s)", (cid, uri))
        c.execute(
            """INSERT INTO cluster_posts_snapshot (uri, did, text, created_at, seen_at, embed_urls)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                uri,
                d,
                "The water supply was poisoned last night and officials hide it",
                T0,
                T0 + timedelta(seconds=i),
                ["https://news.example/story"],
            ),
        )
    for precision, fpr, n in ((0.5, 0.1, 10), (0.9, 0.02, 50)):  # the later run must win
        c.execute(
            "INSERT INTO eval_runs (dataset, split, precision, fpr, n) "
            "VALUES ('synthetic', 'test', %s, %s, %s)",
            (precision, fpr, n),
        )


@pytest.fixture(scope="module")
def cluster_id(conn) -> int:
    return conn.execute("SELECT id FROM clusters").fetchone()["id"]


def test_sources_store_roundtrip(conn, cluster_id):
    from sources import store
    from sources.models import SourceEvidence

    posts = store.load_cluster_posts(conn, cluster_id)
    assert len(posts) == 3 and posts[0].embed_urls == ["https://news.example/story"]
    ev = SourceEvidence(
        summary="s1",
        detail={"key": "page:x", "type": "linked_page"},
        source_url="https://news.example/story",
        model="oxylabs-web-api/scrape",
    )
    first = store.save_evidence(conn, cluster_id, [ev])
    again = store.save_evidence(conn, cluster_id, [ev.model_copy(update={"summary": "s2"})])
    assert first == again  # upsert by key keeps the id stable
    assert store.stored_details(conn, cluster_id) == {
        "page:x": {"key": "page:x", "type": "linked_page"}
    }
    assert store.clusters_to_check(conn, min_score=0.5, limit=5)[0][0] == cluster_id


def test_analyst_store_roundtrip(conn, cluster_id):
    from analyst import store
    from analyst.guard import no_evidence_verdict

    ctx = store.load_context(conn, cluster_id)
    assert ctx.status == "coordinated" and len(ctx.evidence) == 2  # signal + source
    assert len(ctx.sample_posts) == 3
    assert store.clusters_needing_verdict(
        conn, min_score=0.5, settle_s=0, refresh_s=600, limit=5
    ) == [cluster_id]
    assert (
        store.clusters_needing_verdict(
            conn, min_score=0.5, settle_s=0, refresh_s=600, limit=5, exclude=[cluster_id]
        )
        == []
    )

    # Settling counts from creation, so signals touching updated_at every pass can't block it.
    conn.execute(
        "UPDATE clusters SET updated_at = now(), created_at = now() - %s WHERE id = %s",
        (timedelta(minutes=5), cluster_id),
    )
    assert store.clusters_needing_verdict(
        conn, min_score=0.5, settle_s=90, refresh_s=600, limit=5
    ) == [cluster_id]

    store.save_verdict(conn, cluster_id, no_evidence_verdict(ctx))
    assert (
        store.clusters_needing_verdict(conn, min_score=0.5, settle_s=0, refresh_s=600, limit=5)
        == []
    )


def test_api_against_db(conn, cluster_id, monkeypatch):
    from api.deps import get_checker
    from api.main import app
    from common.config import get_settings

    monkeypatch.setenv("DATABASE_URL", DSN)
    get_settings.cache_clear()
    app.dependency_overrides[get_checker] = lambda: None
    try:
        c = TestClient(app)
        listed = c.get("/api/clusters", params={"status": "coordinated"}).json()
        assert [x["id"] for x in listed] == [cluster_id]
        assert listed[0]["top_signals"][0]["name"] == "duplicate_text"
        assert listed[0]["verdict"]["status"] == "coordinated"  # always the detector's call
        assert listed[0]["verdict"]["llm_assessment"] is None  # no model ran for this verdict

        detail = c.get(f"/api/clusters/{cluster_id}").json()
        assert len(detail["members"]) == 3 and detail["members"][0]["handle"].startswith("bot")
        assert detail["posts"][0]["url"].startswith("https://bsky.app/profile/did:plc:bot0/post/")
        assert {e["kind"] for e in detail["evidence"]} == {"signal", "source"}
        assert detail["analyst"]["model"] == "none"

        metrics = c.get("/api/metrics").json()
        assert len(metrics) == 1 and metrics[0]["precision"] == pytest.approx(0.9)

        for body in (
            {"handle": "@bot1.bsky.social"},
            {"url": "https://www.news.example/story?utm_source=x"},
            {"text": "the water supply was POISONED last night and officials hide it"},
        ):
            res = c.post("/api/analyze", json=body).json()
            assert res["status"] == "matched", body
            assert res["clusters"][0]["id"] == cluster_id

        miss = c.post("/api/analyze", json={"handle": "nobody.example"}).json()
        assert miss["status"] == "insufficient"
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_analyst_tool_evidence_db(conn, cluster_id):
    """Gemini's tool results land as evidence rows for the cluster and are reused, not refetched."""
    from analyst import store
    from analyst.tools import Budget, DbSink, ToolBox, WebCache
    from tests.test_analyst_agent import FakeWeb

    ctx = store.load_context(conn, cluster_id)

    def box(web):
        return ToolBox(
            ctx,
            web=web,
            sink=DbSink(conn, cluster_id),
            cache=WebCache(),
            budget=Budget(),
            conn=conn,
        )

    web = FakeWeb()
    first = box(web).call("scrape_url", {"url": "https://news.example/story", "why": "origin"})
    row = conn.execute("SELECT * FROM evidence WHERE id = %s", (first["evidence_id"],)).fetchone()
    assert row["cluster_id"] == cluster_id and row["kind"] == "source"
    assert row["source_url"] == "https://news.example/story"
    assert row["detail"]["requested_by"] == "gemini" and row["detail"]["shared_by_accounts"] == 3

    web2 = FakeWeb()
    again = box(web2).call("scrape_url", {"url": "https://news.example/story"})
    assert again["evidence_id"] == first["evidence_id"] and web2.scrapes == []

    b = box(FakeWeb())
    posts = b.call("get_cluster_posts", {"limit": 2})["posts"]
    assert len(posts) == 2 and posts[0]["author"].startswith("bot")
    assert b.call("get_account", {"account": posts[0]["author"]})["handle"] == posts[0]["author"]
    assert "not a member" in b.call("get_account", {"account": "someone.else"})["error"]
