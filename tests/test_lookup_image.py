"""Image lookups (POST /api/lookup/image): an uploaded meme or screenshot is traced like a post
whose only content is its image (no network; fictional accounts)."""

import hashlib

from fastapi.testclient import TestClient

from api.main import app
from api.routes import lookup as lookup_route
from lookup.jobs import JobStore, cache_key
from lookup.models import ImageRef, LookupInput
from lookup.service import Deps, Lookup
from lookup.upload import sniff_image
from media.nemotron import MediaAnalysis
from sources.models import SearchHit

SUPER_VL = "nemotron-3.5-super-vl"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
COPY = "2121370648207671396"  # 22:32:30 UTC
LATER = "2121385772448096586"
CAPTION = ["WHEN THE COMETS LAND", "AND NOBODY TELLS YOU WHY THE FLIGHTS STOPPED"]


def _ref(data: bytes = JPEG) -> ImageRef:
    return ImageRef(sha256=hashlib.sha256(data).hexdigest(), mime="image/jpeg", bytes=len(data))


class FakeReader:
    def __init__(self, text=CAPTION, media_type="meme", description="A cartoon dog in a fire."):
        self.text, self.media_type, self.description = text, media_type, description
        self.calls = []

    def analyze(self, media, *, context=None):
        self.calls.append((media.data, context))
        return MediaAnalysis(
            media_hash=media.media_hash, description=self.description,
            extracted_text=list(self.text), reuse_cues=["impact font"], manipulation_cues=[],
            confidence=0.8, model=SUPER_VL, latency_ms=900, media_type=self.media_type,
        )  # fmt: skip


def _md(handle: str, sid: str, text: str) -> str:
    """A logged-out X post page as Oxylabs returns it: one bullet item per post."""
    body = f"""[![@{handle}](https://pbs.twimg.com/profile_images/1/a_normal.jpg)](/{handle})

[{handle}](/{handle})[@{handle}](/{handle})

{text}

[10:32 PM · Nov 13, 2026](/{handle}/status/{sid})·[1,928

Views](/{handle}/status/{sid})

[6](/compose/post?in_reply_to={sid})

2

26"""
    return "## Conversation\n\n* " + body.replace("\n", "\n  ") + "\n"


class FakeOxylabs:
    def __init__(self):
        self.queries = []
        self.last_call = {"request_id": "r1", "latency_ms": 5}

    def scrape_markdown(self, url, deadline=None):
        joined = " ".join(CAPTION)
        if url.endswith(COPY):
            return _md("cyexample", COPY, joined), {}
        if url.endswith(LATER):
            return _md("deeexample", LATER, "when the comets land, nobody tells you"), {}
        raise RuntimeError("not found")

    def scrape_facts(self, url, deadline=None):
        raise RuntimeError("not used")

    def search(self, query, max_results=10, deadline=None):
        self.queries.append(query)
        if query.startswith("site:x.com"):
            return [
                SearchHit(
                    url=f"https://x.com/cyexample/status/{COPY}",
                    title="Cy on X",
                    snippets=[" ".join(CAPTION)],
                    position=1,
                ),
                SearchHit(
                    url=f"https://x.com/deeexample/status/{LATER}",
                    title="Dee on X",
                    snippets=["when the comets land, nobody tells you why the flights stopped"],
                    position=2,
                ),
            ]
        return [
            SearchHit(
                url="https://memes.example/comets",
                title="When the comets land: meme",
                snippets=["When the comets land is a meme template"],
                position=1,
            )
        ]


def _run(reader=None, data=JPEG, **kw) -> Lookup:
    deps = Deps(oxylabs=FakeOxylabs(), nemotron=reader or FakeReader(), **kw)
    job = Lookup("lk_img", LookupInput(image=_ref(data)), deps, image=data)
    job.run()
    return job


