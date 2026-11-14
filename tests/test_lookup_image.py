"""Image lookups (POST /api/lookup/image): an uploaded meme or screenshot is traced like a post
whose only content is its image (no network; fictional accounts)."""

import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes import lookup as lookup_route
from lookup.jobs import JobStore, cache_key
from lookup.models import ImageRef, LookupInput
from lookup.service import Deps, Lookup
from lookup.upload import sniff_image
from media.nemotron import MediaAnalysis
from sources.models import PageFacts, SearchHit

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
            ),
            SearchHit(  # shares words with every caption here, but never 3 in a row
                url="https://news.example/flights",
                title="Comets, flights and fire",
                snippets=["Why flights stopped: comets land in a fine week for the dog"],
                position=2,
            ),
            SearchHit(
                url="https://memes.example/fine",
                title="This is fine: the dog in the burning room",
                snippets=["The this is fine dog sits in a room on fire"],
                position=3,
            ),
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


TEXT_STEPS = ("copies_web", "copies_x", "copies_bsky")


def test_uploaded_meme_keeps_hits_with_3_caption_words_in_a_row():
    reader = FakeReader()
    job = _run(reader)
    r = job.result
    assert r.state == "done" and r.kind == "image" and r.input.image.sha256 == _ref().sha256
    assert reader.calls == [(JPEG, None)]  # the uploaded bytes, read once, no post text
    st = {s.id: s for s in r.steps}
    assert st["post"].state == st["author"].state == st["linked"].state == "skipped"
    assert st["media"].label == "Image read"
    assert st["media"].state == "done"
    subj = next(p for p in r.posts if p.id == r.subject_id)
    assert subj.role == "submitted" and subj.id.startswith("upload:")
    assert subj.created_at is None and subj.media_urls == ["/api/lookup/lk_img/image"]
    assert subj.text == "\n".join(CAPTION)  # reading order, for the card
    qs = job.deps.oxylabs.queries
    # the caption in reading order, exact; never the description, never the loose coverage query
    assert '"WHEN THE COMETS LAND AND NOBODY TELLS YOU WHY THE FLIGHTS STOPPED"' in qs
    assert not any("dog" in q or q.endswith(" meme") for q in qs)
    assert st["coverage"].state == "skipped"
    posts = {p.id: p for p in r.posts}
    assert posts[f"x:{COPY}"].caption_match == [" ".join(CAPTION)]
    assert posts[f"x:{LATER}"].caption_match == ["when the comets land", "nobody tells you"]
    # pages too, only with 3+ words in a row: the one sharing loose words is dropped
    pages = {pg.url: pg for pg in r.pages}
    assert set(pages) == {"https://memes.example/comets"}
    assert pages["https://memes.example/comets"].caption_match == ["When the comets land"]
    assert all(e.from_ in posts or e.from_ in {pg.id for pg in r.pages} for e in r.edges)
    # the earliest copy read on its own page is the origin, never the upload itself
    assert r.origin_id == f"x:{COPY}" and posts[f"x:{COPY}"].role == "origin"
    assert any("3 words in a row" in n for n in r.notes)


def test_short_caption_is_searched_and_only_its_words_in_a_row_match():
    job = _run(FakeReader(text=["THIS IS FINE"]))
    r = job.result
    assert job.deps.oxylabs.queries == ['"THIS IS FINE"', 'site:x.com "THIS IS FINE"']
    # the comets posts and pages don't carry "this is fine": only the dog page stays
    assert [p.id for p in r.posts] == [r.subject_id]
    assert [(pg.url, pg.caption_match) for pg in r.pages] == [
        ("https://memes.example/fine", ["This is fine", "this is fine"])
    ]
    assert r.origin_id is None


