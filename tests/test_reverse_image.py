import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sources.oxylabs import OxylabsError
from sources.reverse_image import (
    ReverseImageError,
    parse_tineye,
    parse_yandex,
    parse_yandex_sites,
    search_tineye,
    search_yandex,
    tineye_url,
    yandex_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "reverse_image"
DISASTER = "https://upload.wikimedia.org/wikipedia/en/1/11/Disaster_Girl.jpg"
IMGFLIP = "https://i.imgflip.com/233stg.jpg"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_request_urls_encode_the_entire_image_url():
    image = "https://example.com/a b.jpg?x=1&copy=é#part"
    encoded = "https%3A%2F%2Fexample.com%2Fa%20b.jpg%3Fx%3D1%26copy%3D%C3%A9%23part"
    assert tineye_url(image) == (
        f"https://tineye.com/api/v1/result_json/?url={encoded}&sort=crawl_date&order=asc"
    )
    assert yandex_url(image) == f"https://yandex.com/images/search?rpt=imageview&url={encoded}"
    assert yandex_url(image, sites=True) == yandex_url(image) + "&cbir_page=sites"


@pytest.mark.parametrize(
    ("name", "query", "total"), [("disaster", DISASTER, 17200), ("imgflip", IMGFLIP, 16540)]
)
def test_tineye_real_fixtures_preserve_engine_order(name, query, total):
    result = parse_tineye(fixture(f"tineye_{name}.json"), query)
    assert result.engine == "tineye"
    assert result.query_image_url == query
    assert result.request_url == tineye_url(query)
    assert result.total == total
    assert result.earliest == datetime(2008, 4, 22, tzinfo=UTC)
    assert len(result.hits) == 10
    assert [hit.rank for hit in result.hits] == list(range(10))
    first = result.hits[0]
    assert first.image_url == DISASTER
    assert first.page_url == "https://en.wikipedia.org/wiki/Disaster_Girl"
    assert first.domain == "en.wikipedia.org"
    assert first.first_seen == datetime(2022, 7, 30, tzinfo=UTC)
    assert first.title == "Disaster_Girl.jpg"
    assert (first.width, first.height) == (388, 257)
    zooomr = result.hits[1]
    assert zooomr.image_url == "http://static.zooomr.com/images/592360_8ca5f7f005_s.jpg"
    assert zooomr.page_url == "http://www.zooomr.com/people/americangirl/"
    assert zooomr.domain == "zooomr.com"
    assert zooomr.first_seen == datetime(2008, 4, 22, tzinfo=UTC)
    assert zooomr.title == "592360_8ca5f7f005_s.jpg"
    assert (zooomr.width, zooomr.height) == (75, 75)
    assert all(hit.time_source == "crawl_date" and hit.view == "matches" for hit in result.hits)
    assert all(hit.thumb_url.startswith("https://img.tineye.com/result/") for hit in result.hits)


def test_tineye_chooses_earliest_backlink_not_first_or_match_summary():
    data = json.loads(fixture("tineye_disaster.json"))
    match = data["matches"][1]
    match["backlinks"].reverse()
    match["first_crawl_date"] = "2025-01-01"
    match["backlinks"][-1]["url"] = "https://copy.example/earliest.jpg"
    match["backlinks"][-1]["backlink"] = "https://www.oldest.example/post"
    match["backlinks"][-1]["image_name"] = "earliest.jpg"
    hit = parse_tineye(json.dumps(data)).hits[1]
    assert hit.first_seen == datetime(2008, 4, 22, tzinfo=UTC)
    assert hit.image_url == "https://copy.example/earliest.jpg"
    assert hit.page_url == "https://www.oldest.example/post"
    assert hit.domain == "oldest.example"
    assert hit.title == "earliest.jpg"


@pytest.mark.parametrize("fence", ["", "json", "JSON"])
def test_tineye_accepts_fenced_json(fence):
    raw = fixture("tineye_disaster.json")
    assert len(parse_tineye(f" \n```{fence}\n{raw}\n```\n ").hits) == 10


def test_tineye_empty_matches_are_valid():
    result = parse_tineye(' \n {"matches": [], "num_matches": 0} \n')
    assert result.hits == []
    assert result.total == 0
    assert result.earliest is None


@pytest.mark.parametrize(
    "raw", ["garbage", "<html>blocked</html>", "{}", "[]", "null", '{"matches": null}']
)
def test_tineye_rejects_non_results(raw):
    with pytest.raises(ReverseImageError):
        parse_tineye(raw)


def test_tineye_missing_page_and_date_do_not_invent_provenance():
    data = {"matches": [{"backlinks": [{"url": "https://www.copy.example/image.jpg"}]}]}
    hit = parse_tineye(json.dumps(data)).hits[0]
    assert hit.domain == "copy.example"
    assert hit.page_url is None
    assert hit.first_seen is None
    assert hit.time_source is None


@pytest.mark.parametrize("name", ["disaster", "imgflip"])
def test_yandex_real_other_sizes(name):
    result = parse_yandex(fixture(f"yandex_{name}.md"), IMGFLIP)
    assert result.engine == "yandex"
    assert result.query_image_url == IMGFLIP
    assert len(result.hits) == 18
    assert all(hit.view == "other_sizes" for hit in result.hits)
    assert [hit.rank for hit in result.hits] == list(range(18))
    hit = next(hit for hit in result.hits if hit.image_url == IMGFLIP)
    assert (hit.width, hit.height) == (666, 500)
    assert hit.domain == "i.imgflip.com"
    assert hit.page_url is None
    assert hit.thumb_url is None
    assert all(hit.first_seen is None and hit.time_source is None for hit in result.hits)
    assert result.total is None
    assert result.earliest is None


def test_yandex_similar_images_thumbnails_deduplication_and_navigation():
    markdown = """
![Loaded image](https://avatars.mds.yandex.net/get-images-cbir/123/query/orig)
## In other sizes
[640×480](https://copy.example/image.jpg)
[800×600](//copy.example/second.jpg)
## [Similar images](/images/search?cbir_page=similar)
[![A copy](//avatars.mds.yandex.net/i?id=thumb&n=13) 320×240](https://copy.example/a_(copy).jpg)
[640×480](https://copy.example/image.jpg)
[Another copy](https://copy.example/no-size.jpg)
[More](/images/search?cbir_page=similar)
[Login](https://passport.yandex.com/auth)
[Images](//yandex.ru/images?source=tabbar)
[Web](https://yandex.com.tr/?source=tabbar)
![Query](https://avatars.mds.yandex.net/get-images-cbir/123/query/orig)
[Bad](javascript:alert(1)) [Bad](ftp://copy.example/image.jpg)
[Local](/copy.jpg) [Empty]()
## Sites
[Not an image](https://page.example/post)
"""
    hits = parse_yandex(markdown).hits
    assert [hit.image_url for hit in hits] == [
        "https://copy.example/image.jpg",
        "https://copy.example/second.jpg",
        "https://copy.example/a_(copy).jpg",
        "https://copy.example/no-size.jpg",
    ]
    assert [hit.view for hit in hits] == ["other_sizes", "other_sizes", "similar", "similar"]
    assert hits[2].thumb_url == "https://avatars.mds.yandex.net/i?id=thumb&n=13"
    assert (hits[2].width, hits[2].height) == (320, 240)
    assert (hits[3].width, hits[3].height) == (None, None)


@pytest.mark.parametrize("name", ["disaster", "imgflip"])
def test_yandex_sites_real_fixtures(name):
    result = parse_yandex_sites(fixture(f"yandex_sites_{name}.md"), IMGFLIP)
    assert len(result.hits) == 20
    assert result.request_url == yandex_url(IMGFLIP, sites=True)
    assert result.query_image_url == IMGFLIP
    assert [hit.rank for hit in result.hits] == list(range(20))
    assert all(hit.view == "sites" for hit in result.hits)
    assert all(hit.first_seen is None and hit.time_source is None for hit in result.hits)
    assert all("utm_" not in hit.page_url for hit in result.hits)
    assert any(hit.thumb_url is None for hit in result.hits)
    first = result.hits[0]
    assert first.thumb_url.startswith("https://avatars.mds.yandex.net/i?id=")
    if name == "disaster":
        assert first.image_url == (
            "https://d279m997dpfwgl.cloudfront.net/wp/2021/10/Disaster-Girl-OG-pic.jpeg"
        )
        assert first.page_url == (
            "https://podtail.com/tr/podcast/endless-thread/memes-bonus-zoe-didn-t-start-the-fire/"
        )
        assert first.domain == "podtail.com"
        assert (first.width, first.height) == (1488, 991)
        assert first.title == (
            "MEMES, Bonus: Zoë Didn't Start the Fire - Endless Thread - Podcast - Podtail"
        )
    else:
        assert first.image_url == "https://i.imgflip.com/6y1949.jpg"
        assert first.page_url == "https://imgflip.com/m/fun?sort=latest&after=6y1caf"
        assert first.domain == "imgflip.com"
        assert first.title == "fun Memes & GIFs - Imgflip"
        assert (first.width, first.height) == (577, 433)
        assert result.hits[1].image_url == first.image_url
        assert result.hits[1].page_url == "https://imgflip.com/i/6y1949"
        assert result.hits[2].page_url == "https://imgflip.com/tag/disaster%2Bgirl?page=279"


def test_yandex_sites_missing_thumb_and_encoded_page_parameters():
    markdown = """
## Sites containing information about the image
* [![Copy (original)]() 12×34](//copy.example/full.jpg)

  [A [nested] title](https://www.page.example/a_(b)?q=a%20b&keep=&utm_source=yandexsmartcamera&utm_medium=organic#part)
"""
    hit = parse_yandex_sites(markdown).hits[0]
    assert hit.image_url == "https://copy.example/full.jpg"
    assert hit.thumb_url is None
    assert hit.page_url == "https://www.page.example/a_(b)?q=a%20b&keep=#part"
    assert hit.title == "A [nested] title"
    assert hit.domain == "page.example"
    assert (hit.width, hit.height) == (12, 34)


@pytest.mark.parametrize("parser", [parse_yandex, parse_yandex_sites])
def test_yandex_captcha_is_not_an_empty_success(parser):
    with pytest.raises(ReverseImageError):
        parser("# Are you a robot?\nPlease confirm you're not a robot.\n[Continue](/captcha)")


@pytest.mark.parametrize(
    ("parser", "heading"),
    [
        (parse_yandex, "In other sizes"),
        (parse_yandex, "Similar images"),
        (parse_yandex_sites, "Sites containing information about the image"),
    ],
)
def test_yandex_empty_expected_sections_are_valid(parser, heading):
    assert parser(f"## {heading}\nNo results").hits == []


class FakeClient:
    def __init__(self, markdown="", error=None):
        self.markdown = markdown
        self.error = error
        self.requests = []
        self.last_call = {
            "endpoint": "/scrape",
            "status": 200,
            "latency_ms": 123,
            "request_id": "req",
        }

    def scrape_markdown(self, url, *, deadline=None):
        self.requests.append((url, deadline))
        if self.error:
            raise self.error
        return self.markdown, {"url": "https://redirect.example/", "status": 200}


@pytest.mark.parametrize(
    ("search", "kwargs", "name", "expected_url", "count"),
    [
        (search_tineye, {}, "tineye_imgflip.json", tineye_url(IMGFLIP), 10),
        (search_yandex, {}, "yandex_imgflip.md", yandex_url(IMGFLIP), 18),
        (
            search_yandex,
            {"sites": True},
            "yandex_sites_imgflip.md",
            yandex_url(IMGFLIP, sites=True),
            20,
        ),
    ],
)
@pytest.mark.parametrize("deadline", [None, 1234.5])
def test_search_propagates_deadline_and_copies_receipt(
    search, kwargs, name, expected_url, count, deadline
):
    client = FakeClient(fixture(name))
    result = search(client, IMGFLIP, deadline=deadline, **kwargs)
    assert client.requests == [(expected_url, deadline)]
    assert result.query_image_url == IMGFLIP
    assert result.request_url == expected_url
    assert len(result.hits) == count
    assert result.call == client.last_call
    assert result.call is not client.last_call
    client.last_call["status"] = 500
    assert result.call["status"] == 200


@pytest.mark.parametrize(
    ("search", "kwargs"),
    [(search_tineye, {}), (search_yandex, {}), (search_yandex, {"sites": True})],
)
def test_search_preserves_oxylabs_errors(search, kwargs):
    error = OxylabsError("blocked", status=403, request_id="blocked-req")
    client = FakeClient(error=error)
    with pytest.raises(OxylabsError) as caught:
        search(client, IMGFLIP, **kwargs)
    assert caught.value is error
