from datetime import date
from pathlib import Path

import pytest

from lookup.xpage import unavailable_reason
from sources.checker import SourceChecker
from sources.links import checkable_url, claim_phrase, top_links, url_variants
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


def test_claim_phrase_for_unspaced_scripts():
    ja = "今日は東京で大雨が降っていて電車が止まっています。みんな気をつけてください。"
    assert (
        claim_phrase([ja]) == "今日は東京で大雨が降っていて電車が止まっています"
    )  # no 。: copies differ
    assert claim_phrase(["おはようございます！"]) is None
    th = "วันนี้ฝนตกหนักมากที่กรุงเทพ รถติดทุกถนน ทุกคนระวังด้วยนะ"
    assert claim_phrase([th]) == "วันนี้ฝนตกหนักมากที่กรุงเทพ"
    assert claim_phrase(["สวัสดีครับ"]) is None
    long_ja = "あいうえお" * 10
    assert claim_phrase([long_ja]) == "あいうえお" * 6


def test_claim_phrase_unspaced_review_cases():
    # Never one short Latin word as the whole phrase (it would match unrelated clusters).
    phrase = claim_phrase(["iPhone 15を買ったら画面がすぐ割れてしまって本当にショックでした"])
    assert phrase.startswith("iPhone 15を買ったら") and len(phrase) == 30
    th = claim_phrase(["วันนี้ ฝนตกหนักมากที่กรุงเทพรถติดทุกถนนทุกคนระวังด้วยนะ"])
    assert th.startswith("วันนี้ ฝนตก") and len(th) >= 10
    # A mostly-English sentence with a CJK word is still measured in words (was None).
    mixed = "Officials in Tokyo (東京) say the dam failed and residents must evacuate now"
    assert claim_phrase([mixed]) == (
        "Officials in Tokyo (東京) say the dam failed and residents must evacuate"
    )
    # Half-width ! splits sentences; half-width katakana counts as unspaced.
    assert claim_phrase(["你好!我们今天在北京看到了很多人在街上抗议政府的新政策"]).startswith(
        "我们"
    )
    assert claim_phrase(["ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿ"]) == "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿ"
    # Filler is never the phrase: divider lines, leading emoji or digits.
    notice = (
        "【お知らせ】\nーーーーーーーーーーーーーーーーーーーー\n明日のライブは中止になりました。"
    )
    assert claim_phrase([notice]) == "明日のライブは中止になりました"
    assert claim_phrase(["🔥" * 40 + " 東京で火災が発生しました原因は調査中です"]) == (
        "東京で火災が発生しました原因は調査中です"
    )
    assert claim_phrase(["ーーーーーーーーーーーーーーーー"]) is None
    assert claim_phrase(["ー" * 32 + " 東京で火災が発生しました原因は調査中"]) == (
        "東京で火災が発生しました原因は調査中"
    )
    assert claim_phrase(["草" * 12]) is None and claim_phrase(["う" + "お" * 20]) is None


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


def test_call_cache_is_bounded_and_drops_the_oldest_entry():
    # The API keeps one checker for the process, fed by every /api/analyze input.
    client = FakeClient()
    checker = SourceChecker(client, max_cached=2)
    for text in ("one two three four five", "six seven eight nine ten", "a b c d e f"):
        checker.check([ClusterPost(did="d", text=text)])
    assert len(checker._cache) == 2
    checker.check([ClusterPost(did="d", text="a b c d e f")])  # newest: still cached
    assert len(client.searches) == 3
    checker.check([ClusterPost(did="d", text="one two three four five")])  # evicted: fetched again
    assert len(client.searches) == 4


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


def test_cluster_summaries_keep_cluster_wording():
    items = SourceChecker(FakeClient()).check(posts())
    assert [i.summary for i in items] == [
        'Linked page shared by 4 of 4 accounts: "Water supply poisoned" (Example News).',
        "The linked headline matches 3 web results on 2 domains; "
        "earliest dated: b.example (2026-11-10).",
        "The cluster's message text matches 3 web results on 2 domains; "
        "earliest dated: b.example (2026-11-10).",
    ]


