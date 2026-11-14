"""Screenshot receipts in the media worker (media/screenshot_receipts.py): no network, no DB."""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

import media.__main__ as media_main
from lookup import receipts as lookup_receipts
from lookup.models import DiffPart, Metrics, ScreenshotReceipt
from media import screenshot_receipts as sr
from media import store
from media.nemotron import MediaAnalysis
from media.worker import ClusterMedia, Finding, MediaGroup, MediaRun

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


@dataclass
class ShotFinding(Finding):
    shot: dict | None = None

    def detail(self):
        d = super().detail()
        if self.shot is not None:
            d["screenshot"] = self.shot
        return d


def _shot(media_hash="bafyshot", handle="newsdesk", platform="x", text="The bridge is closed"):
    return {
        "media_hash": media_hash,
        "platform": platform,
        "display_name": "News Desk",
        "handle": handle,
        "shown_time": "3:14 PM · Nov 13, 2026",
        "text": text,
        "counts": {"likes": 1200, "reposts": 300},
        "ui_oddities": [],
        "confidence": 0.9,
        "model": "nemotron-3.5-super-vl",
        "latency_ms": 900,
    }


def _finding(media_hash="bafyshot", shot=True, uris=("r1", "r2")):
    posts = [
        ClusterMedia(1, f"at://did:plc:{u}/app.bsky.feed.post/{u}", f"did:plc:{u}", media_hash)
        for u in uris
    ]
    analysis = MediaAnalysis(
        media_hash=media_hash,
        description="A screenshot of a post",
        extracted_text=[],
        reuse_cues=[],
        manipulation_cues=[],
        confidence=0.8,
        model="nemotron-3.5-super-vl",
        latency_ms=5,
        media_type="screenshot" if shot else "photo",
    )
    return ShotFinding(
        MediaGroup(media_hash, posts),
        analysis,
        "image",
        f"https://cdn.bsky.app/img/{media_hash}@jpeg",
        cached=False,
        shot=_shot(media_hash) if shot else None,
    )


def _receipt(shot, label="matches", **kw):
    base = dict(
        media_hash=shot.media_hash,
        platform=shot.platform,
        handle=shot.handle,
        shown_text=shot.text,
        read_model=shot.model,
        label=label,
        counts_now=Metrics(likes=1300),
        receipt_ids=[1],
        latency_ms=1234,
    )
    if label in ("matches", "differs"):
        base.update(
            original_url="https://x.com/newsdesk/status/1",
            original_text="The bridge is open",
            time_check="consistent",
        )
    return ScreenshotReceipt(**(base | kw))


class Recorder:
    """Fake find_original: records calls, issues one receipt, optional delay."""

    def __init__(self, label="matches", delay=0.0, **kw):
        self.label, self.delay, self.kw, self.calls, self.anchors = label, delay, kw, [], []

    def __call__(self, shot, deps, budget):
        self.calls.append(shot.media_hash)
        self.anchors.append(deps.anchor)
        assert budget.left() > 0
        deps.receipt("x-id", "status", "read the post", source_url="https://x.com/i/1", ok=True)
        if self.delay:
            time.sleep(self.delay)
        return _receipt(shot, self.label, **self.kw)


def _deps(sink, anchor):
    return lookup_receipts.ReceiptDeps(receipt=sink, anchor=anchor)


@pytest.fixture
def find(monkeypatch):
    def install(fake):
        monkeypatch.setattr(lookup_receipts, "find_original", fake)
        return fake

    return install


def test_screenshot_finding_becomes_one_stable_receipt_row(find):
    fake = find(Recorder("matches"))
    rows = sr.receipt_rows([_finding()], _deps, existing={}, now=lambda: NOW)
    assert len(rows) == 1
    row = rows[0]
    d = row["detail"]
    assert d["media_hash"] == "screenshot-receipt:bafyshot"
    assert d["type"] == "screenshot_receipt" and d["source_media_hash"] == "bafyshot"
    assert d["media_url"] == "https://cdn.bsky.app/img/bafyshot@jpeg"
    assert d["post_urls"] and d["n_accounts"] == 2
    assert d["receipt"]["label"] == "matches" and d["checked_at"] == NOW.isoformat()
    assert [f["tool"] for f in d["fetches"]] == ["x-id"]
    assert d["fetches"][0]["source_url"] == "https://x.com/i/1"
    assert row["summary"] == (
        "Screenshot of @newsdesk's post on X: matches the real post "
        "(https://x.com/newsdesk/status/1)."
    )
    assert row["source_url"] == "https://x.com/newsdesk/status/1"
    assert row["model"] == "nemotron-3.5-super-vl"
    assert fake.calls == ["bafyshot"]
    assert fake.anchors == [NOW]  # no post times known: the check's own time