def test_under_3_words_or_only_a_description_is_never_searched():
    for reader in (
        FakeReader(text=["FINE DOG"]),
        FakeReader(text=[], description="A cartoon dog sits in a burning room."),
    ):
        job = _run(reader)
        steps = {s.id: s.state for s in job.result.steps}
        assert job.deps.oxylabs.queries == []
        assert all(steps[sid] == "skipped" for sid in (*TEXT_STEPS, "coverage"))
        assert steps["media"] == "done"  # read, not searched
        assert [p.id for p in job.result.posts] == [job.result.subject_id]
        assert job.result.pages == [] and job.result.origin_id is None
        assert any("matches of the uploaded picture only" in n for n in job.result.notes)


def test_caption_runs():
    from lookup.textsim import caption_runs

    cap = "When the comets land\nAND NOBODY tells you why"
    # case, punctuation and line breaks don't matter; the text's own spelling is returned
    assert caption_runs("“When the comets land,” nobody knew", cap) == ["When the comets land"]
    assert caption_runs("and nobody tells you", cap) == ["and nobody tells you"]
    assert caption_runs("comets land and nobody", cap) == ["comets land and nobody"]
    assert caption_runs("the comets go home", cap) == []  # two words in a row
    assert caption_runs("in the and", "a dog in the and more") == []  # only stop words
    assert caption_runs("anything", "two words") == [] and caption_runs(None, cap) == []


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


def test_unreadable_upload_says_so():
    class Broken(FakeReader):
        def analyze(self, media, *, context=None):
            raise RuntimeError("endpoint down")

    job = _run(Broken())
    r = job.result
    assert r.state == "done" and {s.id: s.state for s in r.steps}["media"] == "failed"
    assert any("could not read the uploaded image" in n for n in r.notes)
    assert job.deps.oxylabs.queries == []
    # it says the text couldn't be read, not that the image has too little of it
    assert {s.id: s.detail for s in r.steps}["copies_x"] == "the image's text could not be read"
    assert any("its text could not be read" in n for n in r.notes)
    assert not any("fewer than" in n for n in r.notes)


def test_rate_limited_client_is_refused_before_the_body_is_read(monkeypatch):
    store = JobStore(lambda: Deps(oxylabs=FakeOxylabs(), nemotron=FakeReader()),
                     max_per_hour=10, max_per_client_per_hour=1)  # fmt: skip
    app.dependency_overrides[lookup_route.get_jobs] = lambda: store
    monkeypatch.setattr(lookup_route, "_media_configured", lambda: True)
    try:
        c = TestClient(app)
        first = c.post("/api/lookup/image", content=JPEG)
        assert first.status_code in (200, 202)
        read = []

        def body():
            read.append(1)
            yield JPEG + b"other"

        assert c.post("/api/lookup/image", content=body()).status_code == 429
        assert read == []  # refused before a byte of the upload was read
        # the JSON route has no image field: an image there is not silently accepted
        assert c.post("/api/lookup", json={"image": {"sha256": "x"}}).status_code == 400
        # let the first lookup finish: a run still queueing reads at exit logs a traceback
        for _ in range(200):
            if c.get(f"/api/lookup/{first.json()['id']}").json()["state"] != "running":
                break
            time.sleep(0.05)
    finally:
        app.dependency_overrides.pop(lookup_route.get_jobs, None)


def test_upload_quote_whose_quoted_post_carries_the_caption_keeps_both():
    """Entire review: a post that quotes the caption is kept with the quoted post, even when
    the quoted text shares too few words to count as a copy; a quote of something else stands
    on its own text."""
    from lookup.models import Post
    from tests.test_lookup import COPY as QCOPY
    from tests.test_lookup import QUOTE_MD, SUBJ

    class Oxy(FakeOxylabs):
        def scrape_markdown(self, url, deadline=None):
            return QUOTE_MD, {}

    def verify(caption):
        deps = Deps(oxylabs=Oxy(), nemotron=FakeReader())
        job = Lookup("lk_q", LookupInput(image=_ref()), deps, image=JPEG)
        job._add_upload_post()
        job._caption = caption
        cand = Post(id=f"x:{QCOPY}", platform="x", url=f"https://x.com/b/status/{QCOPY}")
        return job, job._verify_x(job.add_post(cand), caption)

    job, post = verify("the REAL reason for the ground stop")
    assert post.role == "quote" and post.caption_match == ["the REAL reason for the ground stop"]
    quoted = next(p for p in job.result.posts if p.id == f"x:{SUBJ}")
    assert quoted.caption_match == ["the REAL reason for the ground stop"]
    assert any(e.from_ == quoted.id and e.to == post.id for e in job.result.edges)

    job, post = verify("nobody panics at the airport today")  # no 3 words in a row of either
    assert post is None and [p.id for p in job.result.posts] == [job.result.subject_id]


