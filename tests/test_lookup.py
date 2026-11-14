"""lookup/: URL classification, X page parsing, similarity, the job, the API (no network).

Fixtures are fictional accounts in the shape Oxylabs returns for logged-out X pages.
"""

import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes import lookup as lookup_route
from api.schemas import ClusterSummary
from lookup import textsim
from lookup.assess import MAX_CALL_S, Assessor, _Draft, guard
from lookup.jobs import JobStore, RateLimited, cache_key
from lookup.models import LookupInput
from lookup.service import Deps, Lookup, StepUnavailable, _x_title, snippet_time
from lookup.urls import classify, snowflake_time
from lookup.xpage import (
    page_unavailable_reason,
    parse_count,
    parse_post_page,
    parse_profile,
    unavailable_reason,
)
from sources.models import PageFacts, SearchHit
from sources.oxylabs import OxylabsError

# 2121360054330986967 -> 2026-11-13 21:50:24.573 UTC (decoded from the id alone)
SUBJ = "2121360054330986967"
COPY = "2121370648207671396"  # 22:32:30 UTC
QUOTE = "2121364193509277805"  # 22:06:51 UTC
LATER = "2121385772448096586"
TEXT = (
    "The comets have landed. That's the REAL reason for the ground stop. They don't want "
    "to start a worldwide panic."
)

POST_MD = f"""## Post

# Ada Example on X: "{TEXT}"

## Conversation

[![@ada_example](https://pbs.twimg.com/profile_images/1/a_normal.jpg)](/ada_example)

[Ada Example](/ada_example)[@ada\\_example](/ada_example)

{TEXT}

![](https://pbs.twimg.com/media/HXAAAA?format=webp&name=large)

[9:50 PM · Nov 13, 2026](/ada_example/status/{SUBJ})·[2.9M

Views](/ada_example/status/{SUBJ})

[1.8K](/compose/post?in_reply_to={SUBJ})

5.8K

35K

2.8K

## Relevant people

* [![@ada_example](https://pbs.twimg.com/profile_images/1/a_normal.jpg)](/ada_example)

  [Ada Example](/ada_example)[@ada\\_example](/ada_example)

  {TEXT}

  ![](https://pbs.twimg.com/media/HXAAAA?format=webp&name=large)

  [9:50 PM · Nov 13, 2026](/ada_example/status/{SUBJ})·[2.9M

  Views](/ada_example/status/{SUBJ})

  [1.8K](/compose/post?in_reply_to={SUBJ})

  5.8K

  35K

  2.8K
* [![@bob_example](https://pbs.twimg.com/profile_images/2/b_normal.jpg)](/bob_example)

  [Bob](/bob_example)[@bob\\_example](/bob_example)

  [1h](/bob_example/status/{QUOTE})

  Ha, sure.

  [31](/compose/post?in_reply_to={QUOTE})

  87

  2.5K

  242K
"""

QUOTE_MD = f"""## Conversation

* [![@cy_example](https://pbs.twimg.com/profile_images/3/c_normal.jpg)](/cy_example)

  [Cy](/cy_example)[@cy\\_example](/cy_example)

  🤷

  [![@ada_example](https://pbs.twimg.com/profile_images/1/a_normal.jpg)](/ada_example)

  [Ada Example](/ada_example)[@ada\\_example](/ada_example)

  [1h](/ada_example/status/{SUBJ})

  {TEXT}

  [10:32 PM · Nov 13, 2026](/cy_example/status/{COPY})·[1,928

  Views](/cy_example/status/{COPY})

  [6](/compose/post?in_reply_to={COPY})

  2

  26
"""

PROFILE_MD = f"""Ada Example

64.8K posts

# Ada Example

@ada\\_example

Writer of fictional test fixtures.

Helsinki

[Joined June 2009](/ada_example/about)

[1,284 Following](/ada_example/following)[252.6K Followers](/ada_example/verified_followers)

* [![@ada_example](https://pbs.twimg.com/profile_images/1/a_normal.jpg)](/ada_example)

  [Ada Example](/ada_example)[@ada\\_example](/ada_example)

  [2m](/ada_example/status/{LATER})

  later post

  9

  5.3K
* [![@ada_example](https://pbs.twimg.com/profile_images/1/a_normal.jpg)](/ada_example)

  [Ada Example](/ada_example)[@ada\\_example](/ada_example)

  [1h](/ada_example/status/{SUBJ})

  {TEXT[:40]} Show more

  34

  23K
"""


def test_snowflake_and_classify():
    assert snowflake_time(SUBJ) == datetime(2026, 11, 13, 21, 50, 24, 573000, tzinfo=UTC)
    assert snowflake_time("12345") is None  # pre-snowflake ids carry no time
    t = classify(f"https://twitter.com/ada_example/status/{SUBJ}?s=20")
    assert (t.kind, t.handle, t.post_id, t.url) == (
        "x_post",
        "ada_example",
        SUBJ,
        f"https://x.com/ada_example/status/{SUBJ}",
    )
    assert classify("https://x.com/ada_example").kind == "x_profile"
    assert classify("https://x.com/explore").kind == "web"
    assert classify("https://bsky.app/profile/a.bsky.social/post/3abc").kind == "bsky_post"
    assert classify("https://example.org/story?id=1#top").url == "https://example.org/story?id=1"
    assert classify("not a url") is None