def test_sniff_image_by_magic_bytes():
    assert sniff_image(JPEG) == "image/jpeg"
    assert sniff_image(b"\x89PNG\r\n\x1a\n" + b"\0" * 8) == "image/png"
    assert sniff_image(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert sniff_image(b"GIF89a....") == "image/gif"
    assert sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'/>") is None
    assert sniff_image(b"") is None


def test_uploaded_meme_text_drives_the_searches_and_finds_copies():
    reader = FakeReader()
    job = _run(reader)
    r = job.result
    assert r.state == "done" and r.kind == "image"
    assert reader.calls == [(JPEG, None)]  # the uploaded bytes, read once, no post text
    st = {s.id: s for s in r.steps}
    assert st["post"].state == st["author"].state == st["linked"].state == "skipped"
    assert st["media"].label.startswith("Read the uploaded image")
    subj = next(p for p in r.posts if p.id == r.subject_id)
    assert subj.role == "submitted" and subj.id.startswith("upload:")
    assert subj.created_at is None and subj.media_urls == ["/api/lookup/lk_img/image"]
    assert subj.text == "\n".join(CAPTION)  # reading order, for the card
    assert any("WHEN THE COMETS LAND" in q for q in job.deps.oxylabs.queries)
    assert any(q.endswith(" meme") for q in job.deps.oxylabs.queries)  # coverage: meme pages
    posts = {p.id: p for p in r.posts}
    # the earliest copy read on its own page is the origin, never the upload itself
    assert r.origin_id == f"x:{COPY}" and posts[f"x:{COPY}"].role == "origin"
    assert any(
        e.from_ == f"x:{COPY}" and e.to == r.subject_id and e.rel == "media" for e in r.edges
    )
    assert r.media[0].post_id == r.subject_id
    assert any("possible variations" in n for n in r.notes)


def test_upload_with_no_copies_has_no_origin():
    job = _run(FakeReader(text=["unique words nobody else has posted anywhere"]))
    # the fake X search still returns its two posts, but they share no words with this text
    assert job.result.origin_id is None


def test_short_caption_searches_for_pages_about_the_meme():
    job = _run(FakeReader(text=["THIS IS FINE"]))
    r = job.result
    qs = job.deps.oxylabs.queries
    assert qs == ["THIS IS FINE meme"]  # no exact-phrase copy search on three words
    st = {s.id: s for s in r.steps}
    assert st["copies_x"].state == "skipped" and st["coverage"].state == "done"
    assert all(pg.role == "coverage" for pg in r.pages)  # about the image, never a copy
    assert r.origin_id is None


def test_no_text_searches_by_description_but_never_for_adult_content():
    job = _run(FakeReader(text=[], description="A cartoon dog sits in a burning room."))
    assert job.deps.oxylabs.queries == ["meme A cartoon dog sits in a burning room."]

    class Adult(FakeReader):
        def analyze(self, media, *, context=None):
            a = super().analyze(media, context=context)
            return MediaAnalysis(**{**a.__dict__, "content_flags": ["adult"]})

    job = _run(Adult(text=[], description="Adult content (not described)."))
    assert job.deps.oxylabs.queries == []


def test_image_cache_key_and_bytes_required():
    assert cache_key(LookupInput(image=_ref())) == f"image:{_ref().sha256}"
    try:
        Lookup("lk_x", LookupInput(image=_ref()), Deps(oxylabs=FakeOxylabs()))
    except ValueError:
        pass
    else:
        raise AssertionError("an image lookup without bytes must not start")


def test_api_upload_validation_serving_and_cache(monkeypatch):
    def deps():
        return Deps(oxylabs=FakeOxylabs(), nemotron=FakeReader())

    store = JobStore(deps, max_per_hour=10, max_per_client_per_hour=10)
    app.dependency_overrides[lookup_route.get_jobs] = lambda: store
    monkeypatch.setattr(lookup_route, "_media_configured", lambda: True)
    try:
        c = TestClient(app)
        bad = c.post("/api/lookup/image", content=b"<svg/>", headers={"content-type": "image/png"})
        assert bad.status_code == 415
        big = b"\xff\xd8\xff" + b"\0" * (8 * 1024 * 1024)
        assert c.post("/api/lookup/image", content=big).status_code == 413
        res = c.post("/api/lookup/image", content=JPEG, headers={"content-type": "image/jpeg"})
        assert res.status_code in (200, 202)
        body = res.json()
        assert body["kind"] == "image" and body["input"]["image"]["bytes"] == len(JPEG)
        assert "data" not in body["input"]["image"]  # the bytes are never in the JSON
        img = c.get(f"/api/lookup/{body['id']}/image")
        assert img.status_code == 200 and img.content == JPEG
        assert img.headers["content-type"] == "image/jpeg"
        assert img.headers["x-content-type-options"] == "nosniff"
        again = c.post("/api/lookup/image", content=JPEG)
        assert again.json()["id"] == body["id"]  # same bytes within the TTL: the same job
        assert c.get("/api/lookup/lk_nope/image").status_code == 404
        monkeypatch.setattr(lookup_route, "_media_configured", lambda: False)
        assert c.post("/api/lookup/image", content=JPEG).status_code == 503
    finally:
        app.dependency_overrides.pop(lookup_route.get_jobs, None)


def test_text_lookup_has_no_image_route():
    store = JobStore(lambda: Deps(oxylabs=FakeOxylabs()), max_per_hour=10)
    job = store.start(LookupInput(text="the comets have landed and nobody says why"), "c")
    assert job.upload is None
