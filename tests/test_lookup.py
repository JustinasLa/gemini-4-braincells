"""lookup/: URL classification, X page parsing, similarity, the job, the API (no network).

Fixtures are fictional accounts in the shape Oxylabs returns for logged-out X pages.
"""

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.routes import lookup as lookup_route
from api.schemas import ClusterSummary
from lookup import textsim
from lookup.assess import _Draft, guard
from lookup.jobs import JobStore, RateLimited, cache_key
from lookup.models import LookupInput
from lookup.service import Deps, Lookup, _x_title, snippet_time
from lookup.urls import classify, snowflake_time
from lookup.xpage import parse_count, parse_post_page, parse_profile
from sources.models import PageFacts, SearchHit

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
