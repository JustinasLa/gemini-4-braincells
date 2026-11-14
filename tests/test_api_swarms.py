"""GET /api/swarms, /api/swarms/{id} and /api/swarms/{id}/picture against a fake store (no
database). The SQL behind them (safe, fresh, wall order) is tested in tests/test_swarm_live.py."""

from contextlib import contextmanager
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from api import deps
from api.deps import get_swarm_sessions, get_swarm_store
from api.main import app
from api.routes import swarms as swarm_routes
from swarm.model import SwarmCard, SwarmDetail, SwarmPost, SwarmReport, SwarmSignal, SwarmWall
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
    """Holds one safe group; every other id reads as missing, as SwarmStore does for unsafe or
    stale groups. `events` records when a session opens and closes."""

    def __init__(self):
        self.calls = []
        self.events: list[str] = []
        self.safe_ids = {"abc123def456"}
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
        return self.safe if gid in self.safe_ids else None

    def picture_source(self, gid):
        self.events.append("query")
        return ("did:plc:x", "abc123def456") if gid in self.safe_ids else None

    @contextmanager
    def session(self):
        self.events.append("open")
        yield self
        self.events.append("close")


@pytest.fixture
def client():
    store = FakeSwarmStore()
    swarm_routes.clear_picture_cache()
    app.dependency_overrides[get_swarm_store] = lambda: store
    app.dependency_overrides[get_swarm_sessions] = lambda: store.session
    yield TestClient(app), store
    app.dependency_overrides.clear()
    swarm_routes.clear_picture_cache()


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
    c, store = client
    asked = []

    def fetch(url):
        asked.append(url)
        store.events.append("fetch")
        return b"\xff\xd8jpeg", "image/jpeg"

    monkeypatch.setattr("api.routes.swarms.fetch_picture", fetch)
    r = c.get("/api/swarms/abc123def456/picture")
    assert r.status_code == 200 and r.content == b"\xff\xd8jpeg"
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "private, max-age=300"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert asked == ["https://cdn.bsky.app/img/feed_fullsize/plain/did:plc:x/abc123def456@jpeg"]
    # the database connection is closed before the CDN is asked
    assert store.events == ["open", "query", "close", "fetch"]
    # unsafe or unknown groups have no picture here, and nothing is fetched for them
    assert c.get("/api/swarms/unsafe0000id/picture").status_code == 404
    assert len(asked) == 1


def test_picture_cache_is_checked_against_safety_on_every_request(client, monkeypatch):
    c, store = client
    asked = []
    monkeypatch.setattr(
        "api.routes.swarms.fetch_picture",
        lambda url: asked.append(url) or (b"\x89PNGdata", "image/png"),
    )
    assert c.get("/api/swarms/abc123def456/picture").status_code == 200
    assert c.get("/api/swarms/abc123def456/picture").content == b"\x89PNGdata"
    assert len(asked) == 1  # second answer from the cache
    store.safe_ids.clear()  # a later pass found it unsafe (or it went stale)
    assert c.get("/api/swarms/abc123def456/picture").status_code == 404


@pytest.mark.parametrize(
    "result",
    [
        RuntimeError("cdn down"),
        (b"<html>", "text/html"),
        (b"<svg onload=alert(1)>", "image/svg+xml"),
        (b"GIF89a", "image/gif"),
    ],
    ids=["error", "not an image", "svg", "gif"],
)
def test_picture_that_cannot_be_fetched_is_a_502(client, monkeypatch, result):
    c, _ = client

    def fetch(url):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("api.routes.swarms.fetch_picture", fetch)
    assert c.get("/api/swarms/abc123def456/picture").status_code == 502


def test_detail_never_serves_examples_on_account_or_boost_signals():
    from swarm.store import detail_from_row

    sig = lambda fam: SwarmSignal(  # noqa: E731
        family=fam, name="n", score=1.0, summary="s", examples=["at://did:plc:a/p/1"]
    )
    report = SwarmReport(verdict="swarm", signals=[sig("accounts"), sig("boost"), sig("copy")])
    row = {
        "id": "abc123def456",
        "rep_did": "did:plc:x",
        "rep_cid": "abc123def456",
        "verdict": "swarm",
        "score": 0.9,
        "n_posts": 3,
        "n_accounts": 3,
        "report": report.model_dump(mode="json"),
    }

    class Store:
        def detail(self, gid):
            return detail_from_row(row)

    app.dependency_overrides[get_swarm_store] = lambda: Store()
    try:
        body = TestClient(app).get("/api/swarms/abc123def456").json()
    finally:
        app.dependency_overrides.clear()
    assert [s["examples"] for s in body["report"]["signals"]] == [[], [], ["at://did:plc:a/p/1"]]


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
