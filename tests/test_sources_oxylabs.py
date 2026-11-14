"""Oxylabs Web API client against recorded response shapes (no network)."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from sources.oxylabs import OxylabsClient, OxylabsError, leading_date

SEARCH_RESPONSE = {
    "state": "done",
    "results": [
        {
            "title": "Bluesky 2024 Moderation Report",
            "outline": ["Jan 17, 2025 — In 2024, Bluesky moderators took down 66,308 accounts"],
            "url": "https://bsky.social/about/blog/01-17-2025-moderation-2024",
            "metadata": {"position": 1},
        },
        {
            "title": "How to spot bots on Bluesky",
            "outline": ["1 month ago - Reports go to Bluesky's moderation team."],
            "url": "https://bskycheck.com/guide-bluesky-bots.php",
            "metadata": {"position": 2},
        },
        {"title": "no url", "outline": []},
    ],
    "params": {"query": "q", "location": None, "max_results": 10},
    "metadata": {"timestamp": 1794600496, "request_id": "r1"},
}

SCRAPE_RESPONSE = {
    "state": "done",
    "results": [
        {
            "json": {
                "author": "Aaron Rodericks",
                "claims": ["Bluesky grew to 25.94 million users in 2024.", "  ", 7],
                "headline": "Bluesky 2024 Moderation Report",
                "published_at": "2025-01-17",
                "publisher": "Bluesky",
            },
            "metadata": {
                "url": "https://bsky.social/about/blog/01-17-2025-moderation-2024",
                "statuses": {"http_request": {"code": 200, "tag": "HTTP_OK"}},
            },
        }
    ],
    "metadata": {"request_id": "r2"},
}


def client_with(handler) -> OxylabsClient:
    return OxylabsClient("test-key", transport=httpx.MockTransport(handler), backoff_s=0)


def test_search_parses_hits_and_sends_bearer_auth():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["authorization"]
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=SEARCH_RESPONSE)

    hits = client_with(handler).search('"exact phrase"', max_results=5)

    assert seen["auth"] == "Bearer test-key"
    assert seen["path"] == "/v1/search"
    assert seen["body"] == {"query": '"exact phrase"', "max_results": 5}
    assert [h.position for h in hits] == [1, 2]  # result without url is skipped
    assert hits[0].published_hint == date(2025, 1, 17)
    assert hits[1].published_hint is None  # relative dates are not guessed


def test_scrape_facts_extracts_and_cleans_fields():
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["output"] == ["json", "markdown"] and "schema" in body["json"]
        return httpx.Response(200, json=SCRAPE_RESPONSE)

    facts = client_with(handler).scrape_facts("https://bsky.social/x")

    assert facts.publisher == "Bluesky"
    assert facts.published_at == "2025-01-17"
    assert facts.http_status == 200
    assert facts.final_url.endswith("moderation-2024")
    assert facts.claims == ["Bluesky grew to 25.94 million users in 2024."]


def test_retries_rate_limit_then_succeeds():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429 if len(calls) == 1 else 200, json=SEARCH_RESPONSE)

    assert len(client_with(handler).search("q")) == 2
    assert len(calls) == 2


def test_client_error_is_not_retried_and_carries_detail():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"title": "VALIDATION_ERROR", "detail": "bad field"})

    with pytest.raises(OxylabsError, match="bad field") as exc:
        client_with(handler).search("q")
    assert exc.value.status == 400
    assert len(calls) == 1


def test_missing_key_fails_fast():
    with pytest.raises(ValueError):
        OxylabsClient("")


@pytest.mark.parametrize(
    "snippet,expected",
    [
        ("Dec 19, 2024 — In the wake", date(2024, 12, 19)),
        ("January 29, 2026 - Violence-related", date(2026, 1, 29)),
        ("Oct 1, 2026 · Inspect public", date(2026, 10, 1)),
        ("Sept 3, 2025 — text", date(2025, 9, 3)),
        ("1 month ago - Reports", None),
        ("No date here", None),
    ],
)
def test_leading_date(snippet, expected):
    assert leading_date(snippet) == expected


@pytest.mark.parametrize(
    "url,final_url,expected",
    [
        ("https://x.com/TEN_GOP/status/1", "https://x.com/TEN_GOP/status/1", "suspended"),
        (
            "https://twitter.com/TEN_GOP/status/1",
            "https://twitter.com/TEN_GOP/status/1",
            "suspended",
        ),
        (
            "https://mobile.x.com/TEN_GOP/status/1",
            "https://mobile.x.com/TEN_GOP/status/1",
            "suspended",
        ),
        (
            "https://mobile.twitter.com/TEN_GOP/status/1",
            "https://x.com/TEN_GOP/status/1",
            "suspended",
        ),
        ("https://t.co/short", "https://x.com/TEN_GOP/status/1", "suspended"),
        ("https://x.com/TEN_GOP/status/1", "https://example.com", "suspended"),
        ("https://example.com/article", "https://example.com/article", None),
        ("https://x.com.example.com/article", "https://x.com.example.com/article", None),
    ],
)
def test_scrape_facts_detects_tombstone_only_for_x_hosts(url, final_url, expected):
    md = (
        Path(__file__).parent / "fixtures/xpages/post_suspended_ten_gop_788098438915297280.md"
    ).read_text()
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "json": {"headline": "Post"},
                        "markdown": md,
                        "metadata": {"url": final_url, "statuses": {"http_request": {"code": 200}}},
                    }
                ]
            },
        )

    facts = client_with(handler).scrape_facts(url)
    assert facts.unavailable == expected
    assert len(calls) == 1
    assert calls[0]["output"] == ["json", "markdown"]


def test_scrape_facts_live_post_with_suspended_reply_is_not_unavailable():
    from tests.test_lookup import POST_MD, SUBJ, SUSPENDED_ITEM

    url = f"https://x.com/ada_example/status/{SUBJ}"

    def handler(req):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "json": {"headline": 'Ada Example on X: "The comets have landed."'},
                        "markdown": POST_MD + SUSPENDED_ITEM,
                        "metadata": {"url": url, "statuses": {"http_request": {"code": 200}}},
                    }
                ]
            },
        )

    assert client_with(handler).scrape_facts(url).unavailable is None
