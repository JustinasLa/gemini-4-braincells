"""API routes against a fake store (no database)."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from api.deps import get_checker, get_store
from api.main import app
from api.schemas import ClusterDetail, ClusterSummary, EvalRun
from api.store import bsky_post_url
from sources.models import SourceEvidence

T0 = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)


def summary(cid=1, status="coordinated") -> ClusterSummary:
    return ClusterSummary(id=cid, status=status, score=0.9, window_start=T0, window_end=T0)


class FakeStore:
    def __init__(self):
        self.calls = []

    def list_clusters(self, **kw):
        self.calls.append(("list", kw))
        return [summary()]

    def get_cluster(self, cluster_id):
        return ClusterDetail(**summary(cluster_id).model_dump()) if cluster_id == 1 else None

    def latest_metrics(self):
        return [
            EvalRun(id=1, dataset="synthetic", split="test", precision=0.9, fpr=0.02, created_at=T0)
        ]

    def clusters_for_handle(self, handle):
        self.calls.append(("handle", handle))
        return [summary(2)] if handle == "bot.bsky.social" else []

    def clusters_for_url(self, urls):
        self.calls.append(("url", urls))
        return []

    def clusters_for_text(self, phrase):
        self.calls.append(("text", phrase))
        return []


class FakeChecker:
    def check(self, posts, stored=None):
        return [
            SourceEvidence(
                summary="found", detail={"key": "k"}, source_url="https://x.example", model="m"
            )
        ]


@pytest.fixture
def client():
    store = FakeStore()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_checker] = lambda: FakeChecker()
    yield TestClient(app), store
    app.dependency_overrides.clear()


def test_list_clusters_passes_filters(client):
    c, store = client
    res = c.get("/api/clusters", params={"status": "coordinated", "min_score": 0.5, "limit": 10})
    assert res.status_code == 200
    assert res.json()[0]["id"] == 1
    assert store.calls[0] == (
        "list",
        {"status": "coordinated", "min_score": 0.5, "limit": 10, "include_synthetic": True},
    )


def test_list_clusters_rejects_unknown_status(client):
    c, _ = client
    assert c.get("/api/clusters", params={"status": "evil"}).status_code == 422


def test_get_cluster_and_404(client):
    c, _ = client
    assert c.get("/api/clusters/1").json()["id"] == 1
    assert c.get("/api/clusters/2").status_code == 404


def test_metrics(client):
    c, _ = client
    assert c.get("/api/metrics").json()[0]["fpr"] == 0.02


def test_analyze_requires_input(client):
    c, _ = client
    assert c.post("/api/analyze", json={"text": "  "}).status_code == 422


def test_analyze_matched_handle(client):
    c, store = client
    body = c.post("/api/analyze", json={"handle": "@bot.bsky.social"}).json()
    assert body["status"] == "matched"
    assert body["clusters"][0]["id"] == 2
    assert ("handle", "bot.bsky.social") in store.calls


def test_analyze_unknown_url_is_insufficient_with_web_findings(client):
    c, store = client
    body = c.post("/api/analyze", json={"url": "https://www.news.example/a?utm_source=x"}).json()
    assert body["status"] == "insufficient"
    assert body["web_findings"][0]["source_url"] == "https://x.example"
    assert "no coordination verdict" in body["notes"][0]
    assert (
        "url",
        ["https://news.example/a", "https://www.news.example/a?utm_source=x"],
    ) in store.calls


def test_analyze_short_text_skips_web_search_and_cluster_lookup(client):
    c, store = client
    body = c.post("/api/analyze", json={"text": "hi there"}).json()
    assert body["status"] == "insufficient"
    assert body["web_findings"] == []
    assert any("too short" in n for n in body["notes"])
    assert not [call for call in store.calls if call[0] == "text"]


def test_bsky_post_url():
    assert (
        bsky_post_url("at://did:plc:x/app.bsky.feed.post/abc")
        == "https://bsky.app/profile/did:plc:x/post/abc"
    )
    assert bsky_post_url("at://did:plc:x/app.bsky.feed.like/abc") is None
