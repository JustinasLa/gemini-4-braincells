"""lookup/receipts_x.py: find the real X post a screenshot shows (no network).

Fixtures in tests/fixtures/receipts/x_* are fictional accounts in the shape Oxylabs returns
for logged-out X pages (see POST_MD / PROFILE_MD in tests/test_lookup.py).
"""

import itertools
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from lookup.receipts import Budget, ReceiptDeps, ScreenshotPost, ShotTimeUp
from lookup.receipts_x import find_x
from lookup.urls import snowflake_time
from sources.models import SearchHit
from sources.oxylabs import OxylabsError

FIX = Path(__file__).parent / "fixtures" / "receipts"

MATCH = "2120880086184914951"  # mira_test, 2026-11-12 14:03:11.250 UTC from the id alone
OLDER = "2120814113423634435"  # mira_test, unrelated
QUOTE = "2120923450062180361"  # mira_test quoting nora_sample
QUOTED = "2120582519819603969"  # nora_sample, the quoted post
OTHER = "2120063913423806466"  # nora_sample, a copy of the text (another account)
TEXT = (
    "The harbour ferry timetable changes on Monday, so the early boat now leaves at 6:40 "
    "instead of 6:15. Plan ahead!"
)
OWN = "Read this twice and still cannot believe anyone on the council signed it."
QUOTED_TEXT = "The city council approves a plan to paint every bridge in town bright orange"

PROFILE_URL = "https://x.com/mira_test"


def md(name: str) -> str:
    return (FIX / name).read_text()


def hits(name: str) -> list[SearchHit]:
    return [SearchHit(**h) for h in json.loads((FIX / name).read_text())]


class FakeOxylabs:
    """Thread-safe; `last_call` is per thread like sources.oxylabs.OxylabsClient.

    `exact` / `loose`: hits (or an exception) for the quoted-phrase / unquoted search.
    `pages`: url -> markdown (or an exception)."""

    def __init__(self, *, exact=(), loose=(), pages=None, delay=0.0):
        self.exact, self.loose, self.pages = exact, loose, pages or {}
        self.delay = delay
        self.calls: list[tuple[str, str, str]] = []  # (kind, query or url, request id)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._ids = itertools.count(1)

    @property
    def last_call(self) -> dict:
        return getattr(self._local, "last_call", {})

    def _record(self, kind: str, arg: str, endpoint: str) -> None:
        with self._lock:
            rid = f"req-{next(self._ids)}"
            self.calls.append((kind, arg, rid))
        self._local.last_call = {"endpoint": endpoint, "request_id": rid, "latency_ms": 7}

    def search(self, query, max_results=10, *, deadline=None):
        assert deadline is not None
        time.sleep(self.delay)
        self._record("search", query, "/search")
        out = self.exact if '"' in query else self.loose
        if isinstance(out, Exception):
            raise out
        return list(out)

    def scrape_markdown(self, url, *, deadline=None):
        assert deadline is not None
        time.sleep(self.delay)
        self._record("scrape", url, "/scrape")
        page = self.pages.get(url)
        if page is None:
            raise OxylabsError(f"oxylabs /scrape failed (404): {url}", status=404)
        if isinstance(page, Exception):
            raise page
        return page, {}


class Sink:
    def __init__(self):
        self.rows: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, tool, endpoint, summary, **kw):
        with self._lock:
            self.rows.append({"tool": tool, "endpoint": endpoint, "summary": summary, **kw})
            return len(self.rows)

    def by_id(self, rid: int) -> dict:
        return self.rows[rid - 1]


def shot(text=TEXT, handle="mira_test") -> ScreenshotPost:
    return ScreenshotPost(
        media_hash="h",
        platform="x",
        display_name="Mira Test",
        handle=handle,
        shown_time="2:03 PM · Nov 12, 2026",
        text=text,
        counts={},
        ui_oddities=[],
        confidence=0.9,
        model="test",
        latency_ms=1,
    )


def run(ox, s=None, **deps_kw):
    sink = Sink()
    deps = ReceiptDeps(receipt=sink, oxylabs=ox, **deps_kw)
    return find_x(s or shot(), deps, Budget.seconds(20)), sink


def call_ids(ox, kind):
    return {arg: rid for k, arg, rid in ox.calls if k == kind}


