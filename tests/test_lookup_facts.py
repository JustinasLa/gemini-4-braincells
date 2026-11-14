import json
import threading
from datetime import UTC, datetime

import pytest

from lookup.assess import SYSTEM, _Draft, brief, guard
from lookup.models import ClaimCheck, LookupInput, Page, Post
from lookup.service import Lookup
from sources.models import PageFacts, SearchHit
from tests.test_lookup import make_deps

POST_URL = "https://x.com/Theveripost/status/2121190913804468478"
CLAIM = "Sanders's Medicare drug-pricing amendment was defeated 99–1."
PHOTO_CAPTION = (
    "Senator Bernie Sanders sitting alone on the steps of the U.S. Capitol in Washington, "
    "D.C. after his amendment to cut Medicare drug prices by 50% was defeated 99-1, 2022."
)
ARTICLE_URL = (
    "https://www.businessinsider.com/"
    "bernie-sanders-senate-steps-photo-leads-comparisons-schoolhouse-rock-2022-8"
)


def lookup():
    return Lookup("lk_facts", LookupInput(url=POST_URL), make_deps())


def draft(cites, finding="supported"):
    return _Draft(
        summary="The claim is supported.",
        origin="Earliest observed copy in the sample.",
        corroboration="A procedural vote, not a blanket reduction on every drug.",
        spread_pattern="Copies alone do not prove coordination.",
        cites=cites,
        claim_checks=[
            ClaimCheck(
                claim=CLAIM,
                finding=finding,
                explanation="The motion to waive budget rules failed 1–99.",
                cites=cites,
            )
        ],
        llm_reading="unclear",
    )


def source(job, endpoint="/scrape", *, ok=True, claims=True):
    rid = job.receipt(
        "oxylabs", endpoint, "Source record", source_url="https://record.example/vote", ok=ok
    )
    page = job.add_page(
        Page(
            id="",
            url="https://record.example/vote",
            receipt_id=rid,
            claims=["Motion to waive budget rules: 1 yea, 99 nays."] if claims else [],
        )
    )
    return rid, page


def test_supported_facts_do_not_change_coordination_abstention():
    job = lookup()
    rid, _ = source(job)
    result = guard(draft([rid, rid, 999]), job.result, model="test")
    assert result.claim_checks[0].finding == "supported"
    assert result.claim_checks[0].cites == result.cites == [rid]
    assert job.result.status == "insufficient"
    assert job.result.input.url == POST_URL


@pytest.mark.parametrize(
    "claim",
    [
        CLAIM,
        "The trial reported a relative reduction, not a guaranteed benefit for every patient.",
        "The company forecast revenue growth rather than reporting realized revenue.",
    ],
)
def test_full_lookup_reads_sources_before_assessing_facts(claim):
    deps = make_deps()

    def scrape_facts(url, deadline=None):
        deps.oxylabs.calls.append(("facts", url))
        return PageFacts(url=url, final_url=url, claims=[claim])

    deps.oxylabs.scrape_facts = scrape_facts

    class ReadingAssessor:
        model = "test"

        def assess(self, result, *, deadline=None):
            page = next(page for page in result.pages if page.claims)
            result_draft = draft([page.receipt_id])
            result_draft.claim_checks[0].claim = claim
            result_draft.claim_checks[0].explanation = claim
            return guard(result_draft, result, model=self.model)

    deps.assessor = ReadingAssessor()
    job = Lookup("lk_integration", LookupInput(text=claim), deps)
    job.run()
    result = job.snapshot()
    assert result["state"] == "done"
    assert result["status"] == "insufficient"
    assert result["assessment"]["claim_checks"][0]["finding"] == "supported"
    assert result["assessment"]["claim_checks"][0]["claim"] == claim
    assert any(call[0] == "facts" for call in deps.oxylabs.calls)


