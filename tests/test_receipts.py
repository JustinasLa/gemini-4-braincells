"""Screenshot receipts: shown-time windows, word diff, the judge and find_original (no network)."""

from __future__ import annotations

import sys
import threading
import time
import types
from datetime import UTC, datetime, timedelta

import pytest

from lookup import receipts
from lookup.models import Metrics
from lookup.receipts import (
    COUNTS_NOTE,
    NOT_FOUND,
    Budget,
    Candidate,
    ReceiptDeps,
    ScreenshotPost,
    ShotTimeUp,
    find_original,
    judge,
    shown_window,
    word_diff,
)
from lookup.urls import snowflake_time

ANCHOR = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
TZ = timedelta(hours=14)
TEXT = "The city council voted tonight to close the old library on Main Street next spring"


def shot(**kw) -> ScreenshotPost:
    base = dict(
        media_hash="sha256:abc",
        platform="bluesky",
        display_name="Hanna",
        handle="hanna.bsky.social",
        shown_time="2h",
        text=TEXT,
        counts={"likes": 120, "reposts": 4},
        ui_oddities=[],
        confidence=0.9,
        model="nemotron-test",
        latency_ms=800,
    )
    base.update(kw)
    return ScreenshotPost(**base)


def cand(**kw) -> Candidate:
    base = dict(
        id="at://did:plc:abc/app.bsky.feed.post/3kxyz",
        platform="bluesky",
        url="https://bsky.app/profile/hanna.bsky.social/post/3kxyz",
        handle="hanna.bsky.social",
        author_name="Hanna",
        text=TEXT,
        created_at=ANCHOR - timedelta(hours=5),
        time_source="api",
        metrics=Metrics(likes=150, reposts=6),
        receipt_id=7,
        source="author_feed",
    )
    base.update(kw)
    return Candidate(**base)


def no_fake(r) -> None:
    blob = " ".join(
        [r.label, r.reason, r.time_note or "", r.counts_note or "", *r.differences]
    ).lower()
    assert "fake" not in blob


# ---------------------------------------------------------------- shown_window


@pytest.mark.parametrize(
    "shown",
    [
        "3:14 PM · Nov 13, 2026",
        "3:14 PM Nov 13, 2026",
        "3:14 pm · Nov 13, 2026",
        "Nov 13, 2026 at 3:14 PM",
        "November 13, 2026 at 3:14 PM",
        "15:14 · 13 Nov 2026",
        "Edited · 3:14 PM · Nov 13, 2026 · 1.2M Views",
    ],
)
def test_absolute_time_is_within_14h_either_way(shown):
    t = datetime(2026, 11, 13, 15, 14, tzinfo=UTC)
    earliest, latest, note = shown_window(shown, ANCHOR)
    assert earliest == t - TZ
    assert latest == t + TZ + timedelta(seconds=59)
    assert "time zone of the viewer unknown: within ±14 h" in note


def test_midnight_and_noon_12h():
    e, _, _ = shown_window("12:05 AM · Nov 13, 2026", ANCHOR)
    assert e == datetime(2026, 11, 13, 0, 5, tzinfo=UTC) - TZ
    e, _, _ = shown_window("12:05 PM · Nov 13, 2026", ANCHOR)
    assert e == datetime(2026, 11, 13, 12, 5, tzinfo=UTC) - TZ


@pytest.mark.parametrize("shown", ["Nov 13, 2026", "13 Nov 2026", "November 13, 2026"])
def test_date_only_is_the_whole_day_within_14h(shown):
    day = datetime(2026, 11, 13, tzinfo=UTC)
    earliest, latest, note = shown_window(shown, ANCHOR)
    assert earliest == day - TZ
    assert latest == day + timedelta(days=1) + TZ
    assert "±14 h" in note


def test_date_without_year_is_latest_not_after_anchor():
    e, _, note = shown_window("Nov 13", ANCHOR)
    assert e == datetime(2026, 11, 13, tzinfo=UTC) - TZ
    assert "no year shown" in note
    # Nov 16 is past the anchor's date + 1 day of slack -> last year's
    e, _, _ = shown_window("Nov 16", ANCHOR)
    assert e == datetime(2025, 11, 16, tzinfo=UTC) - TZ
    e, _, _ = shown_window("Nov 15", ANCHOR)  # within the 1 day of slack
    assert e == datetime(2026, 11, 15, tzinfo=UTC) - TZ


