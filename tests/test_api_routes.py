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

    def clusters_for_post(self, author, rkey):
        self.calls.append(("post", (author, rkey)))
        in_cluster = {("did:plc:abc", RKEY), ("alice.bsky.social", RKEY)}
        return [summary(682)] if (author, rkey) in in_cluster else []


RKEY = "3n27wevn47t2i"


class FakeChecker:
    def __init__(self, calls=None):
        self.calls = calls if calls is not None else []

    def check(self, posts, stored=None, **kw):
        self.calls.append(("check", [p.embed_urls for p in posts]))
        return [
            SourceEvidence(
                summary="found", detail={"key": "k"}, source_url="https://x.example", model="m"
            )
        ]


@pytest.fixture
def client():
    store = FakeStore()
    app.dependency_overrides[get_store] = lambda: store
    checker = FakeChecker(store.calls)
    app.dependency_overrides[get_checker] = lambda: checker
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


def test_analyze_strips_nul_and_control_chars(client):
    c, store = client
    text = "five words here and \u0000 then more words after it"
    res = c.post(
        "/api/analyze",
        json={"text": text, "handle": "bot.bsky\u0000.social", "url": "https://news.example/\u0000a"},
    )
    assert res.status_code == 200
    assert ("handle", "bot.bsky.social") in store.calls
    assert ("url", ["https://news.example/a"]) in store.calls
    phrases = [arg for kind, arg in store.calls if kind == "text"]
    assert phrases and all("\x00" not in p for p in phrases)
    assert all("\x00" not in str(arg) for _, arg in store.calls)


def test_analyze_nul_only_input_is_422(client):
    c, _ = client
    assert c.post("/api/analyze", json={"text": "\u0000 "}).status_code == 422
    assert c.post("/api/analyze", json={"handle": "\u0000", "url": "\x01"}).status_code == 422


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


@pytest.mark.parametrize(
    "url",
    [
        f"https://bsky.app/profile/did:plc:abc/post/{RKEY}",
        f"at://did:plc:abc/app.bsky.feed.post/{RKEY}",
        f"https://bsky.app/profile/alice.bsky.social/post/{RKEY}?ref=share",
    ],
)
def test_analyze_bsky_post_in_cluster_is_matched(client, url):
    c, store = client
    body = c.post("/api/analyze", json={"url": url}).json()
    assert body["status"] == "matched"
    assert [x["id"] for x in body["clusters"]] == [682]
    assert body["author_clusters"] == []
    assert any(kind == "post" and arg[1] == RKEY for kind, arg in store.calls)
    assert not [call for call in store.calls if call[0] == "check"]  # bsky links: no web check


def test_analyze_bsky_post_not_in_cluster_gives_author_context_only(client):
    c, store = client
    url = "https://bsky.app/profile/bot.bsky.social/post/zzz"
    body = c.post("/api/analyze", json={"url": url}).json()
    assert body["status"] == "insufficient"
    assert body["clusters"] == []
    assert [x["id"] for x in body["author_clusters"]] == [2]
    assert any("its author is a member of cluster #2" in n for n in body["notes"])
    assert any("not evidence that this post" in n for n in body["notes"])
    assert not any("live web check" in n.lower() for n in body["notes"])
    assert ("handle", "bot.bsky.social") in store.calls
    assert not [call for call in store.calls if call[0] == "check"]


def test_analyze_bsky_profile_link_is_a_handle_lookup(client):
    c, store = client
    body = c.post("/api/analyze", json={"url": "https://bsky.app/profile/bot.bsky.social"}).json()
    assert body["status"] == "matched"
    assert body["clusters"][0]["id"] == 2
    assert ("handle", "bot.bsky.social") in store.calls
    assert not [call for call in store.calls if call[0] in ("check", "post")]


def test_analyze_bsky_link_with_text_checks_only_the_text(client):
    c, store = client
    text = "five words here and then more words after it"
    url = f"https://bsky.app/profile/did:plc:abc/post/{RKEY}"
    c.post("/api/analyze", json={"url": url, "text": text})
    assert ("check", [[]]) in store.calls  # the text is searched, the bsky link is not scraped


def test_bsky_post_url():
    assert (
        bsky_post_url("at://did:plc:x/app.bsky.feed.post/abc")
        == "https://bsky.app/profile/did:plc:x/post/abc"
    )
    assert bsky_post_url("at://did:plc:x/app.bsky.feed.like/abc") is None
