"""Same template, other content in X reply threads (lookup/threadvariants.py,
media/templatematch.py). No network: FxTwitter, the image CDN and Nemotron are fakes."""

from __future__ import annotations

import hashlib
import io

import httpx
from PIL import Image

from lookup.fxtwitter import FxTwitterClient
from lookup.models import ImageMatch, ImageRef, LookupInput
from lookup.service import Deps, Lookup
from media.templatematch import TemplateVerdict, pair, parse_verdict
from tests.test_lookup import SUBJ, FakeOxylabs

ROOT = "2121460294010704052"  # the thread the image's site search finds
REF_PHOTO = "https://pbs.twimg.com/media/REFPHOTO01.jpg?name=orig"


def _status(sid, handle, text, photos=(), replies=0, parent=None):
    return {
        "id": sid,
        "text": text,
        "author": {"screen_name": handle, "name": handle},
        "replies": replies,
        "media": {"photos": [{"url": u} for u in photos]},
        "replying_to": {"screen_name": parent[1], "status": parent[0]} if parent else None,
    }


def _reply(n, photo=None, parent=(ROOT, "campaign")):
    sid = str(2121470000000000000 + n)
    photos = [f"https://pbs.twimg.com/media/{photo}.jpg?name=orig"] if photo else []
    return _status(sid, f"user{n}", f"@campaign reply {n}", photos, parent=parent)


class FakeFx:
    def __init__(self, convs):
        self.convs, self.calls = convs, []
        self.last_call = {"endpoint": "/2/conversation", "latency_ms": 2}

    def conversation(self, sid, *, cursor=None, deadline=None):
        self.calls.append((sid, cursor))
        pages = self.convs[sid]
        data = pages[int(cursor or 0)]
        return FxTwitterClient._conversation(data, with_replies=True)

    def status(self, sid, *, deadline=None):
        self.calls.append((sid, "status"))
        return FxTwitterClient._conversation(self.convs[sid][0], with_replies=False)


def _png(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, format="PNG")
    return buf.getvalue()


TPL_BYTES = _png("green")


def _cdn(seen: list):
    """pbs.twimg.com: /media/TPL* are template pictures, anything else gray."""

    def handler(req: httpx.Request):
        seen.append(req.url.path)
        body = TPL_BYTES if "/TPL" in req.url.path else _png("gray")
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    return httpx.MockTransport(handler)