@pytest.mark.parametrize("finding", ["supported", "contradicted", "mixed"])
@pytest.mark.parametrize(
    "evidence", ["snippet", "failed", "empty", "submitted", "invented", "post"]
)
def test_decisive_findings_require_read_independent_source_text(finding, evidence):
    job = lookup()
    rid, page = source(
        job,
        "/search" if evidence == "snippet" else "/scrape",
        ok=evidence != "failed",
        claims=evidence != "empty",
    )
    if evidence == "submitted":
        job.result.subject_id = page.id
    if evidence == "invented":
        rid = 999
    if evidence == "post":
        job.result.pages = []
    result = guard(draft([rid], finding), job.result, model="test")
    check = result.claim_checks[0]
    assert check.finding == "unverified"
    assert "do not independently verify" in check.explanation
    assert result.summary != "The claim is supported."
    assert "lack independently read supporting sources" in result.corroboration


def test_claim_citations_are_checked_per_claim_not_globally():
    job = lookup()
    rid, _ = source(job)
    result_draft = draft([rid])
    result_draft.claim_checks[0].cites = [999]
    result = guard(result_draft, job.result, model="test")
    assert result.claim_checks[0].finding == "unverified"
    assert result.claim_checks[0].cites == []


@pytest.mark.parametrize("endpoint", ["/search", "/scrape"])
def test_claim_citations_cannot_unlock_uncited_coordination_reading(endpoint):
    job = lookup()
    rid, _ = source(job, endpoint)
    result_draft = draft([rid])
    result_draft.cites = []
    result_draft.llm_reading = "copy_campaign_looking"
    result = guard(result_draft, job.result, model="test")
    assert result.cites == [rid]
    assert result.llm_reading == "unclear"


def test_failed_receipt_can_describe_read_failure_but_not_verify_claim():
    job = lookup()
    rid, _ = source(job, ok=False)
    result_draft = draft([rid])
    result_draft.claim_checks = []
    result_draft.summary = "The submitted post was not found on the page read."
    result = guard(result_draft, job.result, model="test")
    assert result.cites == [rid]
    assert result.summary == result_draft.summary
    result = guard(draft([rid]), job.result, model="test")
    assert result.claim_checks[0].cites == []
    assert result.claim_checks[0].finding == "unverified"


def test_uncited_summary_allows_input_read_failure_fallback():
    result = guard(draft([999]), lookup().result, model="test")
    assert result.summary == ""
    assert result.llm_reading == "unclear"


def test_media_description_does_not_authenticate_exact_image():
    job = lookup()
    rid, _ = source(job)
    job.result.subject_id = "x:2121190913804468478"
    job.result.posts.append(
        Post(
            id=job.result.subject_id,
            platform="x",
            role="submitted",
            media_urls=["https://images.example/photo.jpg"],
        )
    )
    result = guard(draft([rid]), job.result, model="test")
    assert "No source-backed media attribution is available" in result.media_provenance
    assert "caption" in result.media_provenance


def test_legacy_assessment_and_no_media_remain_compatible():
    job = lookup()
    rid, _ = source(job)
    old = draft([rid]).model_dump(exclude={"claim_checks"})
    result = guard(_Draft.model_validate(old), job.result, model="test")
    assert result.claim_checks == []
    assert result.media_provenance is None


def test_brief_distinguishes_read_pages_from_search_hints_and_keeps_urls():
    job = lookup()
    snippet_id = job.receipt("oxylabs", "/search", "Candidate", query="claim")
    job.add_page(Page(id="", url="https://snippet.example", receipt_id=snippet_id))
    rid, _ = source(job)
    data = json.loads(brief(job.result, max_pages=1))
    assert data["pages"][0]["receipt_id"] == rid
    assert data["pages"][0]["evidence_kind"] == "read_page"
    assert data["receipts"][1]["source_url"] == "https://record.example/vote"
    data = json.loads(brief(job.result))
    assert data["pages"][1]["evidence_kind"] == "search_hint"
    assert "not proof of original authorship" in data["origin_scope"]
    assert "earliest_known_source_id" not in data


