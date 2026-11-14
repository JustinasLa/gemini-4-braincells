import json
from datetime import UTC, datetime

import pytest

from lookup.assess import SYSTEM, _Draft, brief, guard
from lookup.models import ClaimCheck, LookupInput, Page, Post
from lookup.service import Lookup
from sources.models import PageFacts
from tests.test_lookup import make_deps

POST_URL = "https://x.com/Theveripost/status/2121190913804468478"
CLAIM = "Sanders's Medicare drug-pricing amendment was defeated 99–1."


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

        def assess(self, result):
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
    assert "Exact media provenance is unverified" in result.media_provenance
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
    query = next(call[1] for call in job.deps.oxylabs.calls if call[0] == "search")
    assert "Medicare" in query and "99-1" in query


def test_zero_budget_never_reads_source_pages():
    job = lookup()
    _, page = source(job, "/search")
    job.deadline = 0
    job._safe_verify_page(page)
    assert job.deps.oxylabs.calls == []
    assert "time budget" in job.result.notes[-1]