@pytest.mark.parametrize(
    "shown, back",
    [
        ("2h", timedelta(hours=2)),
        ("5m", timedelta(minutes=5)),
        ("30s", timedelta(seconds=30)),
        ("3d", timedelta(days=3)),
        ("2 hours ago", timedelta(hours=2)),
        ("1 min", timedelta(minutes=1)),
        ("Edited 2h", timedelta(hours=2)),
        ("now", timedelta(0)),
        ("just now", timedelta(0)),
    ],
)
def test_relative_time_is_open_on_the_early_side(shown, back):
    earliest, latest, note = shown_window(shown, ANCHOR)
    assert earliest is None
    assert ANCHOR - back <= latest <= ANCHOR - back + timedelta(seconds=90)
    assert "relative" in note and "may be older" in note


@pytest.mark.parametrize("shown", [None, "", "yesterday-ish", "3:14 PM", "Feb 30, 2026"])
def test_unreadable_time_is_unknown(shown):
    assert shown_window(shown, ANCHOR) == (None, None, "shown time not readable")


# ---------------------------------------------------------------- word_diff


def test_word_diff_ignores_case_quotes_punctuation_and_urls():
    parts = word_diff(
        "The “quick” brown fox, see example.com/foo… #tag @bob 🔥",
        'the "quick" red fox! https://t.co/xyz #tag @bob 🔥 jumps',
    )
    assert [(p.op, p.text) for p in parts] == [
        ("same", "The “quick”"),
        ("shot_only", "brown"),
        ("post_only", "red"),
        ("same", "fox,"),
        ("shot_only", "see"),
        ("same", "#tag @bob 🔥"),
        ("post_only", "jumps"),
    ]


def test_word_diff_merges_runs_and_handles_empty():
    parts = word_diff("a b c d", "a b x y z")
    assert [p.op for p in parts] == ["same", "shot_only", "post_only"]
    assert parts[1].text == "c d" and parts[2].text == "x y z"
    assert word_diff(None, None) == []
    assert [p.op for p in word_diff("", "hello there")] == ["post_only"]


def test_word_diff_drops_show_more_and_cut_off_word():
    parts = word_diff("we must fix the governm… Show more", "We must fix the government today.")
    assert [(p.op, p.text) for p in parts] == [
        ("same", "we must fix the governm"),
        ("post_only", "today."),
    ]


# ---------------------------------------------------------------- judge


def test_judge_matches_bluesky_post():
    r = judge(shot(), [cand()], ANCHOR, receipt_ids=[3, 7])
    assert r.label == "matches"
    assert r.reason == "The screenshot matches @hanna.bsky.social's real post on Bluesky."
    assert r.original_url == "https://bsky.app/profile/hanna.bsky.social/post/3kxyz"
    assert r.original_id.startswith("at://")
    assert r.original_time_source == "api"
    assert r.similarity == 1.0
    assert r.time_check == "consistent"
    assert "consistent with it" in r.time_note
    assert r.shown_time_earliest is None and r.shown_time_latest is not None
    assert r.differences == []
    assert r.receipt_ids == [3, 7]
    assert r.candidates_checked == 1
    assert r.shown_text == TEXT and r.read_model == "nemotron-test" and r.read_confidence == 0.9
    assert r.counts_now == Metrics(likes=150, reposts=6)
    assert r.counts_note == COUNTS_NOTE
    no_fake(r)


def test_judge_x_match_with_snowflake_time():
    sid = "2121360054330986967"
    created = snowflake_time(sid)
    assert created == datetime(2026, 11, 13, 21, 50, 24, 573000, tzinfo=UTC)
    s = shot(platform="x", handle="StephenKing", shown_time="4:50 PM · Nov 13, 2026")  # UTC-5
    c = cand(
        id=f"x:{sid}",
        platform="x",
        url=f"https://x.com/StephenKing/status/{sid}",
        handle="StephenKing",
        created_at=created,
        time_source="snowflake",
    )
    r = judge(s, [c], ANCHOR)
    assert r.label == "matches"
    assert r.time_check == "consistent"
    assert r.original_time_source == "snowflake"
    assert r.reason == "The screenshot matches @StephenKing's real post on X."
    assert "13 Nov 2026 21:50 UTC" in r.time_note