def test_summaries_for_each_label(find):
    shot = sr.shot_from_finding(_finding())
    differs = _receipt(shot, "differs", differences=["the words differ: 'closed' vs 'open'."])
    assert sr.receipt_summary(differs) == (
        "Screenshot of @newsdesk's post on X: differs from the real post: "
        "the words differ: 'closed' vs 'open'."
    )
    bsky = shot.__class__(**{**_shot(), "platform": "bluesky"})
    nf = _receipt(bsky, "not_found")
    assert sr.receipt_summary(nf) == (
        "Screenshot of @newsdesk's post on Bluesky: no matching post found: it may be deleted, "
        "private or never posted."
    )
    un = _receipt(shot, "unchecked", reason="no legible handle")
    assert sr.receipt_summary(un).endswith(": not checked: no legible handle.")

    find(Recorder("not_found"))
    row = sr.receipt_rows([_finding()], _deps, existing={}, now=lambda: NOW)[0]
    assert row["source_url"].startswith("https://bsky.app/profile/")  # the screenshot's post


def test_no_screenshot_read_means_no_rows_and_no_calls(find):
    fake = find(Recorder())
    assert sr.receipt_rows([_finding(shot=False)], _deps, existing={}) == []
    assert fake.calls == []


def test_anchor_is_earliest_post_carrying_the_screenshot(find):
    fake = find(Recorder())
    f = _finding()
    early, late = NOW - timedelta(days=2), NOW - timedelta(days=1)
    times = {f.group.post_uris[0]: late, f.group.post_uris[1]: early}
    sr.receipt_rows([f], _deps, existing={}, created_at=times, now=lambda: NOW)
    assert fake.anchors == [early]


def _stored(row, *, checked_at):
    return {"id": 7, "detail": {**row["detail"], "checked_at": checked_at.isoformat()}}


def test_fresh_row_is_reused_without_calls_and_stale_row_is_rechecked(find):
    find(Recorder())
    row = sr.receipt_rows([_finding()], _deps, existing={}, now=lambda: NOW)[0]
    key = row["detail"]["media_hash"]

    fake = find(Recorder())
    fresh = {key: _stored(row, checked_at=NOW - timedelta(hours=2))}
    again = sr.receipt_rows([_finding()], _deps, existing=fresh, now=lambda: NOW)
    assert fake.calls == []
    assert again[0]["detail"] is fresh[key]["detail"]  # unchanged: the upsert is a no-op
    assert again[0]["summary"] == row["summary"]

    stale = {key: _stored(row, checked_at=NOW - timedelta(hours=25))}
    sr.receipt_rows([_finding()], _deps, existing=stale, now=lambda: NOW)
    assert fake.calls == ["bafyshot"]

    other_read = {key: _stored(row, checked_at=NOW)}
    other_read[key]["detail"]["read"] = "something-else"  # the same file read differently
    sr.receipt_rows([_finding()], _deps, existing=other_read, now=lambda: NOW)
    assert fake.calls == ["bafyshot", "bafyshot"]


def test_failed_recheck_keeps_the_earlier_answer(find):
    find(Recorder("matches"))
    row = sr.receipt_rows([_finding()], _deps, existing={})[0]
    key = row["detail"]["media_hash"]
    stale = {key: _stored(row, checked_at=datetime.now(UTC) - timedelta(days=3))}

    def boom(shot, deps, budget):
        raise RuntimeError("network down")

    find(boom)
    kept = sr.receipt_rows([_finding()], _deps, existing=stale)
    assert kept[0]["detail"] is stale[key]["detail"]
    fresh = sr.receipt_rows([_finding()], _deps, existing={})
    assert fresh[0]["detail"]["label"] == "unchecked"
    assert fresh[0]["summary"].endswith("not checked: the check failed.")


def test_checks_run_in_parallel_within_the_budget(find):
    fake = find(Recorder(delay=0.3))
    started = time.monotonic()
    rows = sr.receipt_rows([_finding("bafya"), _finding("bafyb")], _deps, existing={})
    assert time.monotonic() - started < 0.6
    assert sorted(fake.calls) == ["bafya", "bafyb"] and len(rows) == 2


def test_a_stuck_check_never_holds_the_worker_past_the_budget(find, monkeypatch):
    monkeypatch.setattr(sr, "GRACE_S", 0.1)
    find(Recorder(delay=1.5))
    started = time.monotonic()
    rows = sr.receipt_rows([_finding()], _deps, existing={}, budget_s=0.2)
    assert time.monotonic() - started < 1.0
    assert rows[0]["detail"]["label"] == "unchecked"
    assert rows[0]["summary"].endswith("not checked: time budget used up.")


def test_comparable_ignores_volatile_receipt_fields_but_sees_a_label_change(find):
    find(Recorder("matches"))
    a = sr.receipt_rows([_finding()], _deps, existing={}, now=lambda: NOW)[0]["detail"]
    find(
        Recorder(
            "matches",
            counts_now=Metrics(likes=9999),
            latency_ms=5,
            receipt_ids=[4, 5],
            candidates_checked=7,  # an unrelated feed post more: not new evidence
        )
    )
    later = NOW + timedelta(days=2)
    b = sr.receipt_rows([_finding()], _deps, existing={}, now=lambda: later)[0]["detail"]
    assert a != b
    assert store._comparable(a) == store._comparable(b)

    find(Recorder("differs", diff=[DiffPart(op="shot_only", text="closed")]))
    c = sr.receipt_rows([_finding()], _deps, existing={}, now=lambda: later)[0]["detail"]
    assert store._comparable(a) != store._comparable(c)
    # other media rows keep their own comparison
    plain = {"media_hash": "h", "checked_at": "x", "fetches": [1]}
    assert store._comparable(plain) == plain