# ---------------------------------------------------------------- a site address in the image

SITE_TEXT = [
    "petition.example-site.org",
    "Stand with Example",
    "Sign the letter",
    "SEARCH PUBLIC SIGNERS BY NAME",
    "Dario",
    "1–2 of 2 matching public signers · Page 1 of 1",
]
PROMO = "2121377395244749097"
OFFTOPIC = "2121377395244749098"


class SiteOxylabs(FakeOxylabs):
    """The page's menus never match as a phrase; its address finds the campaign and its posts."""

    def scrape_markdown(self, url, deadline=None):
        if url.endswith(PROMO):
            return _md("promoexample", PROMO, "Sign it: petition.example-site.org #examplesite"), {}
        if url.endswith(OFFTOPIC):
            return _md("offexample", OFFTOPIC, "cats are great"), {}
        raise RuntimeError("not found")

    def search(self, query, max_results=10, deadline=None):
        self.queries.append(query)
        if "example-site" not in query:
            return []
        if query.startswith("site:x.com"):
            return [
                SearchHit(
                    url=f"https://x.com/promoexample/status/{PROMO}",
                    title='promo on X: "Sign it: petition.example-site.org"',
                    snippets=["Sign it: petition.example-site.org"],
                    position=1,
                ),
                SearchHit(
                    url=f"https://x.com/offexample/status/{OFFTOPIC}",
                    title='off on X: "cats are great"',
                    snippets=["cats are great"],
                    position=2,
                ),
            ]
        return [
            SearchHit(
                url="https://petition.example-site.org/",
                title="An open letter",
                snippets=["petition.example-site.org · An independent campaign"],
                position=1,
            ),
            SearchHit(
                url="https://forum.example/thread",
                title="Is the example-site petition real?",
                snippets=["People keep adding fake names; example-site.net is a lookalike"],
                position=2,
            ),
            SearchHit(
                url="https://other.example/letters",
                title="Open letters to company boards",
                snippets=["Sign the open letter to the board before Friday"],
                position=3,
            ),
        ]


class SiteBsky:
    last_call: dict = {}

    def __init__(self):
        self.queries = []

    def search(self, q, limit=25, deadline=None):
        self.queries.append(q)
        if "example-site" not in q:
            return []

        def pv(rkey, text, at):
            return {
                "uri": f"at://did:plc:aaaaaaaaaaaaaaaaaaaaaaaa/app.bsky.feed.post/{rkey}",
                "author": {"handle": f"{rkey}.bsky.social"},
                "record": {"text": text, "createdAt": at},
            }

        return [
            # every word of it is in the image's text, but it only names the site
            pv("short", "#examplesite petition.example-site.org", "2026-11-13T20:00:00Z"),
            # it repeats the page itself: a copy of the screenshot's text
            pv(
                "copy",
                "petition.example-site.org Stand with Example Sign the letter: SEARCH PUBLIC "
                "SIGNERS BY NAME",
                "2026-11-13T21:00:00Z",
            ),
        ]