def test_match_via_exact_phrase_search_and_post_page():
    post_url = f"https://x.com/mira_test/status/{MATCH}"
    ox = FakeOxylabs(
        exact=hits("x_search_exact.json"),
        loose=[],
        pages={PROFILE_URL: md("x_profile_old.md"), post_url: md("x_post_match.md")},
    )
    cands, sink = run(ox)
    assert [c.id for c in cands] == [f"x:{MATCH}"]
    c = cands[0]
    assert c.created_at == datetime(2026, 11, 12, 14, 3, 11, 250000, tzinfo=UTC)
    assert c.created_at == snowflake_time(MATCH)
    assert (c.platform, c.url, c.handle, c.author_name) == ("x", post_url, "mira_test", "Mira Test")
    assert c.text == TEXT and c.time_source == "snowflake" and c.source == "/search"
    m = c.metrics
    assert (m.views, m.replies, m.reposts, m.likes, m.bookmarks) == (48200, 31, 120, 904, 17)
    # the candidate's receipt is the post page read, with that call's own request id
    r = sink.by_id(c.receipt_id)
    assert (r["endpoint"], r["source_url"], r["ok"] if "ok" in r else True) == (
        "/scrape",
        post_url,
        True,
    )
    assert r["call"]["request_id"] == call_ids(ox, "scrape")[post_url]
    assert r["summary"].startswith("@mira_test's X post read: The harbour ferry")
    # every call is one receipt, each search receipt carries its query and its own call
    searches = [r for r in sink.rows if r["endpoint"] == "/search"]
    assert len(searches) == 2 == len([k for k in ox.calls if k[0] == "search"])
    for r in searches:
        assert r["call"]["request_id"] == call_ids(ox, "search")[r["query"]]
    exact = next(r for r in searches if '"' in r["query"])
    assert exact["query"].startswith('site:x.com/mira_test "The harbour ferry timetable')
    assert (
        exact["summary"]
        == "Search of @mira_test's X posts for the screenshot's exact words: 3 results"
    )
    loose = next(r for r in searches if '"' not in r["query"])
    assert (
        loose["query"]
        == "site:x.com/mira_test harbour ferry timetable changes monday so early boat"
    )
    # the time from the id has its own receipt
    snow = [r for r in sink.rows if r["tool"] == "x-id"]
    assert snow == [
        {
            "tool": "x-id",
            "endpoint": "snowflake",
            "summary": "Exact time from the X post id: 2026-11-12T14:03:11.250000+00:00",
            "source_url": post_url,
        }
    ]
    # 2 searches + profile + 1 post page
    assert len(ox.calls) == 4 and len(sink.rows) == 5


def test_match_straight_from_profile_recent_posts():
    ox = FakeOxylabs(pages={PROFILE_URL: md("x_profile_recent.md")})
    cands, sink = run(ox)
    assert [c.id for c in cands] == [f"x:{MATCH}"]
    c = cands[0]
    assert c.source == "profile" and c.text == TEXT
    assert c.created_at == datetime(2026, 11, 12, 14, 3, 11, 250000, tzinfo=UTC)
    assert sink.by_id(c.receipt_id)["summary"] == "@mira_test's X profile read: 2 recent posts"
    m = c.metrics  # a profile item shows reposts, likes, views (no bookmarks)
    assert (m.views, m.replies, m.reposts, m.likes, m.bookmarks) == (48200, 31, 120, 904, None)
    scrapes = [arg for k, arg, _ in ox.calls if k == "scrape"]
    assert scrapes == [PROFILE_URL]  # no extra read of the post page


def test_hits_by_other_handles_are_ignored():
    # only nora_sample's copy (and a profile URL) are found: nothing of mira_test's to read
    ox = FakeOxylabs(
        exact=[h for h in hits("x_search_exact.json") if "mira_test/status" not in h.url],
        loose=[h for h in hits("x_search_loose.json") if "nora_sample" in h.url],
        pages={PROFILE_URL: md("x_profile_old.md")},
    )
    cands, sink = run(ox)
    assert cands == []
    assert [arg for k, arg, _ in ox.calls if k == "scrape"] == [PROFILE_URL]
    assert not [r for r in sink.rows if r["tool"] == "x-id"]


def test_twitter_com_hit_case_insensitive_handle_is_read():
    post_url = f"https://x.com/mira_test/status/{MATCH}"
    ox = FakeOxylabs(
        exact=[],
        loose=hits("x_search_loose.json"),
        pages={PROFILE_URL: md("x_profile_old.md"), post_url: md("x_post_match.md")},
    )
    cands, _ = run(ox)
    assert [c.id for c in cands] == [f"x:{MATCH}"]
    # the unrelated swim post shares no words with the screenshot: not read
    assert f"https://x.com/mira_test/status/{OLDER}" not in [a for _, a, _ in ox.calls]