def test_judge_one_changed_word_differs():
    s = shot(text=TEXT.replace("close", "sell"))
    r = judge(s, [cand()], ANCHOR)
    assert r.label == "differs"
    assert r.original_url  # the link is kept
    ops = {(p.op, p.text) for p in r.diff}
    assert ("shot_only", "sell") in ops and ("post_only", "close") in ops
    assert r.reason == (
        "The screenshot differs from @hanna.bsky.social's real post on Bluesky: 1 word changed."
    )
    assert "Words in the screenshot that the real post doesn't have: 'sell'" in r.differences
    assert "Words in the real post missing from the screenshot: 'close'" in r.differences
    assert r.time_check == "consistent"
    no_fake(r)


def test_judge_same_words_wrong_time_differs():
    # shows "3d" but the real post is only 5 h older than the latest screenshot time
    r = judge(shot(shown_time="3d"), [cand(created_at=ANCHOR - timedelta(hours=5))], ANCHOR)
    assert r.label == "differs"
    assert r.time_check == "inconsistent"
    assert "later than the time shown allows" in r.time_note
    assert any(d.startswith("The time shown ('3d') doesn't fit") for d in r.differences)
    assert "the time shown doesn't fit" in r.reason
    # an absolute time three days off
    s = shot(platform="x", shown_time="3:14 PM · Nov 13, 2026")
    r = judge(
        s, [cand(platform="x", created_at=datetime(2026, 11, 10, 15, 14, tzinfo=UTC))], ANCHOR
    )
    assert r.label == "differs" and r.time_check == "inconsistent"
    assert "earlier than the time shown allows" in r.time_note
    no_fake(r)


def test_judge_unknown_time_does_not_block_match():
    r = judge(shot(shown_time=None), [cand()], ANCHOR)
    assert r.label == "matches" and r.time_check == "unknown"
    r = judge(shot(), [cand(created_at=None)], ANCHOR)
    assert r.label == "matches" and r.time_check == "unknown"


def test_judge_not_found_wording():
    other = cand(text="Totally unrelated words about weekend football results and weather")
    r = judge(shot(), [other, other], ANCHOR)
    assert r.label == "not_found"
    assert r.reason == NOT_FOUND
    assert r.reason == "no matching post found: it may be deleted, private or never posted"
    assert r.candidates_checked == 2
    assert r.original_url is None
    no_fake(r)
    r = judge(shot(), [], ANCHOR)
    assert r.label == "not_found" and r.candidates_checked == 0
    r = judge(shot(text=""), [cand()], ANCHOR)
    assert r.label == "not_found"


def test_judge_truncated_screenshot_still_matches():
    s = shot(text="The city council voted tonight to close the old library on…")
    r = judge(s, [cand()], ANCHOR)
    assert r.label == "matches", r.differences
    assert r.diff[-1].op == "post_only"
    assert r.differences == []
    # not truncated: the missing tail is a difference
    s = shot(text="The city council voted tonight to close the old library on")
    assert judge(s, [cand()], ANCHOR).label == "differs"


def test_judge_counts_never_change_the_label():
    s = shot(counts={"likes": 99999, "reposts": 5000})
    r = judge(s, [cand(metrics=Metrics(likes=3, reposts=0))], ANCHOR)
    assert r.label == "matches"
    assert r.shown_counts == {"likes": 99999, "reposts": 5000}
    assert r.counts_now.likes == 3
    assert r.counts_note == COUNTS_NOTE
    r = judge(shot(counts={}), [cand(metrics=None)], ANCHOR)
    assert r.label == "matches" and r.counts_note is None


