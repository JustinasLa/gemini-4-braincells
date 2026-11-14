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

    def check(self, posts, stored=None, adhoc=False):
        assert adhoc, "/api/analyze must ask for input-level wording"
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
    body = c.get("/api/clusters/1").json()
    assert body["id"] == 1
    assert body["evidence"] == []  # missing media evidence remains an empty list
    assert c.get("/api/clusters/2").status_code == 404


def test_media_evidence_serializes_all_states_without_changing_detector_status(client):
    c, store = client

    def get_cluster(cluster_id):
        if cluster_id != 1:
            return None
        data = summary(1, "coordinated").model_dump()
        data["evidence"] = [
            {
                "id": 10,
                "kind": "media",
                "summary": "Same image reused across accounts.",
                "detail": {
                    "status": "complete",
                    "media_hash": "cid-one",
                    "description": "A campaign graphic with a date.",
                    "media_type": "image",
                    "extracted_text": ["MEETING FRIDAY"],
                },
                "source_url": "https://bsky.app/profile/did:a/post/one",
                "model": "nemotron-3.5-super-vl",
                "created_at": T0,
            },
            {
                "id": 11,
                "kind": "media",
                "summary": "Video frame analysis.",
                "detail": {
                    "media_hash": "cid-two",
                    "description": "A video frame with a repeated slogan.",
                    "media_type": "video",
                },
                "source_url": "https://bsky.app/profile/did:b/post/two",
                "model": "nemotron-3.5-super-vl",
                "created_at": T0,
            },
            {
                "id": 12,
                "kind": "media",
                "summary": "OCR text is nearly identical across two images.",
                "detail": {
                    "status": "complete",
                    "type": "ocr_template",
                    "media_hash": "ocr-template",
                    "media_hashes": ["cid-one", "cid-two"],
                    "shared_tokens": ["USE", "VIDEO"],
                    "varying_tokens": ["AUSTIN", "CHICAGO"],
                },
                "source_url": "https://bsky.app/profile/did:a/post/one",
                "model": "nemotron-3.5-super-vl",
                "created_at": T0,
            },
            {
                "id": 13,
                "kind": "media",
                "summary": "Media processing failed during fetch; analysis is unavailable.",
                "detail": {
                    "status": "failed",
                    "media_hash": "cid-failed",
                    "error_code": "BlobFetchError",
                    "stage": "fetch",
                },
                "source_url": "https://bsky.app/profile/did:c/post/failed",
                "created_at": T0,
            },
            {
                "id": 14,
                "kind": "media",
                "summary": None,
                "detail": {"status": "pending", "media_hash": "cid-pending"},
                "created_at": T0,
            },
            {
                "id": 15,
                "kind": "media",
                "summary": "Must not look like successful analysis.",
                "detail": None,
                "model": "untrusted",
                "created_at": T0,
            },
        ]
        return ClusterDetail(**data)

    store.get_cluster = get_cluster
    response = c.get("/api/clusters/1")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "coordinated"  # media evidence cannot replace detector status
    evidence = body["evidence"]
    assert [row["detail"]["status"] for row in evidence] == [
        "complete", "complete", "complete", "failed", "pending", "malformed"
    ]
    assert evidence[0]["detail"]["media_hash"] == "cid-one"
    assert evidence[1]["detail"]["media_hash"] == "cid-two"
    assert evidence[2]["detail"]["type"] == "ocr_template"
    assert evidence[3]["detail"]["error_code"] == "BlobFetchError"
    assert evidence[5]["detail"]["malformed_reason"] == "detail_not_object"


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
        json={
            "text": text,
            "handle": "bot.bsky\u0000.social",
            "url": "https://news.example/\u0000a",
        },
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
        f"bsky.app/profile/did:plc:abc/post/{RKEY}",  # copied without a scheme
        f"AT://did:plc:abc/app.bsky.feed.post/{RKEY}/",
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


def test_analyze_control_chars_that_separate_words_become_spaces(client):
    c, store = client
    text = "water\x0csupply was poisoned last night in the city"
    c.post("/api/analyze", json={"text": text})
    phrases = [arg for kind, arg in store.calls if kind == "text"]
    assert phrases and phrases[0].startswith("water supply was")


def test_analyze_lone_surrogate_is_not_a_500(client):
    c, store = client
    res = c.post(
        "/api/analyze",
        content=b'{"handle": "bot\\ud800.bsky.social", "text": "\\udfff"}',
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 200
    assert ("handle", "bot.bsky.social") in store.calls


@pytest.mark.parametrize(
    "url",
    [
        "https://bsky.app/profile/bot.bsky.social/feed/news",
        "https://bsky.app/profile/bot.bsky.social/lists/abc",
        "at://did:plc:abc/app.bsky.actor.profile/self",
        "file:///etc/passwd",
        "https://cdn.example/photo.jpg",
    ],
)
def test_analyze_links_it_does_not_look_up_say_so(client, url):
    c, store = client
    body = c.post("/api/analyze", json={"url": url}).json()
    assert body["status"] == "insufficient"  # a feed or list is not the creator's handle
    assert body["clusters"] == [] and body["author_clusters"] == []
    assert any("not looked up" in n for n in body["notes"])
    assert not [call for call in store.calls if call[0] in ("check", "post", "handle")]


def test_bsky_target_forms():
    from api.routes.analyze import bsky_target

    assert bsky_target("https://bsky.app/profile/@Alice.bsky.social") == ("Alice.bsky.social", None)
    assert bsky_target("www.bsky.app/profile/did:plc:x/post/rk") == ("did:plc:x", "rk")
    assert bsky_target("at://did:plc:x") == ("did:plc:x", None)
    assert bsky_target("at://did:plc:x/app.bsky.feed.post/rk/extra") is None
    assert bsky_target("https://example.com/profile/x/post/y") is None


def test_bsky_post_url():
    assert (
        bsky_post_url("at://did:plc:x/app.bsky.feed.post/abc")
        == "https://bsky.app/profile/did:plc:x/post/abc"
    )
    assert bsky_post_url("at://did:plc:x/app.bsky.feed.like/abc") is None


@pytest.mark.parametrize("host", ["x.com", "mobile.x.com", "mobile.twitter.com"])
def test_analyze_matches_twitter_stored_link(client, monkeypatch, host):
    c, store = client

    def clusters_for_url(urls):
        return [summary(47)] if "https://twitter.com/a/status/1" in urls else []

    monkeypatch.setattr(store, "clusters_for_url", clusters_for_url)
    body = c.post("/api/analyze", json={"url": f"https://{host}/a/status/1"}).json()
    assert body["status"] == "matched"
    assert [cluster["id"] for cluster in body["clusters"]] == [47]