def test_quoted_text_is_not_the_authors_text():
    quote_url = f"https://x.com/mira_test/status/{QUOTE}"
    ox = FakeOxylabs(
        exact=hits("x_search_quote.json"),
        loose=hits("x_search_quote.json"),
        pages={PROFILE_URL: md("x_profile_old.md"), quote_url: md("x_post_quote.md")},
    )
    cands, _ = run(ox, shot(text=OWN))
    assert [c.id for c in cands] == [f"x:{QUOTE}"]
    c = cands[0]
    assert c.text == OWN
    assert "bridge" not in c.text and QUOTED not in c.id
    # two unlabelled counts for three slots: ambiguous, so None (never guessed)
    m = c.metrics
    assert (m.views, m.replies, m.reposts, m.likes, m.bookmarks) == (3410, 9, None, None, None)


def test_one_search_fails_the_other_still_finds_it():
    post_url = f"https://x.com/mira_test/status/{MATCH}"
    ox = FakeOxylabs(
        exact=OxylabsError("oxylabs /search failed (500): request failed", status=500),
        loose=hits("x_search_loose.json"),
        pages={PROFILE_URL: md("x_profile_old.md"), post_url: md("x_post_match.md")},
    )
    cands, sink = run(ox)
    assert [c.id for c in cands] == [f"x:{MATCH}"]
    failed = [r for r in sink.rows if r.get("ok") is False]
    assert len(failed) == 1
    f = failed[0]
    assert f["endpoint"] == "/search" and f["query"].startswith('site:x.com/mira_test "')
    assert f["summary"].startswith(
        "Search of @mira_test's X posts for the screenshot's exact words failed: "
    )
    assert f["call"]["request_id"] == call_ids(ox, "search")[f["query"]]


def test_profile_error_and_missing_post_page_never_raise():
    ox = FakeOxylabs(exact=hits("x_search_exact.json"), loose=[], pages={})
    cands, sink = run(ox)
    assert cands == []
    failed = {r["endpoint"] for r in sink.rows if r.get("ok") is False}
    assert failed == {"/scrape"}
    assert len([r for r in sink.rows if r.get("ok") is False]) == 2  # profile + post page


def test_phase_one_runs_in_parallel():
    ox = FakeOxylabs(
        exact=hits("x_search_exact.json"),
        loose=hits("x_search_loose.json"),
        pages={PROFILE_URL: md("x_profile_recent.md")},
        delay=0.3,
    )
    t0 = time.monotonic()
    cands, _ = run(ox)
    assert time.monotonic() - t0 < 0.8  # serial would be 0.9 s
    assert [c.id for c in cands] == [f"x:{MATCH}"] and len(ox.calls) == 3


def test_phase_two_reads_run_in_parallel():
    ox = FakeOxylabs(
        exact=[
            SearchHit(
                url=f"https://x.com/mira_test/status/{MATCH}", title="", position=1, snippets=[TEXT]
            ),
            SearchHit(
                url=f"https://x.com/mira_test/status/{OLDER}",
                title="",
                position=2,
                snippets=[TEXT[:60]],
            ),
        ],
        loose=[],
        pages={
            PROFILE_URL: md("x_profile_old.md"),
            f"https://x.com/mira_test/status/{MATCH}": md("x_post_match.md"),
            f"https://x.com/mira_test/status/{OLDER}": md("x_post_match.md"),  # not on it
        },
        delay=0.3,
    )
    t0 = time.monotonic()
    cands, sink = run(ox)
    assert time.monotonic() - t0 < 1.1  # serial would be 1.5 s
    assert [c.id for c in cands] == [f"x:{MATCH}"]
    assert len([k for k in ox.calls if k[0] == "scrape"]) == 3
    # OLDER is on the profile with other words: a search snippet claiming the screenshot's
    # words does not make the profile item a match; its own page decides (and lacks it)
    missing = [r for r in sink.rows if r.get("ok") is False]
    assert len(missing) == 1 and "the post was not on it" in missing[0]["summary"]