def test_judge_picks_best_then_time_fit_then_earliest():
    near = cand(id="a", text=TEXT.replace("spring", "summer"))
    exact_late = cand(id="b", created_at=ANCHOR - timedelta(hours=1))  # after "2h": no fit
    exact_fit = cand(id="c", created_at=ANCHOR - timedelta(hours=4))
    exact_fit_earlier = cand(id="d", created_at=ANCHOR - timedelta(hours=9))
    r = judge(shot(), [near, exact_late, exact_fit, exact_fit_earlier], ANCHOR)
    assert r.original_id == "d"
    assert r.candidates_checked == 4
    r = judge(shot(), [near, exact_late], ANCHOR)
    assert r.original_id == "b" and r.label == "differs"


def test_judge_either_side_truncated_counts():
    # the candidate is a short preview of the screenshot's text
    r = judge(
        shot(), [cand(text="The city council voted tonight to close the old library")], ANCHOR
    )
    assert r.label == "differs"
    assert r.similarity is not None and r.similarity >= 0.5


# ---------------------------------------------------------------- find_original


class Sink:
    def __init__(self):
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, tool, endpoint, summary, **kw):
        with self.lock:
            self.calls += 1
            return 100 + self.calls


def fake(monkeypatch, name, fn):
    attr = {"lookup.receipts_bsky": "find_bsky", "lookup.receipts_x": "find_x"}[name]
    mod = types.ModuleType(name)
    setattr(mod, attr, fn)
    monkeypatch.setitem(sys.modules, name, mod)


def deps(sink=None, **kw):
    return ReceiptDeps(receipt=sink or Sink(), anchor=ANCHOR, **kw)


def boom(*a, **k):
    raise AssertionError("resolver must not be called")


def test_find_original_no_handle(monkeypatch):
    fake(monkeypatch, "lookup.receipts_bsky", boom)
    sink = Sink()
    r = find_original(shot(handle=None), deps(sink, bsky=object()), Budget.seconds(30))
    assert r.label == "unchecked"
    assert r.reason == (
        "No account handle is legible in the screenshot, so the real post can't be looked up."
    )
    assert r.receipt_ids == [] and sink.calls == 0
    assert r.latency_ms is not None


def test_find_original_unsupported_and_unclear_platform(monkeypatch):
    fake(monkeypatch, "lookup.receipts_bsky", boom)
    fake(monkeypatch, "lookup.receipts_x", boom)
    d = deps(bsky=object(), oxylabs=object())
    r = find_original(shot(platform="threads"), d, Budget.seconds(30))
    assert r.label == "unchecked"
    assert (
        r.reason == "We can only look up X and Bluesky posts; this screenshot looks like Threads."
    )
    r = find_original(shot(platform="unknown", handle=None), d, Budget.seconds(30))
    assert r.label == "unchecked" and "platform isn't clear" in r.reason


def test_find_original_client_not_configured(monkeypatch):
    fake(monkeypatch, "lookup.receipts_bsky", boom)
    r = find_original(shot(), deps(), Budget.seconds(30))
    assert r.label == "unchecked" and "not configured on this server" in r.reason
    r = find_original(shot(platform="x"), deps(bsky=object()), Budget.seconds(30))
    assert r.reason == "Looking up X posts is not configured on this server."


def test_find_original_collects_receipt_ids(monkeypatch):
    seen = {}

    def find_bsky(s, d, budget):
        seen["anchor"] = d.anchor
        rid = d.receipt("bluesky", "searchPosts", "2 posts", source_url="https://bsky.app/x")
        d.receipt("bluesky", "getAuthorFeed", "30 posts")
        return [cand(receipt_id=rid)]

    fake(monkeypatch, "lookup.receipts_bsky", find_bsky)
    sink = Sink()
    r = find_original(shot(), deps(sink, bsky=object()), Budget.seconds(30))
    assert r.label == "matches"
    assert r.receipt_ids == [101, 102] and sink.calls == 2
    assert seen["anchor"] == ANCHOR
    assert r.latency_ms is not None and r.latency_ms >= 0


def test_find_original_anchor_defaults_to_now(monkeypatch):
    seen = {}

    def find_bsky(s, d, budget):
        seen["anchor"] = d.anchor
        return []

    fake(monkeypatch, "lookup.receipts_bsky", find_bsky)
    d = ReceiptDeps(receipt=Sink(), bsky=object(), now=lambda: ANCHOR)
    r = find_original(shot(), d, Budget.seconds(30))
    assert seen["anchor"] == ANCHOR
    assert r.label == "not_found" and r.reason == NOT_FOUND