def test_site_address_in_image_is_searched_and_matches():
    from lookup.textsim import domains

    assert domains("\n".join(SITE_TEXT)) == ["petition.example-site.org"]
    ox, bsky = SiteOxylabs(), SiteBsky()
    deps = Deps(oxylabs=ox, bsky=bsky, nemotron=FakeReader(text=SITE_TEXT, media_type="screenshot"))
    job = Lookup("lk_site", LookupInput(image=_ref()), deps, image=JPEG)
    job.run()
    r = job.result
    # the phrase search ran, and the address searches: the web by the address, X by its
    # distinctive name part (8+ letters), which also finds #examplesite-style mentions
    assert '"petition.example-site.org"' in ox.queries
    assert 'site:x.com "example-site"' in ox.queries
    assert any(q.startswith('"petition.example-site.org Stand with') for q in ox.queries)
    pages = {pg.url: pg for pg in r.pages}
    assert "https://petition.example-site.org/" in pages
    assert pages["https://petition.example-site.org/"].caption_match == [
        "petition.example-site.org"
    ]
    # the site's distinctive name under another ending (a lookalike) counts too
    assert pages["https://forum.example/thread"].caption_match == ["example-site"]
    assert "https://other.example/letters" not in pages  # names neither the site nor 3 words
    promo = next(p for p in r.posts if p.id == f"x:{PROMO}")
    assert promo.verified and promo.caption_match == ["petition.example-site.org"]
    assert not any(p.id == f"x:{OFFTOPIC}" for p in r.posts)
    # a post that only names the site is about the screenshot, not a copy of it: never its
    # origin; the Bluesky post that repeats the page's text is
    assert '"example-site"' in bsky.queries
    short = next(p for p in r.posts if p.id.endswith("/short"))
    assert short.caption_match == ["petition.example-site.org"]
    assert r.origin_id.endswith("/copy") and promo.role != "origin"
    assert any("name the site written in it (petition.example-site.org)" in n for n in r.notes)
    assert "or name the site" in job._step("copies_web").detail


def test_platform_addresses_and_file_names_are_not_searched():
    from lookup.textsim import domain_hits, domains

    assert domains("x.com/user posted photo.jpg on bsky.app, e.g. at t.co/abc") == []
    assert domains("www.StandWithJensen.com and news.bbc.co.uk and nvidia.com") == [
        "standwithjensen.com",
        "nvidia.com",
    ]
    hits = domain_hits(
        "Visit StandWithJensen.com, #standwithjensen, standwithjensen.online; not "
        "standwithjensenfan.com, and not a plain standwithjensen",
        ["standwithjensen.com"],
    )
    assert hits == ["StandWithJensen.com", "#standwithjensen", "standwithjensen"]
    # a dictionary word as the site's name is never matched as a plain word
    assert domain_hits("The information is incorrect", ["information.com"]) == []
    # a file name is not another ending of the site
    assert domain_hits("remove analytics.js and analytics.pdf", ["analytics.com"]) == []
    for name in (
        "analytics.min.js",
        "analytics.bundle.js",
        "end of analytics. Then",
        "analytics.track_event()",
        "analytics.online_backup",
        "analytics.onlineé",
    ):
        assert domain_hits(name, ["analytics.com"]) == [], name
    assert domain_hits("analytics.co.uk too", ["analytics.com"]) == ["analytics"]
    assert domain_hits("analytics.online is a lookalike", ["analytics.com"]) == ["analytics"]


def _graph_job():
    from datetime import UTC, datetime

    from lookup.models import Page, Post

    job = Lookup("lk_graph", LookupInput(image=_ref()), Deps(oxylabs=FakeOxylabs()), image=JPEG)
    job._add_upload_post()
    job._caption = "\n".join(SITE_TEXT)
    job._domains = ["petition.example-site.org"]
    copy_text = "petition.example-site.org Stand with Example Sign the letter SEARCH PUBLIC SIGNERS"
    job.add_post(
        Post(
            id="at://did:plc:aaaaaaaaaaaaaaaaaaaaaaaa/app.bsky.feed.post/copy",
            platform="bluesky",
            text=copy_text,
            created_at=datetime(2026, 11, 13, 21, tzinfo=UTC),
            role="bluesky",
            similarity=0.9,
            verified=True,
        )
    )

    def page(url, headline, snippet, read=None):
        """`read`: what the page's own read returned (None: never read)."""
        pg = job.add_page(
            Page(
                id="",
                url=url,
                role="reference",
                headline=headline,
                snippet=snippet,
                time=datetime(2026, 11, 12, tzinfo=UTC),
                time_source="page",
                time_uncertainty_s=0,
            )
        )
        if read is not None:
            job._page_read[pg.id] = tuple(read.split("\n"))
        return pg

    return job, page