def test_short_text_is_searched_quoted_and_loose():
    ox = FakeOxylabs(pages={PROFILE_URL: md("x_profile_old.md")})
    run(ox, shot(text="ferry at 6:40"))
    searches = sorted(arg for k, arg, _ in ox.calls if k == "search")
    assert searches == ['site:x.com/mira_test "ferry at 6:40"', "site:x.com/mira_test ferry 6 40"]
    assert [arg for k, arg, _ in ox.calls if k == "scrape"] == [PROFILE_URL]


def test_x_read_max_limits_page_reads():
    ox = FakeOxylabs(exact=hits("x_search_exact.json"), pages={PROFILE_URL: md("x_profile_old.md")})
    run(ox, x_read_max=0)
    assert [arg for k, arg, _ in ox.calls if k == "scrape"] == [PROFILE_URL]


def test_budget_exhausted_makes_no_calls():
    ox = FakeOxylabs(
        exact=hits("x_search_exact.json"), pages={PROFILE_URL: md("x_profile_recent.md")}
    )
    sink = Sink()
    deps = ReceiptDeps(receipt=sink, oxylabs=ox)
    with pytest.raises(ShotTimeUp):
        find_x(shot(), deps, Budget(deadline=time.monotonic() + 1.0))
    assert ox.calls == [] and sink.rows == []


@pytest.mark.parametrize("handle", ["", "has space", "much_too_long_handle_x", "dé"])
def test_invalid_handle_returns_nothing(handle):
    ox = FakeOxylabs()
    cands, sink = run(ox, shot(handle=handle))
    assert cands == [] and ox.calls == [] and sink.rows == []


# ---------------------------------------------------------------- review r1 regressions

NEAR = "2120880086184910000"  # a different recent post by mira_test, one word off


def test_profile_near_match_does_not_stop_the_exact_search_hit_being_read():
    near = md("x_profile_recent.md").replace(MATCH, NEAR).replace("on Monday", "on Tuesday")
    post_url = f"https://x.com/mira_test/status/{MATCH}"
    ox = FakeOxylabs(
        exact=hits("x_search_exact.json"),
        pages={PROFILE_URL: near, post_url: md("x_post_match.md")},
    )
    cands, _ = run(ox)
    assert cands[0].id == f"x:{MATCH}" and cands[0].text == TEXT  # the exact one wins
    assert f"x:{NEAR}" in {c.id for c in cands}  # the near match is still a candidate
    assert post_url in [arg for k, arg, _ in ox.calls if k == "scrape"]


def test_post_page_by_another_author_is_not_a_candidate():
    post_url = f"https://x.com/mira_test/status/{MATCH}"
    other = md("x_post_match.md").replace("mira_test", "nora_sample").replace("Mira", "Nora")
    ox = FakeOxylabs(
        exact=hits("x_search_exact.json"),
        pages={PROFILE_URL: md("x_profile_old.md"), post_url: other},
    )
    cands, sink = run(ox)
    assert f"x:{MATCH}" not in {c.id for c in cands}
    assert any("it is by @nora_sample, not @mira_test" in r["summary"] for r in sink.rows)


def test_every_receipt_is_written_before_return_even_past_the_deadline():
    ox = FakeOxylabs(
        exact=hits("x_search_exact.json"),
        pages={PROFILE_URL: md("x_profile_old.md")},
        delay=3.6,
    )
    sink = Sink()
    out = find_x(shot(), ReceiptDeps(receipt=sink, oxylabs=ox), Budget.seconds(3.3))
    at_return = list(sink.rows)
    time.sleep(0.6)  # the calls finish now, after find_x returned
    assert sink.rows == at_return  # nothing written late
    assert out == []
    assert len(at_return) == 3
    assert all(
        r["ok"] is False and r["summary"].endswith("no answer within the time budget")
        for r in at_return
    )


def test_profile_post_with_the_same_words_in_another_order_does_not_stop_the_search():
    swapped = TEXT.replace("6:40 instead of 6:15", "6:15 instead of 6:40")
    near = md("x_profile_recent.md").replace(MATCH, NEAR).replace(TEXT, swapped)
    assert swapped in near
    post_url = f"https://x.com/mira_test/status/{MATCH}"
    ox = FakeOxylabs(
        exact=hits("x_search_exact.json"),
        pages={PROFILE_URL: near, post_url: md("x_post_match.md")},
    )
    cands, _ = run(ox)
    assert post_url in [arg for k, arg, _ in ox.calls if k == "scrape"]
    assert f"x:{MATCH}" in {c.id for c in cands}