def test_find_original_time_up(monkeypatch):
    def find_bsky(s, d, budget):
        d.receipt("bluesky", "searchPosts", "0 posts")
        raise ShotTimeUp("time budget used up")

    fake(monkeypatch, "lookup.receipts_bsky", find_bsky)
    r = find_original(shot(), deps(bsky=object()), Budget.seconds(30))
    assert r.label == "unchecked" and "time budget used up" in r.reason
    assert r.receipt_ids == [101]
    # a budget already spent never reaches the resolver
    fake(monkeypatch, "lookup.receipts_bsky", boom)
    r = find_original(shot(), deps(bsky=object()), Budget(time.monotonic() - 1))
    assert r.label == "unchecked" and "time budget used up" in r.reason


def test_find_original_resolver_exception(monkeypatch, caplog):
    def find_x(s, d, budget):
        raise ConnectionError("proxy down")

    fake(monkeypatch, "lookup.receipts_x", find_x)
    r = find_original(
        shot(platform="x", handle="hanna"), deps(oxylabs=object()), Budget.seconds(30)
    )
    assert r.label == "unchecked"
    assert r.reason == "lookup failed: ConnectionError"
    assert any(rec.levelname == "WARNING" for rec in caplog.records)


def test_find_original_unknown_platform_tries_both_in_parallel(monkeypatch):
    barrier = threading.Barrier(2, timeout=5)

    def find_bsky(s, d, budget):
        barrier.wait()  # both resolvers run at once, or this times out
        d.receipt("bluesky", "searchPosts", "1 post")
        return [cand(text="Something else entirely about a football match and the weather")]

    def find_x(s, d, budget):
        barrier.wait()
        d.receipt("oxylabs", "/search", "1 result")
        return [cand(id="x:1", platform="x", url="https://x.com/h/status/1", handle="h")]

    fake(monkeypatch, "lookup.receipts_bsky", find_bsky)
    fake(monkeypatch, "lookup.receipts_x", find_x)
    d = deps(bsky=object(), oxylabs=object())
    r = find_original(shot(platform="unknown", handle="h"), d, Budget.seconds(30))
    assert r.label == "matches"
    assert r.original_id == "x:1"
    assert r.reason == "The screenshot matches @h's real post on X."
    assert sorted(r.receipt_ids) == [101, 102]
    assert r.candidates_checked == 2


def test_find_original_never_raises_on_bad_resolver_output(monkeypatch):
    fake(monkeypatch, "lookup.receipts_bsky", lambda s, d, b: [object()])
    r = find_original(shot(), deps(bsky=object()), Budget.seconds(30))
    assert r.label == "unchecked" and r.reason.startswith("lookup failed: ")


def test_module_constants():
    assert receipts.SUPPORTED == ("x", "bluesky")
    assert "fake" not in NOT_FOUND.lower()


# ---------------------------------------------------------------- review r1 regressions


def test_failed_lookups_are_unchecked_not_not_found(monkeypatch):
    def find_bsky(s, d, budget):
        d.receipt("bluesky", "resolveHandle", "not resolved: ConnectError", ok=False)
        d.receipt("bluesky", "searchPosts", "failed: ConnectError", ok=False)
        d.receipt("bluesky", "getAuthorFeed", "30 recent posts")
        return []

    fake(monkeypatch, "lookup.receipts_bsky", find_bsky)
    r = find_original(shot(), deps(bsky=object()), Budget.seconds(30))
    assert r.label == "unchecked"
    assert "2 of 3 lookups failed or ran out of time" in r.reason
    assert NOT_FOUND not in r.reason
    no_fake(r)


def test_complete_lookups_without_the_post_are_not_found(monkeypatch):
    def find_bsky(s, d, budget):
        d.receipt("bluesky", "searchPosts", "0 posts")
        return []

    fake(monkeypatch, "lookup.receipts_bsky", find_bsky)
    r = find_original(shot(), deps(bsky=object()), Budget.seconds(30))
    assert (r.label, r.reason) == ("not_found", NOT_FOUND)