def test_persist_refreshes_volatile_fields_without_a_new_created_at(monkeypatch, find):
    find(Recorder("matches"))
    row = sr.receipt_rows([_finding()], _deps, existing={}, now=lambda: NOW)[0]
    old = {"id": 7, "detail": {**row["detail"], "checked_at": "2026-11-01T00:00:00+00:00"}}
    calls = []
    monkeypatch.setattr(
        store,
        "save_media_evidence",
        lambda conn, cid, *, existing, **row: (existing["id"], False),
    )
    monkeypatch.setattr(store, "refresh_media_detail", lambda conn, i, d: calls.append((i, d)))
    key = row["detail"]["media_hash"]
    assert sr.persist_receipt_rows(None, 1, [row], {key: old}) == 0
    assert calls == [(7, row["detail"])]


def test_flag_turns_receipts_off():
    assert sr.enabled({}) and sr.enabled({"MEDIA_SCREENSHOT_RECEIPTS": "1"})
    assert not sr.enabled({"MEDIA_SCREENSHOT_RECEIPTS": "0"})


@pytest.mark.parametrize("dry_run", [False, True])
def test_read_cluster_persists_receipts_except_on_dry_run(monkeypatch, find, dry_run):
    find(Recorder())
    run = MediaRun(findings=[_finding()])
    saved = []

    class Conn:
        commits = 0

        def commit(self):
            Conn.commits += 1

    monkeypatch.setattr(media_main, "load_cluster_media", lambda conn, cid: [])
    monkeypatch.setattr(media_main, "process_cluster_media", lambda *a, **k: run)
    monkeypatch.setattr(media_main, "persist_evidence", lambda conn, cid, r: 1)
    monkeypatch.setattr(store, "stored_media", lambda conn, cid: {})
    monkeypatch.setattr(store, "post_created_at", lambda conn, uris: {})
    monkeypatch.setattr(
        store,
        "save_media_evidence",
        lambda conn, cid, **row: saved.append(row["detail"]["media_hash"]) or (1, True),
    )
    media_main.read_cluster(Conn(), None, None, 1, max_media=4, dry_run=dry_run, receipts=_deps)
    assert saved == ([] if dry_run else ["screenshot-receipt:bafyshot"])


def test_cluster_with_screenshots_is_read_again_after_recheck_s():
    from types import SimpleNamespace

    from media.worker import ClusterProgress

    clock = [0.0]
    progress = ClusterProgress(clock=lambda: clock[0], recheck_s=100)
    shot_run = SimpleNamespace(
        failures=[], deferred=0, screenshot_failures=0, findings=[SimpleNamespace(screenshot=1)]
    )
    plain_run = SimpleNamespace(
        failures=[], deferred=0, screenshot_failures=0, findings=[SimpleNamespace(screenshot=None)]
    )
    progress.record(1, "t1", shot_run)
    progress.record(2, "t2", plain_run)
    assert progress.done() == {1: "t1", 2: "t2"}
    clock[0] = 101  # the receipt is due: the cluster comes back though it didn't change
    assert progress.done() == {2: "t2"}
    off = ClusterProgress(clock=lambda: clock[0])  # receipts off: read until it changes
    off.record(1, "t1", shot_run)
    clock[0] = 10_000
    assert off.done() == {1: "t1"}


def test_an_earlier_carrier_post_invalidates_the_stored_answer(find):
    u1, u2 = (f"at://did:plc:{u}/app.bsky.feed.post/{u}" for u in ("r1", "r2"))
    t1 = {u1: NOW - timedelta(days=1), u2: NOW - timedelta(hours=5)}
    find(Recorder())
    row = sr.receipt_rows([_finding()], _deps, existing={}, created_at=t1, now=lambda: NOW)[0]
    key = row["detail"]["media_hash"]
    assert row["detail"]["carrier_at"] == t1[u1].isoformat()

    fake = find(Recorder())
    stored = {key: _stored(row, checked_at=NOW - timedelta(hours=1))}
    sr.receipt_rows([_finding()], _deps, existing=stored, created_at=t1, now=lambda: NOW)
    assert fake.calls == []  # same carriers: reused

    t2 = {**t1, u2: NOW - timedelta(days=9)}  # an earlier post carrying the same image
    sr.receipt_rows([_finding()], _deps, existing=stored, created_at=t2, now=lambda: NOW)
    assert fake.calls == ["bafyshot"] and fake.anchors == [t2[u2]]