def test_parse_post_page_main_and_replies():
    page = parse_post_page(POST_MD, SUBJ)
    m = page.main
    assert (m.handle, m.name, m.text) == ("ada_example", "Ada Example", TEXT)
    assert (m.views, m.replies, m.reposts, m.likes, m.bookmarks) == (
        2_900_000,
        1_800,
        5_800,
        35_000,
        2_800,
    )
    assert m.media_urls == ["https://pbs.twimg.com/media/HXAAAA?format=webp&name=large"]
    assert [r.handle for r in page.replies] == ["bob_example"]
    r = page.replies[0]
    assert (r.replies, r.reposts, r.likes, r.views) == (31, 87, 2_500, 242_000)


def test_parse_quote_post():
    m = parse_post_page(QUOTE_MD, COPY).main
    assert m.text == "🤷" and m.quoted_handle == "ada_example" and m.quoted_id == SUBJ
    assert m.quoted_text == TEXT
    # X hides zero counts: 2 of 3 unlabelled numbers shown -> we can't tell which is hidden,
    # so none is guessed; the labelled ones (views, replies) stay
    assert (m.views, m.replies, m.reposts, m.likes, m.bookmarks) == (1928, 6, None, None, None)
    assert m.counts_shown == 2


def test_parse_profile():
    p = parse_profile(PROFILE_MD, "ada_example")
    assert (p.joined, p.followers, p.following, p.posts_count) == (
        "2009-06",
        252_600,
        1_284,
        64_800,
    )
    assert p.bio == "Writer of fictional test fixtures." and p.location == "Helsinki"
    assert [it.status_id for it in p.recent] == [LATER, SUBJ]


def test_counts_and_text_helpers():
    assert [parse_count(x) for x in ("22.3K", "2.9M", "1,284", "7", "x")] == [
        22_300,
        2_900_000,
        1_284,
        7,
        None,
    ]
    assert textsim.similarity(TEXT, TEXT) == 1.0
    assert textsim.similarity(f"Ada Example: “{TEXT}”", TEXT) >= 0.9  # attributed copy
    assert textsim.similarity("totally different words here today", TEXT) < 0.2
    assert textsim.phrase("too short") is None
    assert textsim.strip_snippet("23 hours ago · The comets ...") == "The comets"
    at = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)
    t, unc = snippet_time("2 hours ago - x", at)
    assert t == datetime(2026, 11, 13, 21, 0, tzinfo=UTC) and unc == 7200  # 21:00-23:00
    t, unc = snippet_time("Nov 13, 2026 — x", at)
    assert t == datetime(2026, 11, 13, tzinfo=UTC) and unc == 86400  # any time that day
    assert _x_title('Bob on X: "Ha, sure." / X') == ("Bob", "Ha, sure.")
    assert _x_title("Bob on X") == ("Bob", None)


class FakeOxylabs:
    def __init__(self):
        self.calls = []
        self.last_call = {"request_id": "r1", "latency_ms": 5}

    def scrape_markdown(self, url, deadline=None):
        self.calls.append(("scrape", url))
        if url.endswith(SUBJ):
            return POST_MD, {}
        if url.endswith(COPY):
            return QUOTE_MD, {}
        if url.endswith("/ada_example"):
            return PROFILE_MD, {}
        raise RuntimeError("not found")

    def scrape_facts(self, url, deadline=None):
        self.calls.append(("facts", url))
        return PageFacts(url=url, final_url=url, publisher="Example News", headline="Comets")

    def search(self, query, max_results=10, deadline=None):
        self.calls.append(("search", query))
        if query.startswith("site:x.com"):
            return [
                SearchHit(
                    url=f"https://x.com/cy_example/status/{COPY}",
                    title="Cy on X",
                    snippets=[TEXT],
                    position=1,
                ),
                SearchHit(
                    url=f"https://x.com/ada_example/status/{SUBJ}",
                    title="Ada on X",
                    snippets=[TEXT],
                    position=2,
                ),
                SearchHit(
                    url="https://x.com/zed/status/2121364193509277999",
                    title='Zed on X: "unrelated cats"',
                    snippets=["cats"],
                    position=3,
                ),
            ]
        return [
            SearchHit(
                url="https://news.example/story",
                title="Comet joke",
                position=1,
                snippets=[f"1 hour ago · {TEXT}"],
            ),
        ]


class FakeStore:
    def clusters_for_url(self, urls):
        return []

    def clusters_for_text(self, phrase):
        return [
            ClusterSummary(
                id=7,
                status="coordinated",
                score=0.9,
                window_start=datetime(2026, 11, 13, tzinfo=UTC),
                window_end=datetime(2026, 11, 13, tzinfo=UTC),
            )
        ]

    def clusters_for_handle(self, handle):
        return []


@contextmanager
def fake_store():
    yield FakeStore()


