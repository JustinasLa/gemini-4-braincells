"""GET /api/feed.atom against a fake store (no database)."""

from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from api.deps import get_store
from api.main import app
from api.schemas import ClusterSummary, SignalBrief, VerdictBrief

T0 = datetime(2026, 11, 13, 20, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 13, 20, 10, tzinfo=UTC)
NS = {"a": "http://www.w3.org/2005/Atom"}


def cluster(cid: int, **kw) -> ClusterSummary:
    base = dict(
        id=cid,
        status="coordinated",
        score=0.91,
        p_value=0.002,
        size=14,
        families_fired=3,
        window_kind="10m",
        window_start=T0,
        window_end=T1,
        updated_at=T1,
    )
    return ClusterSummary(**(base | kw))


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def list_clusters(self, **kw):
        self.calls.append(kw)
        return self.rows


@pytest.fixture
def make_client():
    def make(rows):
        store = FakeStore(rows)
        app.dependency_overrides[get_store] = lambda: store
        return TestClient(app), store

    yield make
    app.dependency_overrides.clear()


def parse(res) -> ET.Element:
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("application/atom+xml")
    return ET.fromstring(res.content)


def test_defaults_are_real_coordinated_clusters(make_client):
    c, store = make_client([])
    feed = parse(c.get("/api/feed.atom"))
    assert store.calls == [
        {"status": "coordinated", "min_score": 0.0, "limit": 50, "include_synthetic": False}
    ]
    # Atom requires id, title, updated on the feed even when it has no entries.
    assert feed.find("a:id", NS).text.endswith("feed/coordinated")
    assert feed.find("a:title", NS).text == "Shill or Chill: Shill verdicts"
    assert feed.find("a:updated", NS).text.endswith("Z")
    assert feed.findall("a:entry", NS) == []


def test_filters_pass_through(make_client):
    c, store = make_client([])
    q = {"status": "insufficient", "min_score": 0.5, "limit": 5, "include_synthetic": "true"}
    parse(c.get("/api/feed.atom", params=q))
    assert store.calls == [
        {"status": "insufficient", "min_score": 0.5, "limit": 5, "include_synthetic": True}
    ]


def test_rejects_bad_params(make_client):
    c, _ = make_client([])
    assert c.get("/api/feed.atom", params={"status": "shill"}).status_code == 422
    assert c.get("/api/feed.atom", params={"limit": 0}).status_code == 422
    assert c.get("/api/feed.atom", params={"min_score": 2}).status_code == 422


def test_entry_links_to_dashboard_and_explains_the_call(make_client):
    rows = [
        cluster(
            7,
            top_signals=[
                SignalBrief(family="content", summary="14 accounts posted the same link"),
                SignalBrief(family="timing", summary=None),
            ],
            verdict=VerdictBrief(
                status="coordinated",
                confidence=0.8,
                summary="Fresh accounts pushed one link within seconds.",
                created_at=T1,
                llm_assessment="insufficient",
                agrees_with_detector=False,
            ),
        )
    ]
    c, _ = make_client(rows)
    feed = parse(
        c.get(
            "/api/feed.atom",
            headers={"x-forwarded-proto": "https", "x-forwarded-host": "radar.example"},
        )
    )
    self_link = feed.find("a:link[@rel='self']", NS).get("href")
    assert self_link == "https://radar.example/api/feed.atom"
    assert feed.find("a:updated", NS).text == "2026-11-13T20:10:00Z"

    (entry,) = feed.findall("a:entry", NS)
    assert entry.find("a:id", NS).text.endswith("cluster/7")
    assert entry.find("a:title", NS).text == (
        "Coordinated: 14 accounts acting together, 3 signal families (cluster #7)"
    )
    link = entry.find("a:link[@rel='alternate']", NS).get("href")
    assert link == "https://radar.example/?cluster=7"
    assert entry.find("a:category", NS).get("term") == "coordinated"
    text = entry.find("a:summary", NS).text
    assert text.startswith("Fresh accounts pushed one link within seconds.")
    assert "- 14 accounts posted the same link" in text
    assert "coordination score 0.91; timing p-value 0.002; 10m window" in text
    assert "Gemini reads it differently (Can’t tell); the detector's call stands." in text
    assert "not proof of who is behind it" in text


