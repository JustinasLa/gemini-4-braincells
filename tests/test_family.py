"""lookup/family.py: copies of one photograph sorted into a family tree, with fakes for the
image trace, Nemotron and the encyclopedia sources."""

import io
import threading
import time
from concurrent.futures import Future
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from lookup import family
from lookup.models import ImageMatch, LookupInput, LookupResult, Step
from media.memeread import MemeRead, MemeText


def photo(seed: int, size=(640, 427)) -> Image.Image:
    rng = np.random.default_rng(seed)
    image = Image.fromarray(rng.integers(0, 255, (size[1] // 8, size[0] // 8, 3), dtype=np.uint8))
    image = image.resize(size, Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(1.2))
    draw = ImageDraw.Draw(image)
    for _ in range(40):
        x, y = rng.integers(0, size[0]), rng.integers(0, size[1])
        w, h = rng.integers(10, 80), rng.integers(10, 80)
        draw.rectangle((x, y, x + w, y + h), fill=tuple(int(v) for v in rng.integers(0, 255, 3)))
    return image


def jpeg(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=85)
    return out.getvalue()


def text_on(image, top, bottom=""):
    image = image.copy()
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, image.width, 50), fill="white")
    draw.text((10, 15), top, fill="black")
    if bottom:
        draw.rectangle((0, image.height - 50, image.width, image.height), fill="white")
        draw.text((10, image.height - 35), bottom, fill="black")
    return image


def poster(image, title):
    frame = Image.new("RGB", (image.width + 160, image.height + 220), "black")
    frame.paste(image, (80, 60))
    ImageDraw.Draw(frame).text((frame.width // 2 - 40, image.height + 100), title, fill="white")
    return frame


BASE = photo(7)
UPLOAD = jpeg(text_on(BASE, "THE SENATOR WATCHING", "HIS OWN BILL PASS"))
COPIES = {
    "img:1": (jpeg(BASE), datetime(2008, 7, 17, tzinfo=UTC), "zooomr.com"),
    "img:2": (jpeg(poster(BASE, "BOREDOM")), datetime(2009, 6, 1, tzinfo=UTC), "pk.com"),
    "img:3": (jpeg(poster(BASE, "GOOGLE")), datetime(2009, 6, 2, tzinfo=UTC), "pk.com"),
    "img:4": (jpeg(text_on(BASE, "USA 2020")), datetime(2020, 9, 1, tzinfo=UTC), "imgur.com"),
    "img:5": (jpeg(BASE.resize((320, 213))), datetime(2009, 7, 7, tzinfo=UTC), "fark.com"),
    "img:6": (jpeg(photo(99)), datetime(2005, 1, 1, tzinfo=UTC), "elsewhere.com"),
}
READS = {
    UPLOAD: MemeRead(
        text=[MemeText("THE SENATOR WATCHING", "top"), MemeText("HIS OWN BILL PASS", "bottom")],
        template="Disaster Girl",
        aimed_at="a senator",
    ),
    COPIES["img:1"][0]: MemeRead(),
    COPIES["img:2"][0]: MemeRead(text=[MemeText("BOREDOM", "below_frame")], frame="poster"),
    COPIES["img:3"][0]: MemeRead(
        text=[MemeText("GOOGLE", "below_frame")], frame="poster", aimed_at="Google"
    ),
    COPIES["img:4"][0]: MemeRead(text=[MemeText("USA 2020", "on_picture")], aimed_at="the USA"),
    COPIES["img:5"][0]: MemeRead(),
}


class FakeDiff:
    def __init__(self, changes, kinds):
        self.changes, self.kinds, self.latency_ms = changes, kinds, 5


def job_and_trace(images=COPIES):
    matches = {
        mid: ImageMatch(
            id=mid,
            image_url=f"https://{domain}/{mid}.jpg",
            page_url=f"https://{domain}/page/{mid}",
            domain=domain,
            engine="tineye",
            first_seen=when,
            time_source="crawl_date",
            relation="different",
            width=640,
            height=427,
        )
        for mid, (_data, when, domain) in images.items()
    }
    done = Future()
    done.set_result(None)
    trace = SimpleNamespace(
        query_url="https://example.org/upload.jpg",
        future=done,
        images={mid: data for mid, (data, _w, _d) in images.items()},
        matches=matches,
        base=None,
    )
    result = LookupResult(
        id="lk_test",
        input=LookupInput(),
        kind="image",
        started_at=datetime.now(UTC),
        steps=[Step(id="family", tool="nemotron", label="Family tree built")],
    )
    receipts = []

    def receipt(tool, endpoint, summary, **kw):
        receipts.append((tool, endpoint, summary))
        return len(receipts)

    deps = SimpleNamespace(
        nemotron=object(),
        oxylabs=None,
        wikipedia=False,
        wayback=False,
        relate=None,
        describe_changes=None,
        image_transport=None,
    )
    job = SimpleNamespace(
        deps=deps,
        lock=threading.RLock(),
        result=result,
        _final=None,
        _upload=UPLOAD,
        _step=lambda sid: result.steps[0],
        _spend=lambda need_s=3.0: None,
        _provider_deadline=lambda cap=None: time.monotonic() + 30,
        receipt=receipt,
        skip=lambda sid, why: None,
    )
    return job, trace, receipts


def fake_reads(monkeypatch, face_swapped=()):
    def read_meme(client, data, *, timeout_s):
        return READS.get(data, MemeRead())

    def describe(client, a, b, *, timeout_s):
        # b is the aligned photo crop: a red patch in its middle is the "pasted face"
        with Image.open(io.BytesIO(b)) as image:
            w, h = image.size
            r, g, _b = image.convert("RGB").getpixel((w // 2, h // 2))
        if r > 200 and g < 60:
            return FakeDiff(
                ["The girl's face was replaced with a different face."], ["face_changed"]
            )
        return FakeDiff(["The text was added at the top."], ["text_added"])

    monkeypatch.setattr("media.memeread.read_meme", read_meme)
    monkeypatch.setattr("media.imagediff.describe_changes", describe)


def build(monkeypatch, **kw):
    fake_reads(monkeypatch, **kw)
    job, trace, receipts = job_and_trace()
    family.Builder(job, trace).run()
    return job.result, receipts


def test_tree_branches_from_the_source_photo(monkeypatch):
    result, receipts = build(monkeypatch, face_swapped=())
    step = result.steps[0]
    assert step.state == "done", step.detail
    tree = result.family
    nodes = {n.id: n for n in tree.nodes}
    # the unrelated photo is not in the family
    assert "img:6" not in {m.id for m in tree.members}
    source = nodes["source"]
    assert source.members[0] == "img:1" and set(source.members) == {"img:1", "img:5"}
    assert source.date == "2008-07-17"
    posters = [n for n in tree.nodes if n.kind == "variant" and n.branch == "poster"]
    assert [n.label for n in posters] == ["BOREDOM", "GOOGLE"]
    assert all(n.parent == "b:poster" for n in posters)
    assert posters[1].aimed_at == "Google"
    labels = [n for n in tree.nodes if n.kind == "variant" and n.branch == "label"]
    assert [n.label for n in labels] == ["USA 2020"]
    submitted = nodes["submitted"]
    assert submitted.parent == "b:caption" and submitted.new
    assert submitted.aimed_at == "a senator"
    assert tree.lineage == ["source", "b:caption", "submitted"]
    # every branch hangs off the source photo
    assert {n.parent for n in tree.nodes if n.kind == "branch"} == {"source"}
    assert any(t == "nemotron" and e == "meme read" for t, e, _ in receipts)


def test_submission_with_a_known_caption_joins_that_variant(monkeypatch):
    fake_reads(monkeypatch, face_swapped=())
    monkeypatch.setitem(
        READS,
        COPIES["img:4"][0],
        MemeRead(
            text=[
                MemeText("THE SENATOR WATCHING", "top"),
                MemeText("HIS OWN BILL PASS!", "bottom"),
            ]
        ),
    )
    job, trace, _ = job_and_trace()
    family.Builder(job, trace).run()
    nodes = {n.id: n for n in job.result.family.nodes}
    sub = nodes["submitted"]
    assert not sub.new
    parent = nodes[sub.parent]
    assert parent.kind == "variant" and parent.members == ["img:4"]
    assert "2020-09-01" in sub.change


def test_scene_text_on_most_copies_is_not_an_edit(monkeypatch):
    fake_reads(monkeypatch, face_swapped=())
    for mid in ("img:1", "img:4", "img:5"):
        read = READS[COPIES[mid][0]]
        monkeypatch.setitem(
            READS, COPIES[mid][0], MemeRead(text=[*read.text, MemeText("38", "on_picture")])
        )
    job, trace, _ = job_and_trace()
    family.Builder(job, trace).run()
    tree = job.result.family
    assert not any(n.label == "38" for n in tree.nodes)
    assert set(next(n for n in tree.nodes if n.id == "source").members) == {"img:1", "img:5"}
    assert any("'38'" in note for note in tree.notes)


def test_face_swap_is_its_own_branch(monkeypatch):
    swapped = BASE.copy()
    ImageDraw.Draw(swapped).rectangle((270, 170, 370, 260), fill=(255, 0, 0))
    data = jpeg(swapped)
    images = {**COPIES, "img:7": (data, datetime(2009, 7, 3, tzinfo=UTC), "fark.com")}
    fake_reads(monkeypatch)
    monkeypatch.setitem(READS, data, MemeRead())
    job, trace, _ = job_and_trace(images)
    family.Builder(job, trace).run()
    tree = job.result.family
    swaps = [n for n in tree.nodes if n.kind == "variant" and n.branch == "face_swap"]
    assert len(swaps) == 1 and swaps[0].members == ["img:7"]
    assert "face was replaced" in swaps[0].label
    assert next(m for m in tree.members if m.id == "img:7").kind == "face_swap"
    # the submission's own change list comes from the source-photo comparison
    assert next(n for n in tree.nodes if n.id == "submitted").change == (
        "The text was added at the top."
    )


def test_label_like_image_text_reaches_the_submission(monkeypatch):
    # imagediff-v2 draws no "A: earlier" / "B: later" panel labels, so such text is real.
    fake_reads(monkeypatch)
    sentence = "The text 'B: later' was added at the top."

    def describe(client, a, b, *, timeout_s):
        return FakeDiff([sentence], ["text_added"])

    monkeypatch.setattr("media.imagediff.describe_changes", describe)
    job, trace, _ = job_and_trace()
    family.Builder(job, trace).run()
    assert next(n for n in job.result.family.nodes if n.id == "submitted").change == sentence


def test_no_nemotron_skips(monkeypatch):
    job, trace, _ = job_and_trace()
    job.deps.nemotron = None
    family.Builder(job, trace).run()
    assert job.result.steps[0].state == "skipped" and job.result.family is None


def test_start_without_a_picture_skips():
    job, trace, _ = job_and_trace()
    trace.query_url = ""
    skipped = []
    job.skip = lambda sid, why: skipped.append((sid, why))
    future = family.start(job, trace, None)
    assert future.done() and skipped and skipped[0][0] == "family"


class FakeKym:
    """Oxylabs as Know Your Meme answers it: /memes/storm-area-51 redirects to the entry's own
    kind path, /memes/events/storm-area-51 (the real page, saved by tests/test_memeorigin)."""

    PAGE = "https://knowyourmeme.com/memes/events/storm-area-51"

    def __init__(self, hits=()):
        self.hits, self.scraped = list(hits), []

    def search(self, query, max_results=10, *, deadline=None):
        return [SimpleNamespace(url=u) for u in self.hits]

    def scrape_markdown(self, url, *, deadline=None):
        from pathlib import Path

        self.scraped.append(url)
        md = Path(__file__).parent / "fixtures" / "memeorigin" / "kym_event_storm_area_51.md"
        return md.read_text(), {"url": self.PAGE, "statuses": {"http_request": {"code": 200}}}


def kym_builder(monkeypatch, oxylabs):
    job, trace, receipts = job_and_trace()
    job.deps.oxylabs = oxylabs
    builder = family.Builder(job, trace)
    monkeypatch.setattr(builder, "_get", lambda url, **kw: None)  # no entry picture to check
    from media import align

    return builder, align, receipts


def test_event_entry_found_by_the_image_search(monkeypatch):
    """A photo filed under an event entry: the entry is read and cited at its own path."""
    kym = FakeKym()
    builder, align, receipts = kym_builder(monkeypatch, kym)
    builder._find_sources(
        "", ["https://knowyourmeme.com/photos/1907928-storm-area-51"], align, UPLOAD
    )
    assert kym.scraped == ["https://knowyourmeme.com/memes/storm-area-51"]
    [(entry, source)] = builder.sources
    assert source.source == "Know Your Meme" and source.name == "Storm Area 51"
    assert source.url == entry.url == FakeKym.PAGE
    assert entry.milestones[0].url == FakeKym.PAGE + "#background"
    assert any(s.startswith("Know Your Meme entry read: Storm Area 51") for _t, _e, s in receipts)


def test_event_entry_from_text_search_needs_the_same_picture(monkeypatch):
    """An entry only the text search found is used when its picture is this photograph."""
    kym = FakeKym(
        [
            "https://knowyourmeme.com/memes/storm-area-51/photos",
            "https://knowyourmeme.com/memes/events/storm-area-51",
        ]
    )
    builder, align, _ = kym_builder(monkeypatch, kym)
    builder._find_sources("Storm Area 51", [], align, UPLOAD)
    assert kym.scraped == [FakeKym.PAGE]  # the gallery is skipped, the entry read
    assert builder.sources == []
    assert any("'Storm Area 51'" in n and "not this photograph" in n for n in builder.notes)