def test_prompt_requires_general_claim_and_provenance_caveats():
    for requirement in (
        "procedural vote",
        "forecast",
        "chronology from causation",
        "model memory",
        "not a match to the exact submitted image",
        "untrusted evidence",
        "independent",
    ):
        assert requirement in SYSTEM


def test_source_reads_are_bounded_diverse_and_prioritize_original_records():
    job = lookup()
    for url in (
        "https://news.example/a",
        "https://news.example/b",
        "https://records.gov/vote",
        "https://other.example/story",
    ):
        job.add_page(Page(id="", url=url))
    selected = job._source_page_candidates()
    assert len(selected) == 3
    assert selected[0].url == "https://records.gov/vote"
    assert len({page.url.split("/")[2] for page in selected}) == 3
    job.deps.source_pages_max = 0
    assert job._source_page_candidates() == []


def test_source_read_keeps_undated_text_but_removes_search_date():
    job = lookup()
    _, page = source(job, "/search")
    page.time = datetime(2022, 8, 7, tzinfo=UTC)
    page.time_source = "search_hint"
    page.time_uncertainty_s = 86400
    job.deps.oxylabs.scrape_facts = lambda url, deadline=None: PageFacts(
        url=url,
        final_url=url,
        claims=["Actual page text"],
        headline="Actual title",
    )
    job._safe_verify_page(page)
    assert page.claims == ["Actual page text"]
    assert page.headline == "Actual title"
    assert page.time is page.time_source is page.time_uncertainty_s is None
    assert job.result.receipts[-1].endpoint == "/scrape"


def test_failed_source_fetch_does_not_upgrade_search_evidence():
    job = lookup()
    rid, page = source(job, "/search")
    job.deps.oxylabs.scrape_facts = lambda url, deadline=None: PageFacts(
        url=url,
        final_url=url,
        http_status=403,
        claims=["Blocked page"],
    )
    job._safe_verify_page(page)
    assert page.receipt_id == rid
    assert "not verification" in job.result.notes[-1]
    assert guard(draft([rid]), job.result, model="test").claim_checks[0].finding == "unverified"


def test_coverage_query_keeps_the_claim_not_only_the_caption_opening():
    job = lookup()
    job._subject_text = (
        "Senator Bernie Sanders sitting alone on the steps of the U.S. Capitol in Washington, "
        "D.C. after his amendment to cut Medicare drug prices by 50% was defeated 99-1, 2022."
    )
    job._coverage()
    queries = [call[1] for call in job.deps.oxylabs.calls if call[0] == "search"]
    assert any("Medicare" in query and "99-1" in query for query in queries)
    assert any("Medicare" not in query and "Capitol" in query for query in queries)


def test_coverage_prefers_linked_article_title_over_commentary():
    job = lookup()
    job._subject_text = "This is my commentary about the article being shared."
    job._card_title = "Original study reports its results"
    job._coverage()
    queries = [call[1] for call in job.deps.oxylabs.calls if call[0] == "search"]
    assert job._card_title in queries


def test_zero_budget_never_reads_source_pages():
    job = lookup()
    _, page = source(job, "/search")
    job.deadline = 0
    job._safe_verify_page(page)
    assert job.deps.oxylabs.calls == []
    assert "time budget" in job.result.notes[-1]


def test_source_read_budget_is_shared_across_pipeline_phases():
    job = lookup()
    job._subject_text = CLAIM
    job.deps.source_pages_max = 1
    _, page = source(job, "/search")
    job.deps.oxylabs.scrape_facts = lambda url, deadline=None: PageFacts(
        url=url, final_url=url, claims=[CLAIM]
    )
    job._queue_older_pages()
    job.add_page(Page(id="", url="https://another.example/story"))
    job._queue_older_pages()
    job._join_verify()
    assert job._v_pages == [page.id]
    assert len([r for r in job.result.receipts if r.endpoint == "/scrape"]) == 1
    assert page.id in job._step("verify").found