def test_text_is_escaped_and_control_chars_dropped(make_client):
    nasty = '<script>alert(1)</script> & \x00\x07 "quoted"'
    c, _ = make_client([cluster(3, top_signals=[SignalBrief(summary=nasty)])])
    res = c.get("/api/feed.atom")
    assert b"<script>" not in res.content
    feed = parse(res)  # still well-formed XML
    text = feed.find("a:entry/a:summary", NS).text
    assert '- <script>alert(1)</script> &  "quoted"' in text


def test_falls_back_to_request_host(make_client):
    c, _ = make_client([cluster(1, updated_at=None)])
    feed = parse(c.get("/api/feed.atom?limit=1"))
    assert feed.find("a:link[@rel='self']", NS).get("href") == (
        "http://testserver/api/feed.atom?limit=1"
    )
    entry = feed.find("a:entry", NS)
    assert entry.find("a:link", NS).get("href") == "http://testserver/?cluster=1"
    # no updated_at: the window end stands in
    assert entry.find("a:updated", NS).text == "2026-11-13T20:10:00Z"


def test_later_verdict_moves_entry_time(make_client):
    T2 = datetime(2026, 11, 13, 20, 30, tzinfo=UTC)
    v = VerdictBrief(status="coordinated", confidence=0.8, summary="s", created_at=T2)
    c, _ = make_client([cluster(4, verdict=v)])
    feed = parse(c.get("/api/feed.atom"))
    assert feed.find("a:entry/a:updated", NS).text == "2026-11-13T20:30:00Z"
    assert feed.find("a:updated", NS).text == "2026-11-13T20:30:00Z"


def test_verdict_for_an_earlier_call_is_not_shown_as_current(make_client):
    # Gemini reviewed while the detector said insufficient and disagreed (said coordinated);
    # the detector has since moved to coordinated.
    v = VerdictBrief(
        status="insufficient",
        confidence=0.5,
        summary="Not enough to call it.",
        created_at=T1,
        llm_assessment="coordinated",
        agrees_with_detector=False,
    )
    c, _ = make_client([cluster(5, verdict=v)])
    text = parse(c.get("/api/feed.atom")).find("a:entry/a:summary", NS).text
    assert "Not enough to call it." not in text
    assert "reads it differently" not in text
    assert "Gemini reviewed this cluster before the detector's call changed." in text


def test_older_verdict_without_agree_flag_follows_dashboard_rule(make_client):
    # Before agrees_with_detector existed: the dashboard compares llm_assessment to the status.
    v = VerdictBrief(
        status="coordinated", confidence=0.5, created_at=T1, llm_assessment="insufficient"
    )
    c, _ = make_client([cluster(6, verdict=v)])
    text = parse(c.get("/api/feed.atom")).find("a:entry/a:summary", NS).text
    assert "Gemini reads it differently (Can’t tell); the detector's call stands." in text


def test_window_text_is_utc_and_dates_a_window_across_midnight(make_client):
    from datetime import timedelta, timezone

    helsinki = timezone(timedelta(hours=2))
    start = datetime(2026, 11, 14, 1, 30, tzinfo=helsinki)  # 23:30 UTC on the 13th
    end = datetime(2026, 11, 14, 2, 30, tzinfo=helsinki)  # 00:30 UTC on the 14th
    c, _ = make_client([cluster(8, window_start=start, window_end=end, updated_at=None)])
    res = c.get("/api/feed.atom")
    entry = parse(res).find("a:entry", NS)
    assert "2026-11-13 23:30–2026-11-14 00:30 UTC" in entry.find("a:summary", NS).text
    assert entry.find("a:published", NS).text == "2026-11-14T00:30:00Z"
    assert "X-Forwarded-Host" in res.headers["vary"]


def test_feed_title_per_status(make_client):
    c, _ = make_client([])
    feed = parse(c.get("/api/feed.atom", params={"status": "insufficient"}))
    assert feed.find("a:title", NS).text == "Noise Radar: new can’t tell clusters"
