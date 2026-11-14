"""lookup's "screenshot" step: a screenshot in the submitted post is checked against the real
post (lookup/receipts_step.py). No network: X pages, images, Nemotron and find_original are fakes.
"""

import json

import httpx
import pytest

from lookup import receipts_step
from lookup.assess import _Draft, brief, guard
from lookup.models import LookupInput, Metrics, ScreenshotReceipt
from lookup.receipts import ScreenshotPost
from lookup.service import STEPS, Deps, Lookup
from lookup.urls import snowflake_time
from media.nemotron import MediaAnalysis
from tests.test_lookup import SUBJ, TEXT, FakeOxylabs

ORIG = "2121350000000000000"  # earlier than SUBJ: would be the origin if it counted as a copy
SHOT = ScreenshotPost(
    media_hash="lookup:x",
    platform="x",
    display_name="Ada Example",
    handle="ada_example",
    shown_time="9:10 PM · Nov 13, 2026",
    text=TEXT,
    counts={"likes": 35000, "reposts": None},
    ui_oddities=[],
    confidence=0.9,
    model="nvidia/nemotron-3.5-super-vl",
    latency_ms=1200,
)


class FakeNemotron:
    def __init__(self, media_type="screenshot", shot=SHOT):
        self.media_type, self.shot, self.reads = media_type, shot, []

    def analyze(self, media, *, context=None):
        return MediaAnalysis(
            media_hash=media.media_hash,
            description="A screenshot of an X post.",
            extracted_text=[TEXT],
            reuse_cues=[],
            manipulation_cues=[],
            confidence=0.8,
            model="nvidia/nemotron-3.5-super-vl",
            latency_ms=900,
            media_type=self.media_type,
        )

    def read_screenshot(self, media):
        self.reads.append(media)
        return self.shot


class NoReaderNemotron(FakeNemotron):
    read_screenshot = None  # an older client: getattr finds nothing callable


def _http():
    def handler(req):
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8img")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _canned(label="matches"):
    def find_original(shot, deps, budget):
        assert shot is SHOT and deps.anchor == snowflake_time(SUBJ) and budget.left() > 0
        rid = deps.receipt(
            "oxylabs",
            "/scrape",
            f"@{shot.handle}'s post read",
            source_url=f"https://x.com/ada_example/status/{ORIG}",
        )
        if label == "not_found":
            return ScreenshotReceipt(
                media_hash=shot.media_hash,
                platform="x",
                handle=shot.handle,
                label="not_found",
                reason="no matching post found: it may be deleted, private or never posted",
                receipt_ids=[rid],
            )
        return ScreenshotReceipt(
            media_hash=shot.media_hash,
            platform="x",
            handle=shot.handle,
            shown_text=shot.text,
            label=label,
            reason="same words",
            original_id=f"x:{ORIG}",
            original_url=f"https://x.com/ada_example/status/{ORIG}",
            original_text=TEXT,
            original_created_at=snowflake_time(ORIG),
            original_time_source="snowflake",
            original_receipt_id=rid,
            similarity=1.0,
            time_check="consistent",
            counts_now=Metrics(likes=35100),
            candidates_checked=1,
            receipt_ids=[rid],
        )

    return find_original


def _run(monkeypatch, nemotron, label="matches", url=f"https://x.com/ada_example/status/{SUBJ}"):
    monkeypatch.setattr(receipts_step, "find_original", _canned(label))
    deps = Deps(oxylabs=FakeOxylabs(), nemotron=nemotron, http=_http())
    job = Lookup("lk_s", LookupInput(url=url), deps)
    job.run()
    return job


def _step(job, sid="screenshot"):
    return next(s for s in job.result.steps if s.id == sid)


def test_step_order():
    ids = [s for s, _, _ in STEPS]
    assert ids[ids.index("media") + 1] == "screenshot"


