"""lookup/receipts_bsky.py: find the real post a Bluesky screenshot shows (no live network).

Fixtures in tests/fixtures/receipts/bsky_*.json have the AppView response shape and
fictional content."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from lookup.bsky import BlueskyClient
from lookup.receipts import Budget, ReceiptDeps, ScreenshotPost, ShotTimeUp
from lookup.receipts_bsky import find_bsky, normalise_handle, search_words

FIX = Path(__file__).parent / "fixtures" / "receipts"
ADA_DID = "did:plc:fictionalada0000000000001"
TEXT = (
    "The lighthouse keepers' union meets on Thursday to vote on the new night shift rota "
    "for the harbour"
)
RESOLVE = "com.atproto.identity.resolveHandle"
SEARCH = "app.bsky.feed.searchPosts"
FEED = "app.bsky.feed.getAuthorFeed"


def fx(name: str) -> dict:
    return json.loads((FIX / name).read_text())


class Sink:
    def __init__(self):
        self.rows: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, tool, endpoint, summary, **kw) -> int:
        with self._lock:
            self.rows.append({"tool": tool, "endpoint": endpoint, "summary": summary, **kw})
            return len(self.rows)

    def by(self, endpoint: str) -> dict:
        (row,) = [r for r in self.rows if r["endpoint"] == endpoint]
        return row


def shot(handle: str | None = "ada.example.com", text: str | None = TEXT) -> ScreenshotPost:
    return ScreenshotPost(
        media_hash="h",
        platform="bluesky",
        display_name="Ada Example",
        handle=handle,
        shown_time="Nov 13, 2026 at 2:14 PM",
        text=text,
        counts={"likes": 57},
        ui_oddities=[],
        confidence=0.9,
        model="test",
        latency_ms=1,
    )


def run(routes: dict, *, s: ScreenshotPost | None = None, budget_s: float = 10.0, delay=0.0):
    """routes: xrpc method -> (status, fixture name or dict). Returns (candidates, sink, seen)."""
    seen: list[httpx.Request] = []
    lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            seen.append(request)
        if delay:
            time.sleep(delay)
        status, body = routes[request.url.path.rsplit("/", 1)[-1]]
        return httpx.Response(status, json=fx(body) if isinstance(body, str) else body)

    sink = Sink()
    client = BlueskyClient(transport=httpx.MockTransport(handler))
    deps = ReceiptDeps(receipt=sink, bsky=client)
    out = find_bsky(s or shot(), deps, Budget.seconds(budget_s))
    return out, sink, seen


OK = {
    RESOLVE: (200, "bsky_resolve_ada.json"),
    SEARCH: (200, "bsky_search_ada.json"),
    FEED: (200, "bsky_feed_ada.json"),
}


def test_normalise_handle():
    assert normalise_handle("@Ada") == "ada.bsky.social"
    assert normalise_handle(" @Ada.Example.com ") == "ada.example.com"
    assert normalise_handle("ada.bsky.social") == "ada.bsky.social"


def test_search_words_uses_the_phrase_or_the_first_words():
    assert search_words(TEXT) == "The lighthouse keepers' union meets on Thursday to"
    assert search_words('Short "one" @bob') == "Short one"
    assert search_words("") is None
    assert search_words(None) is None


def test_match_found_via_search_first_then_feed():
    out, sink, seen = run(OK)
    match_uri = f"at://{ADA_DID}/app.bsky.feed.post/3lfictional0001"
    assert out[0].id == match_uri
    assert out[0].source == "searchPosts"
    assert out[0].receipt_id == next(
        i + 1 for i, r in enumerate(sink.rows) if r["endpoint"] == "searchPosts"
    )
    c = out[0]
    assert c.platform == "bluesky"
    assert c.url == "https://bsky.app/profile/ada.example.com/post/3lfictional0001"
    assert (c.handle, c.author_name, c.text, c.time_source) == (
        "ada.example.com",
        "Ada Example",
        TEXT,
        "api",
    )
    assert c.created_at is not None and c.created_at.isoformat().startswith("2026-11-13T14:14:05")
    assert (c.metrics.replies, c.metrics.reposts, c.metrics.likes, c.metrics.quotes) == (
        4,
        12,
        57,
        2,
    )
    # the match is also in the feed: deduped by uri; the feed adds the pin and the reply,
    # not the repost of someone else's post
    assert [x.id.rsplit("/", 1)[-1] for x in out] == [
        "3lfictional0001",
        "3lfictional0003",
        "3lfictional0005",
    ]
    assert [x.source for x in out[1:]] == ["getAuthorFeed", "getAuthorFeed"]
    pinned = out[1]
    assert pinned.metrics.quotes is None  # absent key -> None, never 0
    # requests: the search query and the feed filter
    q = next(r for r in seen if r.url.path.endswith(SEARCH)).url.params
    assert q["q"] == "from:ada.example.com The lighthouse keepers' union meets on Thursday to"
    assert q["sort"] == "latest"
    f = next(r for r in seen if r.url.path.endswith(FEED)).url.params
    assert (f["actor"], f["filter"], f["limit"]) == ("ada.example.com", "posts_with_replies", "50")


def test_receipts_once_per_call_with_endpoints_and_summaries():
    _, sink, seen = run(OK)
    assert len(seen) == 3
    assert sorted(r["endpoint"] for r in sink.rows) == [
        "getAuthorFeed",
        "resolveHandle",
        "searchPosts",
    ]
    assert all(r["tool"] == "bluesky" and r["ok"] for r in sink.rows)
    assert sink.by("resolveHandle")["summary"] == (
        f"Bluesky handle resolved: @ada.example.com -> {ADA_DID}"
    )
    assert sink.by("searchPosts")["summary"] == (
        "Bluesky search of @ada.example.com's posts for the screenshot's words: "
        "2 posts (1 by @ada.example.com)"
    )
    assert sink.by("searchPosts")["query"].startswith("from:ada.example.com ")
    assert sink.by("getAuthorFeed")["summary"] == (
        "Bluesky author feed of @ada.example.com: 4 recent posts (1 repost skipped)"
    )
    for r in sink.rows:  # each receipt carries its own call's latency (thread-local)
        assert r["call"]["status"] == 200
        assert isinstance(r["call"]["latency_ms"], int)
    assert {r["call"]["endpoint"] for r in sink.rows} == {RESOLVE, SEARCH, FEED}


def test_search_empty_found_via_author_feed():
    out, sink, _ = run({**OK, SEARCH: (200, "bsky_search_empty.json")})
    # feed order (pin first); picking the match by its words is judge()'s job
    assert [x.id.rsplit("/", 1)[-1] for x in out] == [
        "3lfictional0003",
        "3lfictional0005",
        "3lfictional0001",
    ]
    (match,) = [x for x in out if x.text == TEXT]
    assert match.source == "getAuthorFeed"
    assert match.receipt_id == next(
        i + 1 for i, r in enumerate(sink.rows) if r["endpoint"] == "getAuthorFeed"
    )
    assert sink.by("searchPosts")["summary"].endswith(": 0 posts")


def test_author_mismatch_filtered():
    out, _, _ = run(OK)
    assert all(x.id.startswith(f"at://{ADA_DID}/") for x in out)
    assert not any("impostor" in x.id or "bob" in x.id for x in out)


def test_resolve_failure_falls_back_to_handle_match():
    out, sink, _ = run({**OK, RESOLVE: (500, {"error": "InternalServerError"})})
    r = sink.by("resolveHandle")
    assert r["ok"] is False and "HTTP 500" in r["summary"]
    # same handle as the screenshot (case-insensitive): kept; the look-alike handle is not
    assert [x.id.rsplit("/", 1)[-1] for x in out] == [
        "3lfictional0001",
        "3lfictional0003",
        "3lfictional0005",
    ]


def test_handle_not_found_returns_nothing_with_an_answer_receipt():
    out, sink, _ = run(
        {
            RESOLVE: (400, "bsky_resolve_not_found.json"),
            SEARCH: (200, "bsky_search_empty.json"),
            FEED: (400, "bsky_feed_not_found.json"),
        },
        s=shot(handle="@Nobody"),
    )
    assert out == []
    r = sink.by("resolveHandle")
    assert r["ok"] is True  # an answer ("no such account"), so not_found, not unchecked
    assert r["summary"] == (
        "Bluesky handle @nobody.bsky.social not found on Bluesky (no account has it now)"
    )
    assert r["call"]["status"] == 400
    f = sink.by("getAuthorFeed")
    assert f["ok"] is True and "profile not found" in f["summary"]
    assert len(sink.rows) == 3


def test_provider_errors_never_raise():
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    sink = Sink()
    client = BlueskyClient(transport=httpx.MockTransport(boom))
    out = find_bsky(shot(), ReceiptDeps(receipt=sink, bsky=client), Budget.seconds(10))
    assert out == []
    assert len(sink.rows) == 3
    assert all(r["ok"] is False and "ConnectError" in r["summary"] for r in sink.rows)
    assert all(r["call"]["status"] is None for r in sink.rows)


def test_no_words_skips_search():
    out, sink, seen = run(OK, s=shot(text=None))
    assert not any(r.url.path.endswith(SEARCH) for r in seen)
    assert sorted(r["endpoint"] for r in sink.rows) == ["getAuthorFeed", "resolveHandle"]
    assert out[0].source == "getAuthorFeed"


def test_calls_run_in_parallel():
    started = time.monotonic()
    out, sink, seen = run(OK, delay=0.3)
    took = time.monotonic() - started
    assert len(seen) == 3 and len(sink.rows) == 3
    assert took < 0.8, took
    assert out


def test_budget_exhausted_starts_no_calls():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    sink = Sink()
    client = BlueskyClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ShotTimeUp):
        find_bsky(shot(), ReceiptDeps(receipt=sink, bsky=client), Budget.seconds(1.0))
    assert seen == []
    assert sink.rows == []


def test_slow_call_past_the_budget_is_an_unanswered_receipt():
    def handler(request):
        if request.url.path.endswith(FEED):
            time.sleep(4.0)
        status, body = OK[request.url.path.rsplit("/", 1)[-1]]
        return httpx.Response(status, json=fx(body))

    sink = Sink()
    client = BlueskyClient(transport=httpx.MockTransport(handler))
    started = time.monotonic()
    out = find_bsky(shot(), ReceiptDeps(receipt=sink, bsky=client), Budget.seconds(3.2))
    assert time.monotonic() - started < 3.8  # returned at the budget, not after the slow call
    f = sink.by("getAuthorFeed")
    assert f["ok"] is False and "no answer within the time budget" in f["summary"]
    assert [x.source for x in out] == ["searchPosts"]
    assert len(sink.rows) == 3
