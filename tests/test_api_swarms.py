"""GET /api/swarms and /api/swarms/{id} against a fake store (no database)."""

from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from api import deps
from api.deps import get_swarm_store
from api.main import app
from swarm.model import SwarmCard, SwarmDetail, SwarmPost, SwarmReport, SwarmWall
from swarm.store import SwarmStore

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def card(gid="abc123def456", verdict="swarm", score=0.9) -> dict:
    return dict(
        id=gid,
        thumb=f"https://cdn.bsky.app/img/feed_thumbnail/plain/did:plc:x/{gid}@jpeg",
        verdict=verdict,
        score=score,
        n_posts=12,
        n_accounts=10,
        first_at=T0,
        last_at=T0,
        families={"timing": 0.8, "copy": 0.9},
        headline="10 accounts · same words",
        kind="meme",
    )


class FakeSwarmStore:
    """Holds one safe and one unsafe group, like SwarmStore after a pass."""

    def __init__(self):
        self.calls = []
        self.safe = SwarmDetail(
            **card(),
            picture="https://cdn.bsky.app/img/feed_fullsize/plain/did:plc:x/abc123def456@jpeg",
            report=SwarmReport(verdict="swarm", score=0.9, basis="live"),
            posts=[SwarmPost(url="https://bsky.app/profile/did:plc:x/post/1", text="hi")],
        )

    def wall(self, *, limit=24, verdict="all"):
        self.calls.append((limit, verdict))
        items = [SwarmCard(**card()), SwarmCard(**card("fff000fff000", "organic", 0.2))]
        if verdict != "all":
            items = [i for i in items if i.verdict == verdict]
        return SwarmWall(items=items[:limit], as_of=T0, window_h=24)

    def detail(self, gid):
        return self.safe if gid == self.safe.id else None  # unsafe ones read as missing


@pytest.fixture
def client():
    store = FakeSwarmStore()
    app.dependency_overrides[get_swarm_store] = lambda: store
    yield TestClient(app), store
    app.dependency_overrides.clear()


def test_wall_lists_swarm_first(client):
    c, store = client
    r = c.get("/api/swarms")
    assert r.status_code == 200
    body = r.json()
    assert [i["verdict"] for i in body["items"]] == ["swarm", "organic"]
    assert body["window_h"] == 24 and store.calls == [(24, "all")]
    assert set(body["items"][0]) >= {"id", "thumb", "verdict", "score", "headline", "families"}


def test_wall_filters_and_validates(client):
    c, store = client
    assert [i["verdict"] for i in c.get("/api/swarms?verdict=organic&limit=5").json()["items"]] == [
        "organic"
    ]
    assert store.calls[-1] == (5, "organic")
    assert c.get("/api/swarms?verdict=bots").status_code == 422
    assert c.get("/api/swarms?limit=0").status_code == 422


def test_detail_and_404(client):
    c, _ = client
    r = c.get("/api/swarms/abc123def456")
    assert r.status_code == 200
    body = r.json()
    assert body["picture"] == "/api/swarms/abc123def456/picture"  # same origin: readable bytes
    assert body["report"]["basis"] == "live" and body["posts"][0]["url"].startswith("https://")
    assert c.get("/api/swarms/unsafe0000id").status_code == 404
    assert c.get("/api/swarms/" + "x" * 80).status_code == 404


def test_picture_is_served_from_the_groups_own_cdn_url(client, monkeypatch):
    c, _ = client
    asked = []

    def fetch(url):
        asked.append(url)
        return b"\xff\xd8jpeg", "image/jpeg"

    monkeypatch.setattr("api.routes.swarms.fetch_picture", fetch)
    r = c.get("/api/swarms/abc123def456/picture")
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
    assert r.headers["content-type"] == "image/jpeg" and "max-age" in r.headers["cache-control"]
    assert asked == ["https://cdn.bsky.app/img/feed_fullsize/plain/did:plc:x/abc123def456@jpeg"]
    # unsafe or unknown groups have no picture here, and nothing is fetched for them
    assert c.get("/api/swarms/unsafe0000id/picture").status_code == 404
    assert len(asked) == 1


@pytest.mark.parametrize(
    "result", [RuntimeError("cdn down"), (b"<html>", "text/html")], ids=["error", "not an image"]
)
def test_picture_that_cannot_be_fetched_is_a_502(client, monkeypatch, result):
    c, _ = client

    def fetch(url):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("api.routes.swarms.fetch_picture", fetch)
    assert c.get("/api/swarms/abc123def456/picture").status_code == 502


class _NoTableConn:
    autocommit = False

    def cursor(self, row_factory=None):
        raise psycopg.errors.UndefinedTable('relation "meme_swarms" does not exist')

    def rollback(self):
        pass


def test_empty_wall_and_404_before_the_migration():
    app.dependency_overrides[get_swarm_store] = lambda: SwarmStore(_NoTableConn())
    try:
        c = TestClient(app)
        r = c.get("/api/swarms")
        assert r.status_code == 200 and r.json()["items"] == []
        assert c.get("/api/swarms/abc").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_503_when_the_database_is_down(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def down():
        raise psycopg.OperationalError("connection refused")
        yield

    monkeypatch.setattr(deps, "connect", down)
    r = TestClient(app).get("/api/swarms")
    assert r.status_code == 503