def test_budget_gone_between_phases_makes_the_lookup_incomplete():
    # phase 1 fits, but the post page read can't start: a failed receipt says so, so
    # find_original reports "unchecked", never "not found"
    from lookup.receipts import find_original

    ox = FakeOxylabs(
        exact=hits("x_search_exact.json"),
        pages={PROFILE_URL: md("x_profile_old.md")},
        delay=0.3,
    )
    sink = Sink()
    r = find_original(
        shot(), ReceiptDeps(receipt=sink, oxylabs=ox, anchor=datetime.now(UTC)), Budget.seconds(3.2)
    )
    assert r.label == "unchecked", (r.label, r.reason, sink.rows)
    assert any(
        row["ok"] is False and "not read, time budget used up" in row["summary"]
        for row in sink.rows
    )


def test_exact_profile_words_at_the_wrong_time_do_not_stop_the_search():
    # the author posted the same words again later; the screenshot shows the earlier date
    newer = "2121381335956615658"  # 13 Nov 2026, outside "Nov 12" ±14 h
    again = md("x_profile_recent.md").replace(MATCH, newer)
    post_url = f"https://x.com/mira_test/status/{MATCH}"
    ox = FakeOxylabs(
        exact=hits("x_search_exact.json"),
        pages={PROFILE_URL: again, post_url: md("x_post_match.md")},
    )
    s = shot()
    sink = Sink()
    deps = ReceiptDeps(receipt=sink, oxylabs=ox, anchor=datetime(2026, 11, 14, tzinfo=UTC))
    cands = find_x(s, deps, Budget.seconds(20))
    assert post_url in [arg for k, arg, _ in ox.calls if k == "scrape"]
    assert {f"x:{MATCH}", f"x:{newer}"} <= {c.id for c in cands}


def test_cut_off_screenshot_of_a_profile_post_is_found_end_to_end():
    from lookup.receipts import find_original

    ox = FakeOxylabs(pages={PROFILE_URL: md("x_profile_recent.md")})
    s = shot(text="The harbour ferry timetable changes on Monday…")
    sink = Sink()
    deps = ReceiptDeps(receipt=sink, oxylabs=ox, anchor=datetime(2026, 11, 14, tzinfo=UTC))
    r = find_original(s, deps, Budget.seconds(20))
    assert (r.label, r.original_id) == ("matches", f"x:{MATCH}"), (r.label, r.reason)
    assert sink.by_id(r.original_receipt_id)["endpoint"] == "/scrape"


def test_cut_off_screenshot_found_only_by_search_is_read_and_matches():
    from lookup.receipts import find_original

    post_url = f"https://x.com/mira_test/status/{MATCH}"
    ox = FakeOxylabs(
        exact=[],
        loose=[h for h in hits("x_search_exact.json") if MATCH in h.url],
        pages={PROFILE_URL: md("x_profile_old.md"), post_url: md("x_post_match.md")},
    )
    s = shot(text="The harbour ferry timetable…")
    deps = ReceiptDeps(receipt=Sink(), oxylabs=ox, anchor=datetime(2026, 11, 14, tzinfo=UTC))
    r = find_original(s, deps, Budget.seconds(20))
    assert post_url in [arg for k, arg, _ in ox.calls if k == "scrape"]
    assert (r.label, r.original_id) == ("matches", f"x:{MATCH}"), (r.label, r.reason)


def test_stop_word_only_profile_post_matches_end_to_end():
    from lookup.receipts import find_original

    page = md("x_profile_recent.md").replace(TEXT, "This is it.")
    ox = FakeOxylabs(pages={PROFILE_URL: page})
    deps = ReceiptDeps(receipt=Sink(), oxylabs=ox, anchor=datetime(2026, 11, 14, tzinfo=UTC))
    r = find_original(shot(text="This is it."), deps, Budget.seconds(20))
    assert (r.label, r.original_id) == ("matches", f"x:{MATCH}"), (r.label, r.reason)


# ---------------------------------------------------------------- review r6 regressions


def test_clipped_profile_post_is_read_on_its_page_before_judging():
    clipped = md("x_profile_recent.md").replace(" Plan ahead!", "")
    assert "Plan ahead!" not in clipped
    post_url = f"https://x.com/mira_test/status/{MATCH}"
    ox = FakeOxylabs(pages={PROFILE_URL: clipped, post_url: md("x_post_match.md")})
    from lookup.receipts import find_original

    deps = ReceiptDeps(receipt=Sink(), oxylabs=ox, anchor=datetime(2026, 11, 14, tzinfo=UTC))
    r = find_original(shot(), deps, Budget.seconds(20))
    assert post_url in [arg for k, arg, _ in ox.calls if k == "scrape"]
    assert (r.label, r.original_text) == ("matches", TEXT), (r.label, r.differences)