def test_screenshot_matches_real_post(monkeypatch):
    nem = FakeNemotron()
    job = _run(monkeypatch, nem)
    r = job.result
    st = _step(job)
    assert st.state == "done", st.detail
    assert st.detail == "1 screenshot: matches @ada_example's real post"
    assert st.found == [f"x:{ORIG}"]
    assert nem.reads and nem.reads[0].media_hash.startswith("lookup:")
    # the receipt carries where the screenshot is and the Nemotron read's receipt
    [shot] = r.screenshots
    assert shot.media_url.startswith("https://pbs.twimg.com/media/HXAAAA")
    assert shot.post_id == r.subject_id == f"x:{SUBJ}"
    read = next(x for x in r.receipts if x.id == shot.receipt_ids[0])
    assert read.tool == "nemotron" and read.endpoint == "chat.completions"
    assert read.summary.startswith("Nemotron read the screenshot: @ada_example on X, 'The comets")
    assert read.latency_ms == 1200
    assert all(any(x.id == i for x in r.receipts) for i in shot.receipt_ids)
    # the real post joins the graph, linked from the post that carries the screenshot
    orig = next(p for p in r.posts if p.id == f"x:{ORIG}")
    assert orig.role == "screenshotted" and orig.verified and orig.author_handle == "ada_example"
    assert orig.metrics.likes == 35100 and orig.receipt_id == shot.original_receipt_id is not None
    edge = next(e for e in r.edges if e.rel == "screenshot_of")
    assert (edge.from_, edge.to, edge.tool, edge.note) == (
        f"x:{SUBJ}",
        f"x:{ORIG}",
        "nemotron",
        "matches",
    )
    # older and with the same words, yet never a copy, in the spread or the origin
    assert r.origin_id == f"x:{SUBJ}"
    assert r.spread.n_posts == 1
    assert not any(e.rel == "copy" and f"x:{ORIG}" in (e.from_, e.to) for e in r.edges)
    snap = job.snapshot()
    json.dumps(snap)
    assert snap["screenshots"][0]["label"] == "matches"
    assert any(e["rel"] == "screenshot_of" and e["from"] == f"x:{SUBJ}" for e in snap["edges"])


def test_not_found_adds_no_post(monkeypatch):
    job = _run(monkeypatch, FakeNemotron(), label="not_found")
    st = _step(job)
    assert st.state == "done" and st.detail == "1 screenshot: no matching post found"
    assert job.result.screenshots[0].label == "not_found"
    assert not any(p.role == "screenshotted" for p in job.result.posts)
    assert not any(e.rel == "screenshot_of" for e in job.result.edges)


def test_image_that_is_not_a_post(monkeypatch):
    job = _run(monkeypatch, FakeNemotron(shot=None))
    st = _step(job)
    assert st.state == "done" and st.detail == "Nemotron found no post in the image"
    assert job.result.screenshots == []
    assert any(x.summary.endswith("no social post in it") for x in job.result.receipts)


@pytest.mark.parametrize(
    ("nemotron", "url", "why"),
    [
        (FakeNemotron("photo"), None, "no image is a screenshot of a post"),
        (NoReaderNemotron(), None, "screenshot reader not available on this server"),
        (None, None, "Nemotron is not configured on this server"),
        (FakeNemotron(), "https://x.com/ada_example", "profile lookup"),
        (FakeNemotron(), "https://news.example/story", "the post has no images"),
    ],
)
def test_skipped_with_reason(monkeypatch, nemotron, url, why):
    job = _run(monkeypatch, nemotron, url=url or f"https://x.com/ada_example/status/{SUBJ}")
    st = _step(job)
    assert (st.state, st.detail) == ("skipped", why)
    assert job.result.screenshots == []


def test_budget_used_up_is_unchecked(monkeypatch):
    nem = FakeNemotron()
    job = Lookup(
        "lk_b",
        LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"),
        Deps(oxylabs=FakeOxylabs(), nemotron=nem, http=_http()),
    )
    job.result.subject_id = f"x:{SUBJ}"
    job._shots = [("https://pbs.twimg.com/media/A?format=jpg", "ab", "image/jpeg", b"x")]
    job.deadline = 0  # long past
    receipts_step.run(job)
    st = _step(job)
    assert st.state == "done" and st.detail == "1 screenshot: unchecked: time budget used up"
    assert job.result.screenshots[0].label == "unchecked" and not nem.reads


def test_brief_carries_screenshots_and_guard_keeps_their_receipts(monkeypatch):
    job = _run(monkeypatch, FakeNemotron())
    r = job.result
    data = json.loads(brief(r))
    [s] = data["screenshots"]
    assert s["label"] == "matches" and s["handle"] == "ada_example"
    assert s["original_url"].endswith(ORIG) and s["receipt_ids"] == r.screenshots[0].receipt_ids
    assert set(s) == {
        "label",
        "reason",
        "platform",
        "handle",
        "shown_time",
        "time_check",
        "similarity",
        "differences",
        "original_url",
        "receipt_ids",
    }
    d = _Draft(
        summary="s",
        origin="o",
        corroboration="c",
        spread_pattern="p",
        cites=[*s["receipt_ids"], 999],
        llm_reading="organic_looking",
    )
    a = guard(d, r, model="m")
    assert a.cites == s["receipt_ids"] and a.llm_reading == "organic_looking"