def test_adhoc_summaries_describe_the_input_without_account_counts():
    post = ClusterPost(did="adhoc", text=TEXT, embed_urls=["https://www.news.example/story"])
    items = SourceChecker(FakeClient()).check([post], adhoc=True)
    assert [i.summary for i in items] == [
        'The linked page: "Water supply poisoned" (Example News).',
        "The linked headline matches 3 web results on 2 domains; "
        "earliest dated: b.example (2026-11-10).",
        "Your text matches 3 web results on 2 domains; earliest dated: b.example (2026-11-10).",
    ]
    for i in items:
        assert "account" not in i.summary.lower() and "cluster" not in i.summary.lower()
    assert items[0].detail["shared_by_accounts"] is None
    assert items[0].detail["cluster_accounts"] is None


def test_adhoc_no_results_wording():
    class Empty(FakeClient):
        def search(self, query, max_results=10):
            return []

    items = SourceChecker(Empty()).check([ClusterPost(did="adhoc", text=TEXT)], adhoc=True)
    assert items[0].summary.startswith('Your text returned no web search results for "')


@pytest.mark.parametrize("adhoc", [False, True])
@pytest.mark.parametrize("reason", ["suspended", "protected", "unavailable", "not_found"])
def test_tombstone_checks_skip_headline_and_only_search_suspended_handle(reason, adhoc):
    class TombstoneClient(FakeClient):
        def scrape_facts(self, url):
            self.scrapes.append(url)
            md_path = next(
                (Path(__file__).parent / "fixtures/xpages").glob(f"post_{reason}_*.md"), None
            )
            code = unavailable_reason(md_path.read_text()) if md_path else "not_found"
            return PageFacts(
                url=url,
                final_url=url,
                headline="We're unable to show this content",
                unavailable=code,
            )

    client = TombstoneClient()
    checker = SourceChecker(client)
    post = ClusterPost(did="adhoc", embed_urls=["https://x.com/TEN_GOP/status/788098438915297280"])
    items = checker.check([post], adhoc=adhoc)
    page = items[0]
    assert page.detail["unavailable"] == reason
    assert "headline" not in [i.detail.get("purpose") for i in items]
    if reason == "suspended":
        assert client.searches == ['"TEN_GOP"']
        assert items[1].detail["key"] == "search:handle:ten_gop"
        assert items[1].detail["purpose"] == "handle"
        assert items[1].summary.startswith(
            "The suspended account @TEN_GOP matches 3 web results on 2 domains"
        )
    else:
        assert client.searches == []
        assert len(items) == 1
    if reason == "unavailable":
        assert "it may be private, deleted or app-only" in page.summary
        assert "deleted by" not in page.summary
    if adhoc:
        assert page.summary.startswith("The linked X post: X ")
        assert all("1 of 1" not in i.summary and "cluster" not in i.summary for i in items)
        assert page.detail["shared_by_accounts"] is page.detail["cluster_accounts"] is None
    else:
        assert page.summary.startswith("Linked X post shared by 1 of 1 accounts: X ")
    stored = {i.detail["key"]: i.detail for i in items}
    assert checker.check([post], stored=stored, adhoc=adhoc) == []
    assert len(client.scrapes) == 1
    # A stored page must suppress the bogus headline even if its search isn't stored yet.
    fresh_client = TombstoneClient()
    retried = SourceChecker(fresh_client).check([post], stored={page.detail["key"]: page.detail})
    assert not fresh_client.scrapes
    assert [i.detail["purpose"] for i in retried] == (["handle"] if reason == "suspended" else [])


@pytest.mark.parametrize("headline", ["Post", "X", "Facebook", "Account suspended", "Two words"])
def test_generic_short_headline_is_not_searched(headline):
    class ShortHeadline(FakeClient):
        def scrape_facts(self, url):
            return PageFacts(url=url, final_url=url, headline=headline)

    client = ShortHeadline()
    items = SourceChecker(client).check([ClusterPost(did="d", embed_urls=["https://example.com"])])
    assert len(items) == 1
    assert client.searches == []