class FakeMatcher:
    """Says a picture is the same template when its bytes are the 'template' colour."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, client, ref, cand, *, timeout_s):
        self.calls.append(cand)
        if self.fail:
            return None
        same = cand == TPL_BYTES
        differs = 'shows "Xi Jingping" instead of "Dario Amodei"' if same else ""
        return TemplateVerdict(same, differs, same), 410, "nemotron-3.5-super-vl"


class SiteOxylabs(FakeOxylabs):
    def search(self, query, max_results=10, deadline=None):
        from sources.models import SearchHit

        self.calls.append(("search", query))
        if query.startswith("site:x.com"):
            return [
                SearchHit(
                    url=f"https://x.com/campaign/status/{ROOT}",
                    title='campaign on X: "We\'re removing fake entries from example-site.org"',
                    snippets=["We're removing fake entries from example-site.org"],
                    position=1,
                )
            ]
        return []


class SiteReader:
    def analyze(self, media, *, context=None):
        from media.nemotron import MediaAnalysis

        return MediaAnalysis(
            media_hash=media.media_hash, description="a screenshot of a signer search",
            extracted_text=["example-site.org", "Search public signers", "Dario Amodei"],
            reuse_cues=[], manipulation_cues=[], confidence=0.9, model="nemotron-3.5-super-vl",
            latency_ms=500, media_type="screenshot",
        )  # fmt: skip


def _upload_job(convs, matcher, cdn_seen):
    data = _png("black")
    ref = ImageRef(sha256=hashlib.sha256(data).hexdigest(), mime="image/png", bytes=len(data))
    fx = FakeFx(convs)
    deps = Deps(
        oxylabs=SiteOxylabs(),
        fxtwitter=fx,
        nemotron=SiteReader(),
        match_templates=matcher,
        image_transport=_cdn(cdn_seen),
    )
    job = Lookup("lk_tpl", LookupInput(image=ref), deps, image=data)
    job.run()
    return job, fx


def test_upload_finds_same_template_pictures_in_the_reply_thread():
    root = _status(ROOT, "campaign", "We're removing fake entries from example-site.org", replies=9)
    replies = [
        _reply(1, "TPL0001"),
        _reply(2, "CAT0002"),
        _reply(3),  # no picture
        _reply(4, "TPL0001"),  # the same picture again: compared once
        _reply(5, "TPL0005"),
    ]
    convs = {ROOT: [{"status": root, "replies": replies, "cursor": {"bottom": "1"}}]}
    matcher = FakeMatcher()
    seen: list = []
    job, fx = _upload_job(convs, matcher, seen)
    r = job.result
    st = job._step("thread_variants")
    assert st.state == "done", st.detail
    assert (ROOT, None) in fx.calls and (ROOT, "1") not in fx.calls  # one page for a found post
    assert len(matcher.calls) == 3  # one call per picture
    tpl = [m for m in r.image_matches if m.relation == "template"]
    assert [m.page_url for m in tpl] == [
        "https://x.com/user1/status/2121470000000000001",
        "https://x.com/user5/status/2121470000000000005",
    ]
    m = tpl[0]
    assert m.engine == "x_replies" and m.time_source == "snowflake" and m.first_seen
    assert m.changes == ['shows "Xi Jingping" instead of "Dario Amodei"']
    assert m.relation_note.startswith("Reply by @user1: same template, other content")
    assert st.found == [x.id for x in tpl]
    assert "3 reply pictures compared · 2 show the same template" in st.detail
    # never the origin of the picture, and linked to the upload as a variant
    assert r.image_origin_id is None
    assert {(e.from_, e.to, e.rel) for e in r.edges} >= {(r.subject_id, m.id, "variant")}
    # receipts: the thread read through FxTwitter, the comparison by Nemotron
    tools = [(x.tool, x.endpoint) for x in r.receipts]
    assert ("fxtwitter", "/2/conversation") in tools
    cmp = next(x for x in r.receipts if x.id == m.receipt_id)
    assert cmp.tool == "nemotron" and "3 reply pictures" in cmp.summary
    assert "2 show the same template" in cmp.summary
    assert all("/media/" in p for p in seen) and len(seen) == 3


def test_submitted_post_thread_reads_two_pages_and_skips_its_own_picture():
    subj = _status(SUBJ, "ada_example", "Look at this page", [REF_PHOTO], replies=40)
    page0 = [_reply(i, f"TPL{i:04d}", parent=(SUBJ, "ada_example")) for i in range(1, 6)]
    page0.append(_reply(6, "REFPHOTO01", parent=(SUBJ, "ada_example")))  # the post's own photo
    page1 = [_reply(i, f"TPL{i:04d}", parent=(SUBJ, "ada_example")) for i in range(7, 9)]
    page1.append(_reply(9, "CAT0009", parent=(SUBJ, "ada_example")))
    convs = {
        SUBJ: [
            {"status": subj, "replies": page0, "cursor": {"bottom": "1"}},
            {"status": subj, "replies": page1, "cursor": {}},
        ]
    }
    matcher = FakeMatcher()
    seen: list = []
    fx = FakeFx(convs)
    job = Lookup(
        "lk_tpl_post",
        LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"),
        Deps(
            oxylabs=FakeOxylabs(),
            fxtwitter=fx,
            nemotron=object(),  # present; the media step's own reads fail harmlessly
            match_templates=matcher,
            image_transport=_cdn(seen),
            media_max=0,
        ),
    )
    job.run()
    st = job._step("thread_variants")
    assert st.state == "done", st.detail
    assert (SUBJ, "1") in fx.calls  # the second page of the post's own thread
    assert len(matcher.calls) == 8  # 5 + 3 pictures; the post's own photo is not compared
    tpl = [m for m in job.result.image_matches if m.relation == "template"]
    assert len(tpl) == 7
    assert not any("REFPHOTO01" in (m.image_url or "") for m in tpl)
    assert any(p.endswith("/REFPHOTO01.jpg") for p in seen)  # the reference was downloaded


def test_failed_comparison_is_recorded_and_finds_nothing():
    root = _status(ROOT, "campaign", "We're removing fake entries from example-site.org", replies=2)
    convs = {ROOT: [{"status": root, "replies": [_reply(1, "TPL0001")], "cursor": {}}]}
    job, _ = _upload_job(convs, FakeMatcher(fail=True), [])
    st = job._step("thread_variants")
    assert st.state == "done" and "0 reply pictures compared" in st.detail
    assert not [m for m in job.result.image_matches if m.relation == "template"]
    rec = next(x for x in job.result.receipts if "one by one" in x.summary)
    assert not rec.ok and "1 comparisons failed" in rec.summary


def test_without_fxtwitter_or_picture_the_step_says_why():
    job = Lookup(
        "lk_np",
        LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"),
        Deps(oxylabs=FakeOxylabs()),
    )
    job.run()  # the post has a picture, but there is no FxTwitter to read its replies
    st = job._step("thread_variants")
    assert (st.state, st.detail) == ("skipped", "FxTwitter is not configured on this server")

    subj = _status(SUBJ, "ada_example", "Just words, no picture")
    fx = FakeFx({SUBJ: [{"status": subj, "replies": [], "cursor": {}}]})
    job = Lookup(
        "lk_np2",
        LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"),
        Deps(oxylabs=FakeOxylabs(), fxtwitter=fx),
    )
    job.run()
    st = job._step("thread_variants")
    assert (st.state, st.detail) == ("skipped", "the post has no picture to look for")


def test_reverse_image_sync_keeps_thread_pictures():
    from lookup.imagetrace import Trace

    job = Lookup("lk_sync", LookupInput(text="one two three four five six"), Deps(oxylabs=None))
    tpl = ImageMatch(id="xr:1:ab", image_url=REF_PHOTO, engine="x_replies", relation="template")
    job._publish_templates([tpl])
    trace = Trace(job)
    trace.matches = {
        "img:1": ImageMatch(id="img:1", image_url="https://a.example/x.jpg", engine="tineye")
    }
    trace.priorities = {"img:1": (0,)}
    trace._sync_matches()
    assert [m.id for m in job.result.image_matches] == ["img:1", "xr:1:ab"]


# ---------------------------------------------------------------- media/templatematch.py


def test_parse_verdict():
    assert parse_verdict(
        {"same_site": True, "same_screen": True, "differs": ' shows  "Xi"\n instead '}
    ) == TemplateVerdict(True, 'shows "Xi" instead', True)
    # same site, another page: not the same template, and no "differs" kept
    assert parse_verdict(
        {"same_site": True, "same_screen": False, "differs": "a confirmation page"}
    ) == TemplateVerdict(False, "", True)
    assert parse_verdict(
        {"same_site": False, "same_screen": True, "differs": "No visible difference."}
    ) == TemplateVerdict(True, "", False)
    assert parse_verdict({"same_screen": "yes"}) is None
    assert parse_verdict(None) is None and parse_verdict(["x"]) is None


def test_pair_lays_out_reference_and_candidate():
    ref = Image.new("RGB", (600, 400), "white")
    cand = Image.new("RGB", (300, 500), "black")
    p = pair(ref, cand)
    assert (p.width, p.height) == (2 * 640 + 16, 640 + 34)


# ---------------------------------------------------------------- review round 1


def test_unchanged_copy_is_not_a_template_match():
    """The same layout with nothing different is a copy of the picture, not another version."""

    class SameMatcher(FakeMatcher):
        def __call__(self, client, ref, cand, *, timeout_s):
            self.calls.append(cand)
            return TemplateVerdict(True, "", True), 300, "nemotron-3.5-super-vl"

    root = _status(ROOT, "campaign", "We're removing fake entries from example-site.org", replies=1)
    convs = {ROOT: [{"status": root, "replies": [_reply(1, "TPL0001")], "cursor": {}}]}
    job, _ = _upload_job(convs, SameMatcher(), [])
    assert not [m for m in job.result.image_matches if m.relation == "template"]
    rec = next(x for x in job.result.receipts if "one by one" in x.summary)
    assert "1 looked unchanged (not counted)" in rec.summary


def test_fxtwitter_outage_fails_the_step_with_receipts():
    from lookup.fxtwitter import FxTwitterError

    class DownFx(FakeFx):
        def conversation(self, sid, *, cursor=None, deadline=None):
            raise FxTwitterError("HTTP 503")

    data = _png("black")
    ref = ImageRef(sha256=hashlib.sha256(data).hexdigest(), mime="image/png", bytes=len(data))
    job = Lookup(
        "lk_down",
        LookupInput(image=ref),
        Deps(
            oxylabs=SiteOxylabs(),
            fxtwitter=DownFx({}),
            nemotron=SiteReader(),
            match_templates=FakeMatcher(),
            image_transport=_cdn([]),
        ),
        image=data,
    )
    job._caption = "example-site.org Search public signers"
    job._domains = ["example-site.org"]
    from lookup.models import Post

    job.add_post(Post(id=f"x:{ROOT}", platform="x", text="example-site.org"))
    job._thread_variants()
    st = job._step("thread_variants")
    assert (st.state, st.detail) == ("failed", "no reply thread could be read (1 tried)")
    bad = [x for x in job.result.receipts if x.tool == "fxtwitter" and not x.ok]
    assert bad and "FxTwitter did not answer" in bad[0].summary


def test_a_fast_match_is_published_while_a_slow_comparison_runs_out_of_time():
    import threading
    import time as _time

    from lookup.threadvariants import ThreadScan

    release = threading.Event()

    class SplitMatcher:
        def __call__(self, client, ref, cand, *, timeout_s):
            if cand == TPL_BYTES:
                return TemplateVerdict(True, 'shows "Xi" instead', True), 200, "m"
            release.wait(10)  # never answers within the lookup's time
            return TemplateVerdict(False, "", False), 9000, "m"

    data = _png("black")
    ref = ImageRef(sha256=hashlib.sha256(data).hexdigest(), mime="image/png", bytes=len(data))
    job = Lookup(
        "lk_slow",
        LookupInput(image=ref),
        Deps(oxylabs=None, nemotron=object(), match_templates=SplitMatcher()),
        image=data,
    )
    job.deadline = _time.monotonic() + 10 + 3.0  # 3 s of provider time left
    scan = ThreadScan(job, (data, None))
    fast = (_reply(1, "TPL0001"), "https://pbs.twimg.com/media/TPL0001.jpg", ROOT)
    slow = (_reply(2, "CAT0002"), "https://pbs.twimg.com/media/CAT0002.jpg", ROOT)
    from lookup.fxtwitter import to_item

    usable = [((to_item(c[0]), c[1], c[2]), b) for c, b in ((fast, TPL_BYTES), (slow, _png("red")))]
    t0 = _time.monotonic()
    try:
        found = scan._compare(data, usable)
    finally:
        release.set()
    assert _time.monotonic() - t0 < 4.5  # ended at the deadline, not after the slow call
    assert [m.page_url for m in found] == ["https://x.com/user1/status/2121470000000000001"]
    assert [m.id for m in job.result.image_matches] == [found[0].id]
    assert (scan.compared, scan.late) == (1, 1)
    rec = next(x for x in job.result.receipts if "one by one" in x.summary)
    assert "1 not answered in time" in rec.summary