def test_differs_with_a_failed_lookup_says_a_closer_post_may_exist(monkeypatch):
    def find_bsky(s, d, budget):
        d.receipt("bluesky", "searchPosts", "failed", ok=False)
        return [cand(text=TEXT.replace("spring", "autumn"))]

    fake(monkeypatch, "lookup.receipts_bsky", find_bsky)
    r = find_original(shot(), deps(bsky=object()), Budget.seconds(30))
    assert r.label == "differs"
    assert any("a closer post may exist" in d for d in r.differences)


def test_late_receipts_after_return_are_dropped(monkeypatch):
    late = {}

    def find_bsky(s, d, budget):
        late["sink"] = d.receipt
        return [cand()]

    fake(monkeypatch, "lookup.receipts_bsky", find_bsky)
    sink = Sink()
    r = find_original(shot(), deps(sink, bsky=object()), Budget.seconds(30))
    assert late["sink"]("bluesky", "searchPosts", "straggler") == 0
    assert sink.calls == 0 and r.receipt_ids == []


@pytest.mark.parametrize(
    "shown, created, anchor, check",
    [
        # an authentic 2025 screenshot, reposted a year later: the year isn't shown
        ("Nov 13", datetime(2025, 11, 13, 15, 0, tzinfo=UTC), ANCHOR, "consistent"),
        ("3:00 PM · Nov 13", datetime(2025, 11, 13, 15, 0, tzinfo=UTC), ANCHOR, "consistent"),
        # year rollover: "Dec 31" seen on New Year's Day
        (
            "Dec 31",
            datetime(2026, 12, 31, 20, 0, tzinfo=UTC),
            datetime(2027, 1, 1, 1, 0, tzinfo=UTC),
            "consistent",
        ),
        # no year fits a post a week off
        ("Nov 13", datetime(2025, 11, 20, 12, 0, tzinfo=UTC), ANCHOR, "inconsistent"),
    ],
)
def test_date_without_year_fits_any_plausible_year(shown, created, anchor, check):
    r = judge(shot(shown_time=shown), [cand(created_at=created)], anchor)
    assert r.time_check == check
    assert r.label == ("matches" if check == "consistent" else "differs")
    if check == "consistent":
        assert r.shown_time_earliest <= created <= r.shown_time_latest


@pytest.mark.parametrize(
    "shown_text, real_text",
    [
        (
            "Tonight the temperature drops to -10 degrees",
            "Tonight the temperature drops to 10 degrees",
        ),
        (
            "Tonight the temperature drops to −10 degrees",
            "Tonight the temperature drops to 10 degrees",
        ),
        (
            "Shares of the company moved +5% in early trading",
            "Shares of the company moved -5% in early trading",
        ),
        (
            "Shares of the company moved 5% in early trading",
            "Shares of the company moved 5 in early trading",
        ),
        (
            "The fund lost $1.2M this quarter, said the report",
            "The fund lost $1.5M this quarter, said the report",
        ),
    ],
)
def test_numeric_signs_and_units_are_words_that_differ(shown_text, real_text):
    r = judge(shot(text=shown_text), [cand(text=real_text)], ANCHOR)
    assert r.label == "differs", r.diff
    assert any(p.op == "shot_only" for p in r.diff)


def test_same_numbers_with_signs_match():
    t = "Tonight the temperature drops to -10 degrees, up +5% on yesterday"
    assert judge(shot(text=t), [cand(text=t)], ANCHOR).label == "matches"


# ---------------------------------------------------------------- review r2 regressions


def test_exact_candidate_wins_over_an_equal_token_set_with_another_sign():
    exact = cand(
        id="at://p/pos",
        text="Shares of the company moved +5% in early trading",
        created_at=ANCHOR - timedelta(hours=3),
    )
    neg = cand(
        id="at://p/neg",
        text="Shares of the company moved -5% in early trading",
        created_at=ANCHOR - timedelta(hours=9),  # older: the old tie-break would pick it
    )
    s = shot(text="Shares of the company moved +5% in early trading")
    for order in ([neg, exact], [exact, neg]):
        r = judge(s, order, ANCHOR)
        assert (r.label, r.original_id) == ("matches", "at://p/pos")


