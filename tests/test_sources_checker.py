from datetime import date

from sources.checker import SourceChecker
from sources.links import checkable_url, claim_phrase, top_links
from sources.models import ClusterPost, PageFacts, SearchHit
from sources.oxylabs import OxylabsError

TEXT = "Breaking: the city water supply was poisoned last night, officials are hiding it! #wake"


class FakeClient:
    def __init__(self, *, fail_scrape: bool = False):
        self.fail_scrape = fail_scrape
        self.searches: list[str] = []
        self.scrapes: list[str] = []

    def search(self, query, max_results=10):
        self.searches.append(query)
        return [
            SearchHit(
                url="https://a.example/1", title="A", position=1, published_hint=date(2026, 11, 12)
            ),
            SearchHit(
                url="https://b.example/2", title="B", position=2, published_hint=date(2026, 11, 10)
            ),
            SearchHit(url="https://b.example/3", title="C", position=3),
        ]

    def scrape_facts(self, url):
        self.scrapes.append(url)
        if self.fail_scrape:
            raise OxylabsError("boom", status=500)
        return PageFacts(
            url=url,
            final_url=url,
            http_status=200,
            publisher="Example News",
            headline="Water supply poisoned",
            claims=["The supply was poisoned."],
        )


def posts(n=4):
    return [
        ClusterPost(
            did=f"did:plc:{i}",
            text=TEXT,
            embed_urls=[
                "https://www.news.example/story?utm_source=x",
                "https://bsky.app/profile/x",
            ],
        )
        for i in range(n)
    ]


def test_checkable_url_uses_ingest_canonical_form():
    assert (
        checkable_url("http://WWW.News.Example/a/?utm_medium=x&id=3#top")
        == "https://news.example/a?id=3"
    )
    assert checkable_url("https://bsky.app/profile/x") is None
    assert checkable_url("https://cdn.example/img.JPG") is None
    assert checkable_url("ftp://x.example/") is None
    assert checkable_url("http://127.0.0.1:8000/") is None


def test_top_links_counts_distinct_accounts():
    ps = posts(3) + [ClusterPost(did="did:plc:0", embed_urls=["https://other.example/"])]
    assert top_links(ps, 5) == [("https://news.example/story", 3), ("https://other.example", 1)]


def test_claim_phrase_picks_longest_sentence_of_most_common_text():
    phrase = claim_phrase([TEXT, TEXT, "something else entirely here today ok"])
    assert phrase == "Breaking: the city water supply was poisoned last night, officials are hiding"
    assert claim_phrase(["too short!"]) is None
    assert claim_phrase([None, ""]) is None


def test_check_builds_page_and_search_evidence_with_urls():
    client = FakeClient()
    items = SourceChecker(client).check(posts())

    kinds = [(i.detail["type"], i.detail.get("purpose")) for i in items]
    assert kinds == [("linked_page", None), ("web_search", "headline"), ("web_search", "post_text")]
    page = items[0]
    assert page.source_url == "https://news.example/story"
    assert page.detail["shared_by_accounts"] == 4 and page.detail["cluster_accounts"] == 4
    text_search = items[2]
    assert text_search.detail["earliest_dated"]["url"] == "https://b.example/2"
    assert text_search.source_url == "https://b.example/2"
    assert "not proof of origin" in text_search.detail["note"]
    assert all(i.source_url for i in items)


def test_check_skips_stored_evidence_and_caches_calls():
    client = FakeClient()
    checker = SourceChecker(client)
    first = checker.check(posts())
    again = checker.check(posts(), stored={i.detail["key"]: i.detail for i in first})
    assert again == []
    assert len(client.scrapes) == 1  # page key stored -> no second scrape


def test_headline_search_is_retried_when_only_the_page_was_stored():
    client = FakeClient()
    first = SourceChecker(client).check(posts())
    page_only = {i.detail["key"]: i.detail for i in first if i.detail["type"] == "linked_page"}
    again = SourceChecker(client).check(posts(), stored=page_only)
    assert [i.detail.get("purpose") for i in again] == ["headline", "post_text"]
    assert len(client.scrapes) == 1


def test_failed_calls_are_not_retried_within_the_failure_ttl():
    client = FakeClient(fail_scrape=True)
    checker = SourceChecker(client)
    checker.check(posts())
    checker.check(posts())
    assert len(client.scrapes) == 1


def test_failed_scrape_is_skipped_not_reported_as_evidence():
    items = SourceChecker(FakeClient(fail_scrape=True)).check(posts())
    assert [i.detail["purpose"] for i in items] == ["post_text"]


def test_no_search_results_is_still_evidence():
    class Empty(FakeClient):
        def search(self, query, max_results=10):
            return []

    items = SourceChecker(Empty()).check([ClusterPost(did="d", text=TEXT)])
    assert len(items) == 1
    assert "no web search results" in items[0].summary
    assert items[0].source_url is None