def test_page_that_only_names_the_site_is_not_the_uploads_origin():
    job, page = _graph_job()
    page(
        "https://petition.example-site.org/",
        "petition.example-site.org",
        "An open letter",
        read="petition.example-site.org\nAn open letter to the board",
    )
    job._finish_graph()
    assert job.result.origin_id.endswith("/copy")  # the post that repeats the page's text


def test_page_that_repeats_the_images_text_can_be_its_origin():
    job, page = _graph_job()
    pg = page(
        "https://archive.example/snap",
        "Stand with Example: Sign the letter",
        "SEARCH PUBLIC SIGNERS BY NAME · 1–2 of 2 matching public signers",
        read="Stand with Example: Sign the letter\nSEARCH PUBLIC SIGNERS BY NAME",
    )
    job._finish_graph()
    assert job.result.origin_id == pg.id


def test_a_search_snippet_alone_never_makes_a_page_the_uploads_origin():
    job, page = _graph_job()
    snippet = "Stand with Example: Sign the letter SEARCH PUBLIC SIGNERS BY NAME"
    page("https://stale.example/a", snippet, snippet, read="petition.example-site.org\nAbout us")
    page("https://unread.example/b", snippet, snippet)  # dated, but its read never came back
    job._finish_graph()
    assert job.result.origin_id.endswith("/copy")


@pytest.mark.parametrize(
    "headline,claims,expected_match",
    [
        ("Unrelated title", ["Unrelated page text"], []),
        (None, ["Unrelated page text"], []),  # the old search title remains on the Page
        (None, [], []),  # an empty successful read still supersedes the search hit
        ("An open letter", ["SEARCH PUBLIC SIGNERS BY NAME"], ["SEARCH PUBLIC SIGNERS BY NAME"]),
        ("Stand with Example", [], ["Stand with Example"]),
        ("Stand", ["with Example"], []),  # distinct fields do not make a caption run
        ("Unrelated", ["Stand", "with Example"], []),
        (None, ["SEARCH PUBLIC\nSIGNERS BY NAME"], ["SEARCH PUBLIC\nSIGNERS BY NAME"]),
    ],
)
def test_upload_page_filter_uses_its_own_read_not_the_search_hit(headline, claims, expected_match):
    job, page = _graph_job()
    pg = page("https://source.example/a", "Stand with Example", "Sign the letter")
    job.add_edge(job.result.subject_id, pg.id, "cites")
    job._step("copies_web").found = [pg.id]
    job.deps.oxylabs.scrape_facts = lambda url, deadline=None: PageFacts(
        url=url, final_url=url, headline=headline, claims=claims
    )

    job._safe_verify_page(pg)
    job._finish_graph()

    assert (pg in job.result.pages) == bool(expected_match)
    if expected_match:
        assert pg.caption_match == expected_match
        assert job._step("copies_web").found == [pg.id]
    else:
        assert job._step("copies_web").found == []
        assert all(pg.id not in (e.from_, e.to) for e in job.result.edges)


def test_image_origin_cannot_join_caption_words_across_separate_read_fields():
    job, page = _graph_job()
    page(
        "https://fragments.example/a",
        "Stand with",
        "Example Sign the letter",
        read="Stand with\nExample Sign the letter",
    )
    job._finish_graph()
    assert job.result.origin_id.endswith("/copy")


def test_upload_page_filter_keeps_search_matches_when_the_read_fails():
    job, page = _graph_job()
    pg = page("https://unread.example/a", "Stand with Example", "Sign the letter")
    # FakeOxylabs.scrape_facts raises: no read evidence exists to replace the search hit.
    job._safe_verify_page(pg)
    job._finish_graph()

    assert pg in job.result.pages
    assert pg.caption_match == ["Stand with Example", "Sign the letter"]