def test_word_order_counts_when_ranking():
    s = shot(text="the early boat now leaves at 6:40 instead of 6:15 from the harbour")
    swapped = cand(
        id="at://p/swap", text="the early boat now leaves at 6:15 instead of 6:40 from the harbour"
    )
    right = cand(id="at://p/right", text=s.text, created_at=ANCHOR - timedelta(hours=3))
    r = judge(s, [swapped, right], ANCHOR)
    assert (r.label, r.original_id) == ("matches", "at://p/right")
    assert receipts.same_words(s.text, right.text)
    assert not receipts.same_words(s.text, swapped.text)


@pytest.mark.parametrize("text", [None, "", "   ", "…", "https://example.com/x"])
def test_no_legible_text_is_unchecked_without_calls(monkeypatch, text):
    fake(monkeypatch, "lookup.receipts_bsky", boom)
    sink = Sink()
    r = find_original(shot(text=text), deps(sink, bsky=object()), Budget.seconds(30))
    assert r.label == "unchecked" and "No post text is legible" in r.reason
    assert sink.calls == 0
    no_fake(r)


# ---------------------------------------------------------------- review r3 regressions


def test_truncated_screenshot_matches_the_post_it_starts():
    s = shot(text="The bridge is closed…")
    real = cand(
        text="The bridge is closed until engineers finish inspecting every support column "
        "on Monday afternoon."
    )
    r = judge(s, [real], ANCHOR)
    assert r.label == "matches", (r.label, r.similarity)


def test_resolver_error_with_the_other_resolvers_candidates_is_unchecked(monkeypatch):
    def find_bsky(s, d, budget):
        d.receipt("bluesky", "getAuthorFeed", "30 recent posts")
        return [cand(text="Something else entirely about weekend football results today")]

    def find_x(s, d, budget):
        raise ShotTimeUp("time budget used up")

    fake(monkeypatch, "lookup.receipts_bsky", find_bsky)
    fake(monkeypatch, "lookup.receipts_x", find_x)
    d = deps(bsky=object(), oxylabs=object())
    r = find_original(shot(platform="unknown", handle="hanna"), d, Budget.seconds(30))
    assert r.label == "unchecked" and "failed or ran out of time" in r.reason


def test_date_without_year_from_long_ago_still_fits():
    r = judge(
        shot(shown_time="Nov 13"),
        [cand(created_at=datetime(2016, 11, 13, 15, 0, tzinfo=UTC))],
        ANCHOR,
    )
    assert (r.time_check, r.label) == ("consistent", "matches")


def test_time_fits():
    created = datetime(2026, 11, 12, 14, 3, tzinfo=UTC)
    assert receipts.time_fits("2:03 PM · Nov 12, 2026", created, ANCHOR)
    assert receipts.time_fits(None, created, ANCHOR)  # unknown never rules it out
    assert not receipts.time_fits("2:03 PM · Nov 12, 2025", created, ANCHOR)


# ---------------------------------------------------------------- review r4 regressions


def test_a_fitting_time_beats_higher_similarity_when_the_words_tie():
    s = shot(text="The bridge is closed…", shown_time="2h")
    right = cand(
        id="at://p/right",
        text="The bridge is closed until engineers finish inspecting every support column",
        created_at=ANCHOR - timedelta(hours=3),
        receipt_id=11,
    )
    wrong_day = cand(
        id="at://p/old",
        text="The bridge is closed",
        created_at=ANCHOR + timedelta(days=3),  # after the screenshot: can't be it
        receipt_id=12,
    )
    for order in ([wrong_day, right], [right, wrong_day]):
        r = judge(s, order, ANCHOR)
        assert (r.label, r.original_id, r.original_receipt_id) == ("matches", "at://p/right", 11)


def test_unknown_platform_with_one_resolver_unavailable_is_unchecked(monkeypatch):
    def find_bsky(s, d, budget):
        d.receipt("bluesky", "searchPosts", "0 posts")
        return []

    fake(monkeypatch, "lookup.receipts_bsky", find_bsky)
    r = find_original(
        shot(platform="unknown", handle="hanna"), deps(bsky=object()), Budget.seconds(30)
    )
    assert r.label == "unchecked"
    assert "looking up X posts is not configured" in r.reason
    no_fake(r)