def test_clipped_profile_post_falls_back_to_the_profile_copy_when_its_page_fails():
    clipped = md("x_profile_recent.md").replace(" Plan ahead!", "")
    ox = FakeOxylabs(pages={PROFILE_URL: clipped})  # the post page 404s
    cands, _ = run(ox)
    assert [c.id for c in cands] == [f"x:{MATCH}"] and cands[0].source == "profile"


def test_stop_word_only_post_found_by_search_alone():
    from lookup.receipts import find_original

    post_url = f"https://x.com/mira_test/status/{MATCH}"
    hit = SearchHit(url=post_url, title='Mira Test on X: "This is it."', position=1)
    page = md("x_post_match.md").replace(TEXT, "This is it.")
    ox = FakeOxylabs(exact=[hit], pages={PROFILE_URL: md("x_profile_old.md"), post_url: page})
    deps = ReceiptDeps(receipt=Sink(), oxylabs=ox, anchor=datetime(2026, 11, 14, tzinfo=UTC))
    r = find_original(shot(text="This is it."), deps, Budget.seconds(20))
    assert 'site:x.com/mira_test "This is it"' in [arg for k, arg, _ in ox.calls if k == "search"]
    assert (r.label, r.original_id) == ("matches", f"x:{MATCH}"), (r.label, r.reason)


# ---------------------------------------------------------------- review r7 regression


def test_profile_variants_do_not_crowd_out_the_exact_search_hit():
    # the account posts near-identical updates; the screenshot shows the 6:40 one, which has
    # dropped off the profile but is in the search results
    from lookup.receipts import find_original

    v1, v2 = "2120880086184910001", "2120880086184910002"
    base = md("x_profile_recent.md")
    start = base.index("* [![@mira_test]")
    end = base.find("\n* ", start + 1)
    item = base[start : end if end != -1 else len(base)]
    variants = (
        item.replace(MATCH, v1).replace("6:40", "6:45")
        + "\n"
        + item.replace(MATCH, v2).replace("6:40", "6:50")
    )
    profile = base[:start] + variants + base[start + len(item) :]
    assert profile.count("6:45") == 1 and profile.count("6:50") == 1 and MATCH not in profile
    post_url = f"https://x.com/mira_test/status/{MATCH}"
    pages = {
        PROFILE_URL: profile,
        post_url: md("x_post_match.md"),
        f"https://x.com/mira_test/status/{v1}": md("x_post_match.md")
        .replace(MATCH, v1)
        .replace("6:40", "6:45"),
        f"https://x.com/mira_test/status/{v2}": md("x_post_match.md")
        .replace(MATCH, v2)
        .replace("6:40", "6:50"),
    }
    ox = FakeOxylabs(exact=[h for h in hits("x_search_exact.json") if MATCH in h.url], pages=pages)
    deps = ReceiptDeps(
        receipt=Sink(), oxylabs=ox, anchor=datetime(2026, 11, 14, tzinfo=UTC), x_read_max=2
    )
    r = find_original(shot(), deps, Budget.seconds(20))
    assert post_url in [arg for k, arg, _ in ox.calls if k == "scrape"]
    assert (r.label, r.original_id) == ("matches", f"x:{MATCH}"), (r.label, r.differences)


def test_call_finishing_just_after_the_wait_counts_as_started(monkeypatch):
    # a call that ends between wait() timing out and the state check started: it is
    # "no answer within the time budget", never "not read" (and never a ShotTimeUp)
    import lookup.receipts_x as rx

    real_wait = rx.wait

    def late_wait(fs, timeout=None):
        real_wait(fs)  # every call finishes, but the wait reports none in time
        return set(), set(fs)

    monkeypatch.setattr(rx, "wait", late_wait)
    ox = FakeOxylabs(exact=hits("x_search_exact.json"), pages={PROFILE_URL: md("x_profile_old.md")})
    cands, sink = run(ox)
    assert cands == []
    oxy = [r for r in sink.rows if r["tool"] == "oxylabs"]
    assert len(oxy) == 3
    assert all(
        r["ok"] is False and r["summary"].endswith("no answer within the time budget") for r in oxy
    )