def make_deps(store=None):
    return Deps(oxylabs=FakeOxylabs(), bsky=None, store=store, nemotron=None, assessor=None)


def test_lookup_run_end_to_end():
    job = Lookup("lk_t", LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"), make_deps())
    job.run()
    r = job.result
    assert r.state == "done" and r.kind == "x_post"
    states = {s.id: s.state for s in r.steps}
    assert states["post"] == states["author"] == states["copies_x"] == "done"
    assert states["monitor"] == "skipped" and states["media"] == "skipped"  # not configured
    posts = {p.id: p for p in r.posts}
    subj = posts[f"x:{SUBJ}"]
    assert subj.role == "submitted" and subj.metrics.views == 2_900_000
    assert subj.created_at == snowflake_time(SUBJ) and subj.time_source == "snowflake"
    # the search said "copy"; reading its page showed it quotes the post
    assert posts[f"x:{COPY}"].role == "quote" and posts[f"x:{COPY}"].verified
    assert "x:2121364193509277999" not in posts  # loose keyword hit dropped
    assert any(e.from_ == f"x:{SUBJ}" and e.to == f"x:{COPY}" and e.rel == "quote" for e in r.edges)
    acct = next(a for a in r.accounts if a.handle == "ada_example")
    assert acct.followers == 252_600 and acct.joined == "2009-06" and acct.age_days > 6000
    assert r.status == "insufficient"  # never a verdict from one link's footprint
    assert r.origin_id == f"x:{SUBJ}"
    assert all(rc.id == k + 1 for k, rc in enumerate(r.receipts))
    assert any("not every copy" in n for n in r.notes)
    snap = job.snapshot()
    assert snap["edges"][0].keys() >= {"from", "to", "rel"}  # serialized with "from"


def test_matched_cluster_sets_status():
    job = Lookup("lk_m", LookupInput(text=TEXT), make_deps(store=fake_store))
    job.run()
    assert job.result.status == "matched" and job.result.clusters[0]["id"] == 7
    assert {s.id: s.state for s in job.result.steps}["post"] == "skipped"


def test_guard_drops_unknown_citations():
    job = Lookup("lk_g", LookupInput(text=TEXT), make_deps())
    job.run()
    d = _Draft(
        summary="s",
        origin="o",
        corroboration="c",
        spread_pattern="p",
        cites=[999],
        llm_reading="copy_campaign_looking",
    )
    a = guard(d, job.result, model="gemini-test")
    assert a.cites == [] and a.llm_reading == "unclear"
    d.cites = [1]
    assert guard(d, job.result, model="gemini-test").llm_reading == "copy_campaign_looking"


class FakeGeminiModels:
    """Raises the queued errors in order, then answers with a valid reading."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.timeouts_ms = []

    def generate_content(self, *, model, contents, config):
        self.timeouts_ms.append(config.http_options.timeout)
        if self.errors:
            raise self.errors.pop(0)
        draft = _Draft(summary="s", origin="o", corroboration="c", spread_pattern="p",
                       cites=[1], llm_reading="unclear")  # fmt: skip
        return SimpleNamespace(usage_metadata=None, parsed=draft, text=draft.model_dump_json())


def _assessor(models):
    a = Assessor("test-key", "gemini-test")
    a._client = SimpleNamespace(models=models)
    return a


def _deadline_exceeded():
    from google.genai import errors

    body = {"error": {"code": 504, "message": "Deadline expired", "status": "DEADLINE_EXCEEDED"}}
    return errors.ServerError(504, body)


def test_assess_retries_once_on_deadline_exceeded():
    job = Lookup("lk_a", LookupInput(text=TEXT), make_deps())
    job.run()
    models = FakeGeminiModels(_deadline_exceeded())
    a = _assessor(models).assess(job.result, deadline=time.monotonic() + 60)
    assert a.summary == "s" and len(models.timeouts_ms) == 2
    # each try may use what is left of the budget (the timeout is also Gemini's server deadline)
    assert all(50_000 < t <= 60_000 for t in models.timeouts_ms)


def test_assess_gives_up_without_budget_or_after_second_failure():
    from google.genai import errors

    job = Lookup("lk_b", LookupInput(text=TEXT), make_deps())
    job.run()
    models = FakeGeminiModels(_deadline_exceeded())
    with pytest.raises(errors.ServerError):  # 10 s left: too little for a second reading
        _assessor(models).assess(job.result, deadline=time.monotonic() + 10)
    assert len(models.timeouts_ms) == 1
    models = FakeGeminiModels(_deadline_exceeded(), _deadline_exceeded())
    with pytest.raises(errors.ServerError):
        _assessor(models).assess(job.result, deadline=time.monotonic() + 80)
    assert len(models.timeouts_ms) == 2
    models = FakeGeminiModels()
    _assessor(models).assess(job.result)  # no deadline: capped, never unbounded
    assert models.timeouts_ms == [int(MAX_CALL_S * 1000)]


def test_jobstore_cache_and_limits():
    clock = [0.0]
    store = JobStore(make_deps, max_per_hour=2, max_per_client_per_hour=5, clock=lambda: clock[0])
    a = store.start(LookupInput(text=TEXT), "c1")
    assert store.start(LookupInput(text=TEXT.upper()), "c1") is a  # cached by normalised key
    store.start(LookupInput(text=TEXT + " again"), "c1")
    with pytest.raises(RateLimited):
        store.start(LookupInput(text=TEXT + " third"), "c2")
    assert cache_key(LookupInput(url=f"https://twitter.com/ada_example/status/{SUBJ}")) == (
        f"url:https://x.com/ada_example/status/{SUBJ}"
    )


def test_api_validation_and_polling(monkeypatch):
    store = JobStore(make_deps, max_per_hour=10, max_per_client_per_hour=10)
    app.dependency_overrides[lookup_route.get_jobs] = lambda: store
    try:
        c = TestClient(app)
        assert c.post("/api/lookup", json={}).status_code == 400
        assert c.post("/api/lookup", json={"url": "ftp://x"}).status_code == 400
        assert c.post("/api/lookup", json={"text": "too short"}).status_code == 400
        res = c.post("/api/lookup", json={"text": TEXT})
        assert res.status_code in (200, 202)
        jid = res.json()["id"]
        for _ in range(100):
            body = c.get(f"/api/lookup/{jid}").json()
            if body["state"] != "running":
                break
        assert body["state"] == "done"
        assert c.get("/api/lookup/lk_nope").status_code == 404
    finally:
        app.dependency_overrides.pop(lookup_route.get_jobs, None)


def test_submitted_role_survives_merge_order():
    job = Lookup("lk_o", LookupInput(text=TEXT), make_deps())
    from lookup.models import Post

    job.add_post(Post(id="x:1", platform="x", role="author_recent", verified=True))
    job.add_post(Post(id="x:1", platform="x", role="submitted", verified=True, text="t"))
    job.add_post(Post(id="x:1", platform="x", role="author_recent", verified=True))
    (p,) = job.result.posts
    assert p.role == "submitted" and p.similarity == 1.0 and p.text == "t"


# ---- regressions from review (Sol r1)


def test_escaped_handle_in_avatar_label():
    md = QUOTE_MD.replace("[![@cy_example]", "[![@cy\\_example]")
    assert parse_post_page(md, COPY).main is not None


def test_likes_only_tail_is_not_reposts():
    md = POST_MD.replace("\n\n  87\n\n  2.5K\n\n  242K", "\n\n  35K")
    r = parse_post_page(md, SUBJ).replies[0]
    assert (r.reposts, r.likes, r.views) == (None, None, None)


def _job(text=TEXT, store=None):
    return Lookup("lk_r", LookupInput(text=text), make_deps(store=store))


def test_unverified_hit_is_not_a_copy_or_origin():
    from lookup.models import Post

    job = _job()
    # a search snippet claims an older copy; it was never read on its own page
    job.add_post(
        Post(
            id="x:1",
            platform="x",
            role="mention",
            similarity=1.0,
            text=TEXT,
            created_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
    )
    job.add_post(
        Post(
            id="x:2",
            platform="x",
            role="submitted",
            verified=True,
            text=TEXT,
            created_at=datetime(2026, 11, 13, tzinfo=UTC),
        )
    )
    job.result.subject_id = "x:2"
    job._finish_graph()
    assert job.result.origin_id == "x:2"
    assert job.result.spread.n_posts == 1


def test_snippet_does_not_overwrite_verified_post():
    from lookup.models import Post

    job = _job()
    job.add_post(Post(id="x:5", platform="x", role="mention", verified=True, text="read"))
    job.add_post(Post(id="x:5", platform="x", role="copy", verified=False, text="snippet"))
    (p,) = job.result.posts
    assert p.verified and p.text == "read" and p.role == "mention"


def test_only_pages_dated_on_the_page_can_be_the_origin():
    from lookup.models import Page, Post

    job = _job()
    job.add_post(
        Post(
            id="x:2",
            platform="x",
            role="submitted",
            verified=True,
            text=TEXT,
            created_at=datetime(2026, 11, 13, 21, 50, tzinfo=UTC),
        )
    )
    job.result.subject_id = "x:2"
    day = 86400
    # a search snippet says Nov 1: not read, so not the origin
    job.add_page(
        Page(
            id="",
            url="https://s.example/",
            role="reference",
            time_source="search_hint",
            time=datetime(2026, 11, 1, tzinfo=UTC),
            time_uncertainty_s=day,
        )
    )
    # read on the page, but date-only on the same day: may be later than the post
    job.add_page(
        Page(
            id="",
            url="https://a.example/",
            role="reference",
            time_source="page",
            time=datetime(2026, 11, 13, tzinfo=UTC),
            time_uncertainty_s=day,
        )
    )
    job._finish_graph()
    assert job.result.origin_id == "x:2"
    # read on the page, the whole day before: really earlier
    job.add_page(
        Page(
            id="",
            url="https://b.example/",
            role="reference",
            time_source="page",
            time=datetime(2026, 11, 12, tzinfo=UTC),
            time_uncertainty_s=day,
        )
    )
    job._finish_graph()
    assert job.result.origin_id == "page:3"


def test_older_snippet_page_is_read_before_it_can_be_origin():
    job = Lookup("lk_v", LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"), make_deps())
    job.run()
    # the web search hit ("1 hour ago") is not older than the post, so it isn't scraped
    assert not any(c[0] == "facts" for c in job.deps.oxylabs.calls)


def test_finished_result_is_frozen():
    from lookup.models import Post

    job = _job()
    job.run()
    before = job.snapshot()
    job.add_post(Post(id="x:late", platform="x", role="copy", verified=True))
    assert job.snapshot() == before  # a worker that outlived the budget can't change it


class AccountOnlyStore(FakeStore):
    def clusters_for_text(self, phrase):
        return []

    def clusters_for_handle(self, handle):
        return FakeStore.clusters_for_text(self, "")


def test_account_only_match_is_context_not_verdict():
    @contextmanager
    def store():
        yield AccountOnlyStore()

    job = _job(store=store)
    job._bsky_handles.add("copier.bsky.social")
    job.run()
    assert job.result.status == "insufficient"
    (c,) = job.result.clusters
    assert c["match"] == ["account"] and c["verdict_applies"] is False


def test_quote_of_another_post_is_attributed_to_it():
    from lookup.models import Post

    other = "2121361111111111111"
    md = QUOTE_MD.replace(f"/ada_example/status/{SUBJ})", f"/ada_example/status/{other})")
    job = Lookup("lk_q", LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"), make_deps())
    job.result.subject_id = f"x:{SUBJ}"
    job.deps.oxylabs.scrape_markdown = lambda url, deadline=None: (md, {})
    job._verify_x(
        Post(
            id=f"x:{COPY}",
            platform="x",
            url=f"https://x.com/cy_example/status/{COPY}",
            role="mention",
        ),
        TEXT,
    )
    edges = {(e.from_, e.to, e.rel) for e in job.result.edges}
    assert (f"x:{other}", f"x:{COPY}", "quote") in edges
    assert not any(e[0] == f"x:{SUBJ}" for e in edges)


def test_time_budget_stops_provider_calls():
    deps = make_deps()
    deps.time_budget_s = 0.0
    job = Lookup("lk_b", LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"), deps)
    job.run()
    assert job.result.state == "done"
    assert deps.oxylabs.calls == []  # nothing started after the budget was gone


def test_linked_origin_gets_no_invented_edge_to_an_older_copy():
    from lookup.models import Page, Post

    job = _job()
    t13 = datetime(2026, 11, 13, 21, 50, tzinfo=UTC)
    job.add_post(
        Post(id="x:2", platform="x", role="submitted", verified=True, text=TEXT, created_at=t13)
    )
    job.add_post(
        Post(
            id="x:1",
            platform="x",
            role="copy",
            verified=True,
            text=TEXT,
            similarity=1.0,
            created_at=datetime(2026, 11, 13, 20, 0, tzinfo=UTC),
        )
    )
    job.result.subject_id = "x:2"
    pg = job.add_page(
        Page(
            id="",
            url="https://news.example/a",
            role="linked",
            time_source="page",
            time=datetime(2026, 11, 13, 9, 0, tzinfo=UTC),
            time_uncertainty_s=0,
        )
    )
    job.add_edge(pg.id, "x:2", "link")  # what _linked records: B links it
    job._finish_graph()
    assert job.result.origin_id == pg.id
    assert not any(e.from_ == pg.id and e.to == "x:1" for e in job.result.edges)


def test_scraped_page_facts_win_over_an_earlier_snippet():
    from lookup.models import Page

    job = _job()
    job.add_page(
        Page(
            id="",
            url="https://news.example/a",
            role="coverage",
            time_source="search_hint",
            time=datetime(2026, 11, 13, tzinfo=UTC),
            time_uncertainty_s=86400,
        )
    )
    job.add_page(
        Page(
            id="",
            url="https://news.example/a",
            role="linked",
            publisher="Example",
            published_at="2026-11-13T09:00:00Z",
            time_source="page",
            time=datetime(2026, 11, 13, 9, tzinfo=UTC),
            time_uncertainty_s=0,
            receipt_id=4,
        )
    )
    (pg,) = job.result.pages
    assert (pg.id, pg.role, pg.publisher, pg.time_source, pg.receipt_id) == (
        "page:1",
        "linked",
        "Example",
        "page",
        4,
    )


def test_bluesky_retry_never_starts_after_the_deadline():
    import time as _time

    import httpx

    from lookup.bsky import BlueskyClient, BlueskyTimeUp

    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(503)

    c = BlueskyClient(transport=httpx.MockTransport(handler))
    with pytest.raises(BlueskyTimeUp):
        c.search("q", deadline=_time.monotonic() - 1)
    assert calls == []
    with pytest.raises((httpx.HTTPStatusError, BlueskyTimeUp)):
        c.search("q", deadline=_time.monotonic() + 1.2)  # first try ok, retry would be late
    assert len(calls) == 1


def test_undated_scrape_still_merges_its_facts():
    from lookup.models import Page

    job = _job()
    job.add_page(Page(id="", url="https://news.example/b", role="coverage", snippet="s"))
    job.add_page(
        Page(
            id="",
            url="https://news.example/b",
            role="linked",
            author="A. Writer",
            final_url="https://news.example/b?x=1",
            claims=["c"],
            receipt_id=9,
        )
    )
    (pg,) = job.result.pages
    assert (pg.author, pg.final_url, pg.claims, pg.receipt_id) == (
        "A. Writer",
        "https://news.example/b?x=1",
        ["c"],
        9,
    )


XPAGES = Path(__file__).parent / "fixtures" / "xpages"


@pytest.mark.parametrize("path", sorted(XPAGES.glob("*_*.md")))
def test_x_unavailable_fixture(path):
    assert unavailable_reason(path.read_text()) == path.name.split("_")[1]


@pytest.mark.parametrize("md", [POST_MD, QUOTE_MD, PROFILE_MD])
def test_live_x_page_is_not_unavailable(md):
    assert unavailable_reason(md) is None


@pytest.mark.parametrize("md", [POST_MD, PROFILE_MD])
def test_tombstone_sentence_inside_live_post_is_not_a_notice(md):
    # Both the unindented main post and the indented timeline item are actual posts.
    md = md.replace(TEXT, "This post is from a suspended account.")
    md = md.replace("later post", "This post is from a suspended account.")
    assert unavailable_reason(md) is None


@pytest.mark.parametrize("apostrophe", ["'", "’"])
def test_unknown_x_profile_notice(apostrophe):
    assert unavailable_reason(f"## This account doesn{apostrophe}t exist") == "not_found"


@pytest.mark.parametrize(
    "md,reason",
    [
        ("* This Post is from a suspended account. [Learn more](https://help.x.com)", "suspended"),
        (
            "* You’re unable to view this Post because this account owner limits "
            "who can view their Posts. [Learn more](https://help.x.com)",
            "protected",
        ),
        ("## We’re unable to show this content", "unavailable"),
    ],
)
def test_x_notice_alternate_forms(md, reason):
    assert unavailable_reason(md) == reason


@pytest.mark.parametrize(
    "post_file,profile_file,handle,status_id,detail,author_detail",
    [
        (
            "post_suspended_LeroyLovesUSA_796772863869075460.md",
            "profile_suspended_LeroyLovesUSA.md",
            "LeroyLovesUSA",
            "796772863869075460",
            "X shows this post is from a suspended account",
            "account suspended",
        ),
        (
            "post_protected_chrismessina_223115412.md",
            "profile_protected_chrismessina.md",
            "chrismessina",
            "223115412",
            "X shows this post is from a protected account (only approved followers can see it)",
            "joined 2006-07 · 98.3K followers · posts are protected",
        ),
        (
            "post_unavailable_JackPosobiec_860567072010620929.md",
            None,
            "JackPosobiec",
            "860567072010620929",
            "X says this post isn't available (it may be private, deleted or app-only)",
            "joined 2009-06 · 252.6K followers · 2 recent posts",
        ),
        (
            "post_unavailable_realDonaldTrump_869766994899468288.md",
            None,
            "realDonaldTrump",
            "869766994899468288",
            "X says this post isn't available (it may be private, deleted or app-only)",
            "joined 2009-06 · 252.6K followers · 2 recent posts",
        ),
    ],
)
def test_lookup_x_tombstones(post_file, profile_file, handle, status_id, detail, author_detail):
    class TombstoneOxylabs(FakeOxylabs):
        def scrape_markdown(self, url, deadline=None):
            self.calls.append(("scrape", url))
            if "/status/" in url:
                return (XPAGES / post_file).read_text(), {}
            md = (
                (XPAGES / profile_file).read_text()
                if profile_file
                else PROFILE_MD.replace("ada_example", handle).replace(r"ada\_example", handle)
            )
            return md, {}

    deps = make_deps()
    deps.oxylabs = TombstoneOxylabs()
    job = Lookup(
        "lk_tombstone", LookupInput(url=f"https://x.com/{handle}/status/{status_id}"), deps
    )
    job.run()
    steps = {s.id: s for s in job.result.steps}
    assert job.result.state == "done"
    assert (steps["post"].state, steps["post"].detail) == ("done", detail)
    assert (steps["author"].state, steps["author"].detail) == ("done", author_detail)
    assert all("Error" not in s.detail for s in job.result.steps)
    assert f"{detail}, so its text can't be read; copies and spread were not searched." in (
        job.result.notes
    )
    assert job._subject_time == snowflake_time(status_id)
    assert any(r.ok and r.summary == detail for r in job.result.receipts)
    assert all(r.ok for r in job.result.receipts if r.source_url == f"https://x.com/{handle}")
    for sid in ("copies_web", "copies_x", "copies_bsky"):
        assert (steps[sid].state, steps[sid].detail) == (
            "skipped",
            "the post's text couldn't be read",
        )
    assert not [c for c in deps.oxylabs.calls if c[0] == "search"]


@pytest.mark.parametrize(
    "error,state,detail",
    [
        (
            OxylabsError("retries exhausted", status=500),
            "failed",
            "Oxylabs couldn't read the page (HTTP 500)",
        ),
        (OxylabsError("transport failed"), "failed", "Oxylabs couldn't read the page"),
        (RuntimeError("private internal detail"), "failed", "unexpected error (logged)"),
        (
            StepUnavailable("profile not readable logged out"),
            "failed",
            "profile not readable logged out",
        ),
        (StepUnavailable("account suspended", "done"), "done", "account suspended"),
        (StepUnavailable("not configured", "skipped"), "skipped", "not configured"),
    ],
)
def test_lookup_step_clean_errors(error, state, detail):
    job = Lookup("lk_error", LookupInput(text=TEXT), make_deps())
    with job.step("post") as st:
        st.detail = "partly read"
        raise error
    assert (st.state, st.detail) == (state, detail)
    assert st.ended_at is not None


@pytest.mark.parametrize(
    "md,state,detail",
    [
        ("## This account doesn’t exist", "done", "account doesn't exist"),
        ("## Profile", "failed", "profile not readable logged out"),
    ],
)
def test_lookup_unknown_or_unreadable_profile(md, state, detail):
    class ProfileOxylabs(FakeOxylabs):
        def scrape_markdown(self, url, deadline=None):
            return md, {}

    job = Lookup(
        "lk_profile", LookupInput(url="https://x.com/missing"), Deps(oxylabs=ProfileOxylabs())
    )
    job.run()
    st = job._step("author")
    assert (st.state, st.detail) == (state, detail)
    if state == "done":
        assert any(
            r.ok and r.summary == "X profile @missing: account doesn't exist"
            for r in job.result.receipts
        )


def test_lookup_unrecognized_post_still_fails():
    class EmptyOxylabs(FakeOxylabs):
        def scrape_markdown(self, url, deadline=None):
            return "## Post", {}

    job = Lookup(
        "lk_empty",
        LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"),
        Deps(oxylabs=EmptyOxylabs()),
    )
    job.run()
    assert (job._step("post").state, job._step("post").detail) == (
        "failed",
        "post not visible logged out",
    )
    assert job._step("copies_web").detail == "the post's text couldn't be read"


def test_lookup_short_read_text_keeps_short_skip_message():
    job = Lookup(
        "lk_short", LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"), make_deps()
    )
    job.result.subject_id = f"x:{SUBJ}"
    job._subject_text = "Hello"
    job._copies_web()
    job._copies_x()
    job._copies_bsky()
    assert job._step("copies_web").detail == "text too short for an exact-phrase search"
    assert job._step("copies_x").detail == "text too short for an exact-phrase search"
    assert job._step("copies_bsky").detail == "text too short"


def test_lookup_monitor_matches_twitter_link_from_x_input():
    class AliasStore(FakeStore):
        def clusters_for_url(self, urls):
            return (
                super().clusters_for_text("")
                if f"https://twitter.com/ada_example/status/{SUBJ}" in urls
                else []
            )

    @contextmanager
    def store():
        yield AliasStore()

    job = Lookup(
        "lk_alias",
        LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"),
        make_deps(store=store),
    )
    job._monitor()
    assert job.result.status == "matched"
    assert job.result.clusters[0]["match"] == ["link"]
    assert job.result.clusters[0]["verdict_applies"]


SUSPENDED_ITEM = (
    "* This Post is from a suspended account. "
    "[Learn more](https://help.x.com/rules-and-policies/notices-on-x)\n"
)


def test_suspended_reply_does_not_make_a_live_post_or_profile_unavailable():
    # A live post with a reply (or a profile with a timeline item) from a suspended account.
    post_md, profile_md = POST_MD + SUSPENDED_ITEM, PROFILE_MD + SUSPENDED_ITEM
    assert unavailable_reason(post_md) == "suspended"  # the bare detector sees the notice
    assert page_unavailable_reason(post_md, f"https://x.com/ada_example/status/{SUBJ}") is None
    assert page_unavailable_reason(profile_md, "https://x.com/ada_example") is None
    tomb = (XPAGES / "post_suspended_LeroyLovesUSA_796772863869075460.md").read_text()
    url = "https://x.com/LeroyLovesUSA/status/796772863869075460"
    assert page_unavailable_reason(tomb, url) == "suspended"
    prof = (XPAGES / "profile_suspended_LeroyLovesUSA.md").read_text()
    assert page_unavailable_reason(prof, "https://x.com/LeroyLovesUSA") == "suspended"


def test_live_author_with_a_suspended_timeline_item_is_not_called_suspended():
    class Oxy(FakeOxylabs):
        def scrape_markdown(self, url, deadline=None):
            md, meta = super().scrape_markdown(url, deadline)
            return (md + SUSPENDED_ITEM if url.endswith("/ada_example") else md), meta

    deps = make_deps()
    deps.oxylabs = Oxy()
    job = Lookup("lk_live", LookupInput(url=f"https://x.com/ada_example/status/{SUBJ}"), deps)
    job.run()
    author = next(s for s in job.result.steps if s.id == "author")
    assert author.state == "done" and "suspended" not in author.detail
    assert author.detail.startswith("joined ")


def test_profile_bio_or_name_saying_suspended_is_not_a_notice():
    for words in ("Account suspended", "These posts are protected"):
        bio = PROFILE_MD.replace("Writer of fictional test fixtures.", words)
        name = PROFILE_MD.replace("# Ada Example", f"# {words}")
        for md in (bio, name):
            assert page_unavailable_reason(md, "https://x.com/ada_example") is None
    prot = (XPAGES / "profile_protected_chrismessina.md").read_text()
    assert page_unavailable_reason(prot, "https://x.com/chrismessina") == "protected"


def test_suspended_post_with_user_text_says_copies_use_that_text():
    class Oxy(FakeOxylabs):
        def scrape_markdown(self, url, deadline=None):
            self.calls.append(("scrape", url))
            if "/status/" in url:
                name = "post_suspended_LeroyLovesUSA_796772863869075460.md"
            else:
                name = "profile_suspended_LeroyLovesUSA.md"
            return (XPAGES / name).read_text(), {}

    deps = make_deps()
    deps.oxylabs = Oxy()
    url = "https://x.com/LeroyLovesUSA/status/796772863869075460"
    job = Lookup("lk_text", LookupInput(url=url, text=TEXT), deps)
    job.run()
    assert (
        "X shows this post is from a suspended account, so its text can't be read; "
        "copies are searched with the text you gave." in job.result.notes
    )
    steps = {s.id: s for s in job.result.steps}
    assert steps["copies_web"].detail != "the post's text couldn't be read"


def test_link_resolving_to_an_x_tombstone_is_not_read_as_content():
    tomb = "https://x.com/realDonaldTrump/status/869766994899468288"

    class Oxy(FakeOxylabs):
        def scrape_facts(self, url, deadline=None):
            self.calls.append(("facts", url))
            return PageFacts(
                url=url,
                final_url=tomb,
                http_status=404,
                publisher="X",
                headline="We're unable to show this content",
                claims=["The page does not exist."],
                unavailable="unavailable",
            )

    deps = make_deps()
    deps.oxylabs = Oxy()
    job = Lookup("lk_web_tomb", LookupInput(url="https://news.example/redirect"), deps)
    job.run()
    steps = {s.id: s for s in job.result.steps}
    detail = "X says this post isn't available (it may be private, deleted or app-only)"
    assert (steps["linked"].state, steps["linked"].detail) == ("done", detail)
    assert not [c for c in deps.oxylabs.calls if c[0] == "search"]
    assert all(p.headline is None and not p.claims for p in job.result.pages)
    assert any(r.summary == f"Linked page read: {detail}" for r in job.result.receipts)
    assert f"{detail}, so its text can't be read; copies and spread were not searched." in (
        job.result.notes
    )
    for sid in ("copies_web", "copies_x"):
        assert steps[sid].detail == "the post's text couldn't be read"


@pytest.mark.parametrize("order", ["page_text_first", "facts_first", "facts_fail"])
def test_link_to_an_x_tombstone_never_seeds_searches_whatever_finishes_first(order):
    import threading

    tomb = "https://x.com/LeroyLovesUSA/status/796772863869075460"
    md = (XPAGES / "post_suspended_LeroyLovesUSA_796772863869075460.md").read_text()
    page_done, facts_done = threading.Event(), threading.Event()

    class Oxy(FakeOxylabs):
        def scrape_markdown(self, url, deadline=None):
            self.calls.append(("scrape", url))
            if order == "facts_first":
                facts_done.wait(5)
            try:
                return md, {"url": tomb}
            finally:
                page_done.set()

        def scrape_facts(self, url, deadline=None):
            self.calls.append(("facts", url))
            try:
                if order == "page_text_first":
                    page_done.wait(5)
                if order == "facts_fail":
                    page_done.wait(5)
                    raise OxylabsError("oxylabs /scrape failed (500)", status=500)
                return PageFacts(url=url, final_url=tomb, headline="Post", unavailable="suspended")
            finally:
                facts_done.set()

    deps = make_deps()
    deps.oxylabs = Oxy()
    job = Lookup("lk_race", LookupInput(url="https://news.example/redirect"), deps)
    job.run()
    detail = "X shows this post is from a suspended account"
    assert not [c for c in deps.oxylabs.calls if c[0] == "search"]
    assert all(p.headline is None and not p.claims for p in job.result.pages)
    assert job._subject_text is None and job._card_title is None
    assert f"{detail}, so its text can't be read; copies and spread were not searched." in (
        job.result.notes
    )
    assert [r.summary for r in job.result.receipts].count(f"Linked page read: {detail}") == 1
    steps = {s.id: s for s in job.result.steps}
    if order == "facts_fail":
        assert steps["linked"].detail == "Oxylabs couldn't read the page (HTTP 500)"
    else:
        assert (steps["linked"].state, steps["linked"].detail) == ("done", detail)