class ImageOnlyOxylabs(FakeOxylabs):
    def scrape_markdown(self, url, deadline=None):
        md, meta = super().scrape_markdown(url, deadline)
        if url.endswith(SUBJ):  # the post is only its image (a screenshot)
            md = md.replace(f'# Ada Example on X: "{TEXT}"', "# Ada Example on X")
            md = md.replace(TEXT, "")
        return md, meta


def test_image_only_post_is_checked_alongside_the_searches(monkeypatch):
    monkeypatch.setattr(receipts_step, "find_original", _canned())
    deps = Deps(oxylabs=ImageOnlyOxylabs(), nemotron=FakeNemotron(), http=_http())
    job = Lookup("lk_i", LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"), deps)
    job.run()
    assert _step(job, "media").state == "done"
    st = _step(job)
    assert st.state == "done" and st.detail == "1 screenshot: matches @ada_example's real post"
    assert any("text Nemotron read inside its image" in n for n in job.result.notes)
    assert any(p.role == "screenshotted" for p in job.result.posts)


# --- end to end: the real find_original and Bluesky resolver over recorded-shape fixtures ------


def _bsky_fixture_client():
    from pathlib import Path

    from lookup.bsky import BlueskyClient

    fix = Path(__file__).parent / "fixtures" / "receipts"
    routes = {
        "com.atproto.identity.resolveHandle": "bsky_resolve_ada.json",
        "app.bsky.feed.searchPosts": "bsky_search_ada.json",
        "app.bsky.feed.getAuthorFeed": "bsky_feed_ada.json",
    }

    def handler(req):
        name = routes[req.url.path.rsplit("/", 1)[-1]]
        return httpx.Response(200, json=json.loads((fix / name).read_text()))

    return BlueskyClient(transport=httpx.MockTransport(handler))


BSKY_TEXT = (
    "The lighthouse keepers' union meets on Thursday to vote on the new night shift rota "
    "for the harbour"
)


@pytest.mark.parametrize(
    "text,label",
    [(BSKY_TEXT, "matches"), (BSKY_TEXT.replace("Thursday", "Friday"), "differs")],
)
def test_end_to_end_bluesky_screenshot_in_an_x_post(text, label):
    shot = ScreenshotPost(
        media_hash="lookup:x",
        platform="bluesky",
        display_name="Ada Example",
        handle="ada.example.com",
        shown_time="Nov 13, 2026 at 2:14 PM",
        text=text,
        counts={"likes": 57},
        ui_oddities=[],
        confidence=0.9,
        model="nvidia/nemotron-3.5-super-vl",
        latency_ms=1200,
    )
    deps = Deps(
        oxylabs=FakeOxylabs(),
        bsky=_bsky_fixture_client(),
        nemotron=FakeNemotron(shot=shot),
        http=_http(),
    )
    job = Lookup("lk_e2e", LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"), deps)
    job.run()
    snap = job.snapshot()
    [r] = snap["screenshots"]
    assert r["label"] == label, r
    assert r["original_url"].startswith("https://bsky.app/profile/ada.example.com/post/")
    assert r["time_check"] == "consistent"
    assert r["counts_note"]  # counts are context only
    ids = {x["id"]: x for x in snap["receipts"]}
    endpoints = {ids[i]["endpoint"] for i in r["receipt_ids"]}
    assert {"chat.completions", "resolveHandle", "searchPosts", "getAuthorFeed"} <= endpoints
    assert any(e["rel"] == "screenshot_of" and e["to"] == r["original_id"] for e in snap["edges"])
    [orig] = [p for p in snap["posts"] if p["id"] == r["original_id"]]
    assert orig["role"] == "screenshotted"
    # the real post cites the fetch that read it, not just the check's last receipt
    assert orig["receipt_id"] == r["original_receipt_id"]
    assert ids[orig["receipt_id"]]["endpoint"] in ("searchPosts", "getAuthorFeed")
    if label == "differs":
        ops = {(d["op"], d["text"]) for d in r["diff"]}
        assert ("shot_only", "Friday") in ops and ("post_only", "Thursday") in ops
    assert "fake" not in json.dumps(r).lower()


# --- review r1 regressions ---------------------------------------------------------------------


def _graph_lookup():
    from lookup.models import Post

    job = Lookup("lk_g", LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"), Deps(None))
    job.result.subject_id = f"x:{SUBJ}"
    job.add_post(
        Post(
            id=f"x:{SUBJ}",
            platform="x",
            author_handle="ada_example",
            text=TEXT,
            created_at=snowflake_time(SUBJ),
            time_source="snowflake",
            role="submitted",
            verified=True,
        )
    )
    copy = Post(
        id=f"x:{ORIG}",
        platform="x",
        url=f"https://x.com/ada_example/status/{ORIG}",
        author_handle="ada_example",
        text=TEXT,
        created_at=snowflake_time(ORIG),
        time_source="snowflake",
        role="copy",
        similarity=1.0,
        verified=True,
    )
    return job, copy


@pytest.mark.parametrize("copy_first", [True, False])
def test_screenshot_original_is_never_a_copy_or_origin_in_either_order(copy_first):
    job, copy = _graph_lookup()
    rec = _canned()(SHOT, _AnchorDeps(job), _Left())
    if copy_first:
        job.add_post(copy)
        receipts_step._record(job, rec, "https://pbs.twimg.com/media/x.jpg")
    else:
        receipts_step._record(job, rec, "https://pbs.twimg.com/media/x.jpg")
        job.add_post(copy)
    job._finish_graph()
    [orig] = [p for p in job.result.posts if p.id == f"x:{ORIG}"]
    assert orig.role == "screenshotted"
    assert job.result.origin_id != f"x:{ORIG}"
    assert job.result.spread is None or job.result.spread.n_posts == 1


class _AnchorDeps:
    def __init__(self, job):
        self.anchor = snowflake_time(SUBJ)
        self.receipt = job.receipt


class _Left:
    def left(self):
        return 10.0


def test_reader_gets_the_provider_deadline_when_it_takes_one(monkeypatch):
    class DeadlineNemotron(FakeNemotron):
        def read_screenshot(self, media, *, deadline=None):
            self.reads.append(media)
            self.deadline = deadline
            return self.shot

    nem = DeadlineNemotron()
    job = _run(monkeypatch, nem)
    assert isinstance(nem.deadline, float)
    assert abs(nem.deadline - (job._provider_deadline() - receipts_step.CUTOFF_GRACE_S)) < 1e-6
    assert _step(job).state == "done"


def test_receipts_after_the_step_stops_waiting_are_dropped():
    job, _ = _graph_lookup()
    sink = receipts_step._Sink(job)
    assert sink("nemotron", "chat.completions", "in time") == 1
    sink.close()
    assert sink("oxylabs", "/search", "straggler") == 0
    assert [r.summary for r in job.result.receipts] == ["in time"]


def test_a_check_still_running_at_the_cutoff_leaves_a_failed_receipt(monkeypatch):
    import threading
    import time

    from lookup.service import ASSESS_RESERVE_S

    gate = threading.Event()

    class SlowNemotron(FakeNemotron):
        def read_screenshot(self, media, *, deadline=None):
            gate.wait(5)
            return None

    deps = Deps(oxylabs=FakeOxylabs(), nemotron=SlowNemotron(), http=_http())
    job = Lookup("lk_slow", LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"), deps)
    job._spend = lambda *a, **k: None  # let the read start; the cutoff is what's tested
    job._shots = [("https://pbs.twimg.com/media/s.jpg", "h1", "image/jpeg", b"img")]
    job.deadline = time.monotonic() + ASSESS_RESERVE_S + 1.5
    try:
        receipts_step.run(job)
    finally:
        gate.set()
    [rec] = job.result.screenshots
    assert rec.label == "unchecked" and rec.reason == "time budget used up"
    [rid] = rec.receipt_ids
    r = next(x for x in job.result.receipts if x.id == rid)
    assert r.ok is False and "no answer within the time budget" in r.summary


def test_date_judgement_cues_are_dropped_from_the_image_reading(monkeypatch):
    class CueNemotron(FakeNemotron):
        def analyze(self, media, *, context=None):
            a = super().analyze(media, context=context)
            from dataclasses import replace

            return replace(
                a,
                manipulation_cues=[
                    "Future date (Nov 14, 2026) suggests fabricated or mock-up context",
                    "Cropped edges",
                ],
            )

    job = _run(monkeypatch, CueNemotron())
    [m] = job.result.media
    assert m.manipulation_cues == ["Cropped edges"]