def test_suspended_account_search_is_deduplicated_across_links():
    class Suspended(FakeClient):
        def scrape_facts(self, url):
            return PageFacts(url=url, final_url=url, unavailable="suspended", headline="Post")

    client = Suspended()
    items = SourceChecker(client).check(
        [
            ClusterPost(
                did="d",
                embed_urls=[
                    "https://x.com/TEN_GOP/status/1",
                    "https://x.com/ten_gop/status/2",
                ],
            )
        ]
    )
    assert client.searches == ['"TEN_GOP"']
    assert len([i for i in items if i.detail.get("purpose") == "handle"]) == 1


@pytest.mark.parametrize("host", ["x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com"])
def test_url_variants_include_x_aliases_without_changing_normalization(host):
    from signals.textnorm import normalize_url

    raw = f"http://{host}/a/status/1/?utm_source=test#top"
    variants = url_variants(raw)
    assert raw in variants
    assert normalize_url(raw) == f"https://{host}/a/status/1"
    for alias in ("x.com", "twitter.com", "mobile.x.com", "mobile.twitter.com"):
        assert f"https://{alias}/a/status/1" in variants
    assert len(variants) == len(set(variants))


def test_url_variants_leave_other_hosts_alone():
    assert url_variants("http://www.example.com/a/#top") == [
        "http://www.example.com/a/#top",
        "https://example.com/a",
    ]
    assert url_variants("https://x.com.example.com/a") == ["https://x.com.example.com/a"]


def test_legacy_stored_x_tombstone_headline_is_not_searched():
    # Stored before PageFacts.unavailable existed: no "unavailable" key, X's notice as headline.
    url = "https://x.com/realDonaldTrump/status/869766994899468288"
    key = f"page:{url}"
    legacy = {"key": key, "final_url": url, "headline": "We're unable to show this content"}
    client = FakeClient()
    SourceChecker(client).check([ClusterPost(did="d", embed_urls=[url])], stored={key: legacy})
    assert client.searches == [] and client.scrapes == []


def test_legacy_stored_x_page_with_a_real_headline_is_still_searched():
    url = "https://x.com/jack/status/20"
    key = f"page:{url}"
    legacy = {"key": key, "final_url": url, "headline": 'jack on X: "just setting up my twttr"'}
    client = FakeClient()
    SourceChecker(client).check([ClusterPost(did="d", embed_urls=[url])], stored={key: legacy})
    assert client.searches == ['"jack on X: "just setting up my twttr""']


def test_suspended_account_behind_a_short_link_gets_the_handle_search():
    class Short(FakeClient):
        def scrape_facts(self, url):
            return PageFacts(
                url=url,
                final_url="https://x.com/TEN_GOP/status/788098438915297280",
                unavailable="suspended",
                headline="Post",
            )

    client = Short()
    SourceChecker(client).check([ClusterPost(did="d", embed_urls=["https://t.co/abc"])], adhoc=True)
    assert client.searches == ['"TEN_GOP"']


def test_suspended_handle_falls_back_to_the_input_when_the_final_url_has_none():
    class WebStatus(FakeClient):
        def scrape_facts(self, url):
            return PageFacts(
                url=url,
                final_url="https://x.com/i/web/status/788098438915297280",
                unavailable="suspended",
                headline="Post",
            )

    url = "https://x.com/TEN_GOP/status/788098438915297280"
    client = WebStatus()
    SourceChecker(client).check([ClusterPost(did="d", embed_urls=[url])], adhoc=True)
    assert client.searches == ['"TEN_GOP"']
    key = f"page:{url}"
    stored = {
        key: {
            "key": key,
            "final_url": "https://x.com/i/web/status/1",
            "headline": "Post",
            "unavailable": "suspended",
        }
    }
    client = FakeClient()
    SourceChecker(client).check([ClusterPost(did="d", embed_urls=[url])], stored=stored)
    assert client.searches == ['"TEN_GOP"']