def test_unavailable_source_cannot_verify_a_claim():
    job = lookup()
    rid, page = source(job, "/search")
    job.deps.oxylabs.scrape_facts = lambda url, deadline=None: PageFacts(
        url=url, final_url=url, unavailable="deleted", claims=[CLAIM]
    )
    job._safe_verify_page(page)
    assert page.receipt_id == rid
    assert guard(draft([rid]), job.result, model="test").claim_checks[0].finding == "unverified"


@pytest.mark.parametrize("claim_search_fails", [False, True])
def test_broad_search_preserves_photo_source_alongside_claim_search(claim_search_fails):
    job = lookup()
    job._subject_text = PHOTO_CAPTION
    concurrent = threading.Barrier(2)

    def search(query, limit):
        concurrent.wait(timeout=2)
        if "Medicare" in query:
            if claim_search_fails:
                raise RuntimeError("claim search unavailable")
            return [SearchHit(url="https://reddit.com/r/example/", position=1)], {}
        return [
            SearchHit(url=ARTICLE_URL, title="Photo credited to its photographer", position=1)
        ], {}

    job._search = search
    job._coverage()
    assert any(page.url == ARTICLE_URL for page in job.result.pages)
    assert job._step("coverage").state == "done"
    assert len(job.result.receipts) == (1 if claim_search_fails else 2)


def test_source_priority_keeps_historical_reporting_ahead_of_reposts():
    job = lookup()
    for url, year in (
        ("https://www.reddit.com/r/example/", 2020),
        ("https://www.youtube.com/watch?v=example", 2020),
        ("https://instagram.com/p/example/", 2020),
        ("https://recent.example/repost", 2026),
        (ARTICLE_URL, 2022),
        ("https://another.example/report", 2022),
    ):
        job.add_page(
            Page(
                id="",
                url=url,
                role="coverage",
                time=datetime(year, 8, 8, tzinfo=UTC),
                time_source="search_hint",
            )
        )
    selected = job._source_page_candidates()
    assert [page.url for page in selected] == [
        ARTICLE_URL,
        "https://another.example/report",
        "https://recent.example/repost",
    ]


def test_source_budget_waits_for_coverage_instead_of_spending_on_copy_search():
    job = Lookup("lk_sources", LookupInput(text=PHOTO_CAPTION), make_deps())
    job.deps.source_pages_max = 1
    copied = threading.Event()

    def copies():
        job.add_page(Page(id="", url="https://reddit.com/r/example/"))
        copied.set()

    def coverage():
        assert copied.wait(timeout=2)
        assert not job._v_pages
        job.add_page(Page(id="", url=ARTICLE_URL))

    job._copies_web = copies
    job._coverage = coverage
    job.run()
    chosen = [page.url for page in job.result.pages if page.id in job._v_pages]
    assert chosen == [ARTICLE_URL]


@pytest.mark.parametrize("evidence", ["read", "snippet", "failed", "submitted", "invented"])
def test_media_attribution_requires_its_own_read_source_citations(evidence):
    job = lookup()
    rid, page = source(
        job, "/search" if evidence == "snippet" else "/scrape", ok=evidence != "failed"
    )
    job.result.posts.append(
        Post(
            id="submitted",
            platform="x",
            media_urls=["https://images.example/photo.jpg"],
        )
    )
    job.result.subject_id = page.id if evidence == "submitted" else "submitted"
    if evidence == "submitted":
        job.result.posts[0].id = page.id
    result_draft = draft([])
    result_draft.claim_checks = []
    result_draft.media_provenance = "The article credits a named photographer and a 2022 date."
    result_draft.media_provenance_cites = [999 if evidence == "invented" else rid]
    result = guard(result_draft, job.result, model="test")
    if evidence == "read":
        assert result_draft.media_provenance in result.media_provenance
        assert "not an automated match" in result.media_provenance
        assert result.media_provenance_cites == [rid]
    else:
        assert result_draft.media_provenance not in result.media_provenance
        assert result.media_provenance_cites == []
    assert result.llm_reading == "unclear"