# ---------------------------------------------------------------- review r5 regressions


@pytest.mark.parametrize("shown", [None, "", "Nov 12, 2026", "3:14 PM · Nov 12, 2026", "2h"])
def test_a_post_newer_than_the_carrier_never_matches(shown):
    carrier = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    newer = cand(created_at=datetime(2026, 11, 12, 15, 14, tzinfo=UTC))
    r = judge(shot(shown_time=shown), [newer], carrier)
    assert (r.label, r.time_check) == ("differs", "inconsistent")
    assert "after the post that carries the screenshot" in r.time_note
    assert receipts.time_fits(shown, newer.created_at, carrier) is False


def test_a_few_minutes_of_clock_skew_still_fits():
    carrier = datetime(2026, 11, 12, 15, 10, tzinfo=UTC)
    r = judge(shot(shown_time=None), [cand(created_at=carrier + timedelta(minutes=4))], carrier)
    assert r.label == "matches"


def test_stop_word_only_post_matches_word_for_word():
    r = judge(shot(text="This is it."), [cand(text="This is it.")], ANCHOR)
    assert r.label == "matches" and r.similarity == 1.0


# ---------------------------------------------------------------- review r8 regression


def test_invalid_x_handle_is_unchecked_not_not_found(monkeypatch):
    fake(monkeypatch, "lookup.receipts_x", boom)
    sink = Sink()
    s = shot(platform="x", handle="much_too_long_handle_x")
    r = find_original(s, deps(sink, oxylabs=object()), Budget.seconds(30))
    assert r.label == "unchecked" and "isn't a valid X handle" in r.reason
    assert sink.calls == 0


def test_unknown_platform_with_a_non_x_handle_searches_bluesky_only(monkeypatch):
    fake(monkeypatch, "lookup.receipts_x", boom)
    fake(monkeypatch, "lookup.receipts_bsky", lambda s, d, b: [cand()])
    s = shot(platform="unknown", handle="hanna.bsky.social")
    r = find_original(s, deps(bsky=object(), oxylabs=object()), Budget.seconds(30))
    assert r.label == "matches"


# ---------------------------------------------------------------- edited X posts


def _edited_candidate():
    from lookup.receipts_x import _candidate
    from lookup.xpage import parse_post_page
    from tests.test_lookup import EDITED_MD

    m = parse_post_page(EDITED_MD, "2121316314375749699").main
    return _candidate(m, 1, "/search")  # its id is the edited version's: time = the edit


@pytest.mark.parametrize(
    "shown, anchor, check",
    [
        # 18:56 UTC edit, shown in a viewer's UTC+2: the normal +-14 h window applies
        ("Last edited 8:56 PM · Nov 13, 2026", ANCHOR, "consistent"),
        ("Last edited 6:56 PM · Nov 20, 2026", datetime(2026, 11, 21, tzinfo=UTC), "inconsistent"),
        ("6:56 PM · Nov 13, 2026 · Not edited", ANCHOR, "consistent"),
    ],
)
def test_edited_x_post_end_to_end_time(shown, anchor, check):
    c = _edited_candidate()
    assert c.edited and c.created_at == datetime(2026, 11, 13, 18, 56, 36, 155000, tzinfo=UTC)
    r = judge(shot(platform="x", handle="ada_example", text=c.text, shown_time=shown), [c], anchor)
    assert r.time_check == check
    noted = any("may show an earlier version" in d for d in r.differences)
    assert noted == (check == "inconsistent")  # only when it differs


def test_no_edit_note_for_a_post_that_was_not_edited():
    s = shot(text=TEXT.replace("spring", "autumn"))
    r = judge(s, [cand()], ANCHOR)
    assert r.label == "differs"
    assert not any("earlier version" in d for d in r.differences)


def test_edited_real_post_that_differs_says_it_was_edited():
    c = _edited_candidate()
    s = shot(
        platform="x", handle="ada_example", text=c.text.replace("6:40", "6:30"), shown_time=None
    )
    r = judge(s, [c], ANCHOR)
    assert r.label == "differs"
    assert any("may show an earlier version" in d for d in r.differences)
