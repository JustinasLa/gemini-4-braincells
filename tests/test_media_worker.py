import dataclasses
import re
from types import SimpleNamespace

import httpx
import pytest

from media.blobs import BlobFetcher, BlobFetchError, blob_url, media_urls
from media.nemotron import MediaAnalysis, NotVisionModelError
from media.worker import (
    TEMPLATE_KEY,
    ClusterMedia,
    ClusterProgress,
    FailureBackoff,
    group_media,
    ocr_template,
    process_cluster_media,
)


def _analysis(media_hash, text=(), model="nemotron-3.5-super-vl"):
    return MediaAnalysis(
        media_hash=media_hash,
        description="A poster",
        extracted_text=list(text),
        reuse_cues=[],
        manipulation_cues=[],
        confidence=0.7,
        model=model,
        latency_ms=1,
    )


class FakeFetcher:
    def __init__(self, missing=()):
        self.missing, self.calls = set(missing), []

    def fetch(self, *, did, cid, pds_host=None, embed_kind=None):
        self.calls.append(cid)
        if cid in self.missing:
            raise BlobFetchError("not found")

        class B:
            data, mime_type, url, media_kind = b"img", "image/jpeg", f"https://cdn/{cid}", "image"

        return B()


class FakeClient:
    def __init__(self, texts=None, error=None):
        self.texts, self.error, self.calls = texts or {}, error, []

    def analyze(self, media, *, context=None):
        self.calls.append(media.media_hash)
        if self.error:
            raise self.error
        return _analysis(media.media_hash, self.texts.get(media.media_hash, ()))


def _post(n, did, media_hash, kind="images"):
    return ClusterMedia(
        1, f"at://{did}/app.bsky.feed.post/r{n}", did, media_hash, None, kind, f"post {n}"
    )


# --- blob URLs -------------------------------------------------------------------------------


def test_blob_url_supports_plain_pds_host():
    assert blob_url("pds.example", "did:plc:a", "bafy cid") == (
        "https://pds.example/xrpc/com.atproto.sync.getBlob?did=did%3Aplc%3Aa&cid=bafy+cid"
    )


def test_media_urls_by_embed_kind():
    assert [k for _, k in media_urls("did:plc:a", "c", "video", "pds.x")] == ["video_thumbnail"]
    images = media_urls("did:plc:a", "c", "images", None)
    assert images == [("https://cdn.bsky.app/img/feed_fullsize/plain/did:plc:a/c@jpeg", "image")]
    either = [k for _, k in media_urls("did:plc:a", "c", "recordWithMedia", "pds.x")]
    assert either == ["image", "video_thumbnail", "image"]


def test_blob_fetcher_falls_back_and_rejects_non_images():
    def handler(request):
        if "cdn.bsky.app" in request.url.host:
            return httpx.Response(404)
        if "video.bsky.app" in request.url.host:
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")
        return httpx.Response(200, headers={"content-type": "image/png; x=1"}, content=b"png")

    with BlobFetcher(client=httpx.Client(transport=httpx.MockTransport(handler))) as fetcher:
        blob = fetcher.fetch(did="did:plc:a", cid="c", pds_host="pds.example")
        assert (blob.data, blob.mime_type) == (b"png", "image/png")
        assert blob.url.startswith("https://pds.example/")
        with pytest.raises(BlobFetchError, match="HTTP 404"):
            fetcher.fetch(did="did:plc:a", cid="c", embed_kind="images")


# --- candidate media selection ----------------------------------------------------------------


def test_group_media_ranks_most_reused_first():
    posts = [
        _post(1, "did:a", "solo"),
        _post(2, "did:a", "twice-same-account"),
        _post(3, "did:a", "twice-same-account"),
        _post(4, "did:b", "shared"),
        _post(5, "did:c", "shared"),
    ]
    groups = group_media(posts)
    assert [g.media_hash for g in groups] == ["shared", "twice-same-account", "solo"]
    assert groups[0].dids == ["did:b", "did:c"]


def test_worker_reads_each_cid_once_caps_media_and_keeps_failures():
    posts = [
        _post(1, "did:a", "same"),
        _post(2, "did:b", "same"),
        _post(3, "did:c", "missing"),
        _post(4, "did:d", "extra"),
    ]
    client = FakeClient()
    run = process_cluster_media(posts, client, FakeFetcher(missing={"missing"}), max_media=2)
    assert [f.analysis.media_hash for f in run.findings] == ["same"]
    assert [f.media_hash for f in run.failures] == ["missing"]
    assert run.skipped == 1 and client.calls == ["same"]
    detail = run.findings[0].detail()
    assert detail["status"] == "complete"
    assert detail["n_accounts"] == 2 and detail["n_posts"] == 2
    assert detail["post_urls"][0] == "https://bsky.app/profile/did:a/post/r1"
    assert "2 accounts" in run.findings[0].summary()
    assert run.findings[0].source_url() == "https://bsky.app/profile/did:a/post/r1"


def test_failed_media_is_exposed_as_retryable_evidence():
    run = process_cluster_media(
        [_post(1, "did:a", "missing")], FakeClient(), FakeFetcher(missing={"missing"})
    )
    row = run.evidence_rows()[0]
    assert row["model"] is None
    assert row["summary"] == "Media processing failed during fetch; analysis is unavailable."
    assert row["detail"] == {
        "media_hash": "missing",
        "status": "failed",
        "type": "processing_failure",
        "stage": "fetch",
        "error_code": "BlobFetchError",
        "media_kind": "images",
        "post_uris": ["at://did:a/app.bsky.feed.post/r1"],
        "post_urls": ["https://bsky.app/profile/did:a/post/r1"],
        "dids": ["did:a"],
        "profile_urls": ["https://bsky.app/profile/did:a"],
    }
    assert row["source_url"] == "https://bsky.app/profile/did:a/post/r1"
    assert "not found" not in str(row)


def test_malformed_cached_analysis_is_reanalyzed_instead_of_used_as_success():
    stored = _analysis("same").evidence_detail() | {"description": "", "status": "complete"}
    client, fetcher = FakeClient(), FakeFetcher()
    run = process_cluster_media(
        [_post(1, "did:a", "same")],
        client,
        fetcher,
        cache_lookup=lambda _hash: stored,
    )
    assert client.calls == ["same"] and fetcher.calls == ["same"]
    assert run.findings[0].cached is False


def test_stored_analysis_is_reused_without_fetch_or_model_call():
    stored = _analysis("same").evidence_detail() | {"media_kind": "image", "media_url": "u"}
    client, fetcher = FakeClient(), FakeFetcher()
    run = process_cluster_media(
        [_post(1, "did:a", "same")],
        client,
        fetcher,
        cache_lookup=lambda h: stored if h == "same" else None,
    )
    assert client.calls == [] and fetcher.calls == []
    assert run.findings[0].cached and run.findings[0].analysis.description == "A poster"


class CarrierFetcher(FakeFetcher):
    def __init__(self, missing_dids):
        super().__init__()
        self.missing_dids = set(missing_dids)

    def fetch(self, *, did, cid, pds_host=None, embed_kind=None):
        self.calls.append((did, cid, pds_host, embed_kind))
        if did in self.missing_dids:
            raise BlobFetchError("carrier no longer serves this blob")
        return SimpleNamespace(
            data=b"img", mime_type="image/jpeg", url=f"https://cdn/{did}/{cid}"
        )


@pytest.mark.parametrize("cached_screenshot", [False, True])
def test_shared_blob_uses_another_carrier_when_the_first_is_unavailable(cached_screenshot):
    posts = [_post(1, "did:a", "same"), _post(2, "did:b", "same")]
    fetcher = CarrierFetcher(missing_dids={"did:a"})
    client = ScreenshotClient()
    stored = _stored(ScreenshotClient(), screenshot_status="failed") if cached_screenshot else None
    if stored is not None:
        stored["media_hash"] = "same"
    run = process_cluster_media(posts, client, fetcher, cache_lookup=lambda _: stored)

    assert run.failures == [] and run.screenshot_failures == 0
    assert [call[0] for call in fetcher.calls] == ["did:a", "did:b"]
    assert client.calls == ([] if cached_screenshot else ["same"])
    assert client.shot_calls == ["same"]
    assert run.findings[0].group.dids == ["did:a", "did:b"]
    if not cached_screenshot:
        assert run.findings[0].media_url == "https://cdn/did:b/same"


def test_shared_blob_tries_each_source_once_before_recording_failure():
    posts = [
        _post(1, "did:a", "same"),
        _post(2, "did:a", "same"),
        _post(3, "did:b", "same"),
    ]
    fetcher = CarrierFetcher(missing_dids={"did:a", "did:b"})
    client = FakeClient()
    run = process_cluster_media(posts, client, fetcher)

    assert [call[0] for call in fetcher.calls] == ["did:a", "did:b"]
    assert client.calls == [] and run.findings == []
    assert len(run.failures) == 1
    assert run.failures[0].stage == "fetch"
    assert run.failures[0].dids == ("did:a", "did:b")


def test_dead_blob_in_a_large_cluster_has_bounded_carrier_attempts():
    posts = [_post(i, f"did:{i}", "same") for i in range(50)]
    fetcher = CarrierFetcher(missing_dids={p.did for p in posts})
    run = process_cluster_media(posts, FakeClient(), fetcher)
    assert len(fetcher.calls) == 4
    assert len(run.failures) == 1 and run.failures[0].stage == "fetch"


def test_non_vl_error_is_not_swallowed():
    client = FakeClient(error=NotVisionModelError("text model"))
    with pytest.raises(NotVisionModelError):
        process_cluster_media([_post(1, "did:a", "x")], client, FakeFetcher())


def test_bounded_carrier_retries_can_reach_accounts_beyond_the_initial_prefix(monkeypatch):
    posts = [_post(i, f"did:{i}", "same") for i in range(50)]
    fetcher = CarrierFetcher(missing_dids={f"did:{i}" for i in range(49)})
    calls = []

    def choose(sources, count):
        calls.append(True)
        return sources[:count] if len(calls) == 1 else sources[-count:]

    monkeypatch.setattr("media.worker.sample", choose)
    first = process_cluster_media(posts, FakeClient(), fetcher)
    second = process_cluster_media(posts, FakeClient(), fetcher)
    assert len(first.failures) == 1
    assert second.failures == [] and len(second.findings) == 1
    assert len(fetcher.calls) == 8


def test_failure_backoff_defers_then_retries():
    now = [0.0]
    backoff = FailureBackoff(base_s=10, clock=lambda: now[0])
    backoff.failed("h")
    assert backoff("h") and not backoff("other")
    now[0] = 11
    assert not backoff("h")
    backoff.failed("h")  # second failure: 20 s
    now[0] = 25
    assert backoff("h")


# --- cross-image OCR template ------------------------------------------------------------------


def _run_with_texts(texts):
    posts = [_post(i, f"did:{i}", h) for i, h in enumerate(texts)]
    return process_cluster_media(posts, FakeClient(texts), FakeFetcher(), max_media=10)


AD = "LAW FIRMS USE VIDEO MARKETING Around 24% of law firms use video {city} VIDEO PRODUCTION CO."


def test_ocr_template_links_different_images_with_swapped_words():
    run = _run_with_texts(
        {
            "a": [AD.format(city="CHICAGO")],
            "b": [AD.format(city="AUSTIN")],
            "c": [AD.format(city="NASHVILLE")],
            "unrelated": ["Totally different words about a cat picture today"],
        }
    )
    rows = run.evidence_rows()
    template = rows[-1]
    assert template["detail"]["media_hash"] == TEMPLATE_KEY
    assert template["detail"]["media_hashes"] == ["a", "b", "c"]
    assert template["detail"]["varying_tokens"] == ["austin", "chicago", "nashville"]
    assert template["model"] == "nemotron-3.5-super-vl"
    assert "3 different images posted by 3 accounts" in template["summary"]
    assert len(rows) == 5


def test_ocr_template_needs_two_accounts_and_real_text():
    one_account = [_post(1, "did:a", "a"), _post(2, "did:a", "b")]
    texts = {"a": [AD.format(city="X")], "b": [AD.format(city="Y")]}
    run = process_cluster_media(one_account, FakeClient(texts), FakeFetcher())
    assert ocr_template(run.findings) is None
    assert ocr_template(_run_with_texts({"a": ["HI"], "b": ["HI"]}).findings) is None


def test_backed_off_media_is_counted_as_deferred_then_retried():
    now = [0.0]
    backoff = FailureBackoff(base_s=300, clock=lambda: now[0])
    fetcher = FakeFetcher(missing={"flaky"})
    posts = [_post(1, "did:a", "flaky"), _post(2, "did:b", "ok")]
    first = process_cluster_media(posts, FakeClient(), fetcher, skip=backoff)
    for f in first.failures:
        backoff.failed(f.media_hash)
    assert [f.media_hash for f in first.failures] == ["flaky"]

    now[0] = 60  # next pass, still backing off: not a failure, but not complete either
    second = process_cluster_media(posts, FakeClient(), fetcher, skip=backoff)
    assert second.failures == [] and second.deferred == 1

    now[0] = 301  # backoff over: tried again
    fetcher.missing.clear()
    third = process_cluster_media(posts, FakeClient(), fetcher, skip=backoff)
    assert third.deferred == 0 and {f.group.media_hash for f in third.findings} == {"flaky", "ok"}


def test_cluster_progress_holds_incomplete_clusters_back_for_a_while():
    from media.worker import MediaFailure, MediaRun

    now = [0.0]
    progress = ClusterProgress(retry_s=300, clock=lambda: now[0])
    progress.record(1, "t1", MediaRun())
    progress.record(2, "t2", MediaRun(failures=[MediaFailure("h", "did:a", "404")]))
    progress.record(3, "t3", MediaRun(deferred=1))
    assert progress.done() == {1: "t1", 2: "t2", 3: "t3"}
    now[0] = 301
    assert progress.done() == {1: "t1"}
    progress.record(2, "t2", MediaRun())
    assert progress.done() == {1: "t1", 2: "t2"}


# --- screenshot read ---------------------------------------------------------------------------


def _shot(media_hash, handle="jane.example", display_name="Jane", platform="x", oddities=()):
    from media.screenshot import ScreenshotPost

    return ScreenshotPost(
        media_hash=media_hash,
        platform=platform,
        display_name=display_name,
        handle=handle,
        shown_time="3:14 PM · Nov 13, 2026",
        text="Hello",
        counts=dict.fromkeys(("replies", "reposts", "quotes", "likes", "views", "bookmarks")),
        ui_oddities=list(oddities),
        confidence=0.8,
        model="nemotron-3.5-super-vl",
        latency_ms=5,
    )


class ScreenshotClient(FakeClient):
    def __init__(self, media_type="screenshot", flags=(), shot="post", shot_error=None):
        super().__init__()
        self.media_type, self.flags = media_type, list(flags)
        self.shot, self.shot_error, self.shot_calls = shot, shot_error, []

    def analyze(self, media, *, context=None):
        self.calls.append(media.media_hash)
        return dataclasses.replace(
            _analysis(media.media_hash), media_type=self.media_type, content_flags=self.flags
        )

    def read_screenshot(self, media):
        self.shot_calls.append(media.media_hash)
        if self.shot_error:
            raise self.shot_error
        return _shot(media.media_hash) if self.shot == "post" else None


def _one(client, **kw):
    return process_cluster_media([_post(1, "did:a", "h")], client, FakeFetcher(), **kw)


def test_screenshot_is_read_once_and_stored_in_detail():
    client = ScreenshotClient()
    run = _one(client)
    assert client.calls == ["h"] and client.shot_calls == ["h"]
    detail = run.findings[0].detail()
    assert detail["screenshot_status"] == "post" and "screenshot_error" not in detail
    assert detail["screenshot"]["handle"] == "jane.example"
    assert detail["screenshot"]["prompt_version"] == "screenshot-v1"
    assert run.screenshot_failures == 0


def test_non_screenshot_media_gets_no_screenshot_read_or_keys():
    client = ScreenshotClient(media_type="meme")
    run = _one(client)
    assert client.shot_calls == []
    assert not {"screenshot", "screenshot_status"} & run.findings[0].detail().keys()


def test_screenshot_that_is_not_a_post():
    run = _one(ScreenshotClient(shot=None))
    assert run.findings[0].detail()["screenshot_status"] == "not_post"
    assert run.findings[0].detail()["screenshot"] is None
    assert "Reads as a screenshot" not in run.findings[0].summary()


def test_client_without_read_screenshot_leaves_status_unset():
    class AnalyzeOnly(FakeClient):
        def analyze(self, media, *, context=None):
            return dataclasses.replace(super().analyze(media), media_type="screenshot")

    run = _one(AnalyzeOnly())
    assert run.findings[0].screenshot_status is None and run.screenshot_failures == 0


def test_adult_screenshot_is_skipped_without_a_call():
    client = ScreenshotClient(flags=["adult"])
    run = _one(client)
    assert client.shot_calls == []
    assert run.findings[0].detail()["screenshot_status"] == "skipped_adult"


def test_failed_screenshot_read_keeps_analysis_and_marks_cluster_incomplete():
    from media.nemotron import NemotronError

    client = ScreenshotClient(shot_error=NemotronError("invalid screenshot read"))
    run = _one(client)
    assert run.failures == [] and run.findings[0].analysis.description == "A poster"
    detail = run.findings[0].detail()
    assert detail["screenshot_status"] == "failed" and detail["screenshot"] is None
    assert detail["screenshot_error"] == "invalid screenshot read"
    assert run.screenshot_failures == 1
    now = [0.0]
    progress = ClusterProgress(retry_s=300, clock=lambda: now[0])
    progress.record(7, "t", run)
    assert progress.done() == {7: "t"}
    now[0] = 301
    assert progress.done() == {}


def test_screenshot_read_never_swallows_non_vl_error():
    client = ScreenshotClient(shot_error=NotVisionModelError("text model"))
    with pytest.raises(NotVisionModelError):
        _one(client)


def _stored(client, **extra):
    detail = _one(client).findings[0].detail()
    return {k: v for k, v in detail.items() if k not in extra} | extra


def test_cached_screenshot_post_is_reused_without_model_calls():
    stored = _stored(ScreenshotClient())
    client, fetcher = ScreenshotClient(), FakeFetcher()
    run = process_cluster_media(
        [_post(1, "did:a", "h")], client, fetcher, cache_lookup=lambda h: stored
    )
    assert client.calls == [] and client.shot_calls == [] and fetcher.calls == []
    finding = run.findings[0]
    assert finding.cached and finding.screenshot_status == "post"
    assert finding.screenshot == _shot("h")


def test_cached_screenshot_from_older_screenshot_prompt_is_read_again():
    stored = _stored(ScreenshotClient())
    stored["screenshot"] = stored["screenshot"] | {"prompt_version": "screenshot-v0"}
    client = ScreenshotClient()
    run = process_cluster_media(
        [_post(1, "did:a", "h")], client, FakeFetcher(), cache_lookup=lambda h: stored
    )
    assert client.calls == [] and client.shot_calls == ["h"]
    assert run.findings[0].screenshot.prompt_version == "screenshot-v1"


def test_screenshot_status_records_its_prompt_version():
    detail = _stored(ScreenshotClient())
    assert detail["screenshot_prompt_version"] == "screenshot-v1"


def test_cached_not_post_from_older_screenshot_prompt_is_read_again():
    stored = _stored(ScreenshotClient(), screenshot_status="not_post", screenshot=None)
    stored["screenshot_prompt_version"] = "screenshot-v0"
    client = ScreenshotClient()
    run = process_cluster_media(
        [_post(1, "did:a", "h")], client, FakeFetcher(), cache_lookup=lambda h: stored
    )
    assert client.calls == [] and client.shot_calls == ["h"]
    assert run.findings[0].screenshot_status == "post"


def test_cached_failed_screenshot_read_calls_only_read_screenshot():
    from media.nemotron import NemotronError

    stored = _stored(ScreenshotClient(shot_error=NemotronError("timeout")))
    assert stored["screenshot_status"] == "failed"
    client, fetcher = ScreenshotClient(), FakeFetcher()
    run = process_cluster_media(
        [_post(1, "did:a", "h")], client, fetcher, cache_lookup=lambda h: stored
    )
    assert client.calls == [] and client.shot_calls == ["h"] and fetcher.calls == ["h"]
    detail = run.findings[0].detail()
    assert detail["screenshot_status"] == "post" and "screenshot_error" not in detail
    assert detail["cached"] is True and run.screenshot_failures == 0


def test_cached_failed_screenshot_whose_blob_is_gone_stays_failed():
    stored = _stored(ScreenshotClient(), screenshot_status="failed")
    client = ScreenshotClient()
    run = process_cluster_media(
        [_post(1, "did:a", "h")],
        client,
        FakeFetcher(missing={"h"}),
        cache_lookup=lambda h: stored,
    )
    assert client.calls == [] and client.shot_calls == [] and run.failures == []
    finding = run.findings[0]
    assert finding.analysis.description == "A poster"
    assert finding.screenshot_status == "failed" and finding.screenshot_error == "not found"
    assert run.screenshot_failures == 1


def test_screenshot_summary_describes_the_media_not_the_people():
    from media.worker import Finding, MediaGroup

    analysis = dataclasses.replace(_analysis("h"), media_type="screenshot")
    group = MediaGroup("h", [_post(1, "did:a", "h")])
    finding = Finding(group, analysis, "image", "u", False, _shot("h"), "post")
    assert finding.summary() == (
        "Nemotron Super VL: A poster. Reads as a screenshot of an X post shown as from "
        "@jane.example."
    )
    odd = _shot("h", handle=None, platform="unknown", oddities=["mixed fonts", "odd badge", "c"])
    finding = Finding(group, analysis, "image", "u", False, odd, "post")
    assert finding.summary().endswith(
        ' Reads as a screenshot of a social-media post shown as from "Jane".'
        " Screenshot cues: mixed fonts; odd badge."
    )
    assert not re.search(r"\b(bot|shill|spammer)", finding.summary(), re.I)
    insta = _shot("h", handle=None, display_name=None, platform="instagram")
    finding = Finding(group, analysis, "image", "u", False, insta, "post")
    assert finding.summary().endswith(" Reads as a screenshot of an Instagram post.")


def test_comparable_ignores_screenshot_latency():
    from media.store import _comparable

    detail = _stored(ScreenshotClient())
    later = detail | {"screenshot": detail["screenshot"] | {"latency_ms": 999}}
    assert _comparable(detail) == _comparable(later)
    changed = detail | {"screenshot": detail["screenshot"] | {"text": "Edited"}}
    assert _comparable(detail) != _comparable(changed)


# --- bounded blob download and worker pass isolation -------------------------------------------


def test_blob_fetcher_stops_reading_an_oversized_body(monkeypatch):
    import media.blobs as blobs

    monkeypatch.setattr(blobs, "MAX_BYTES", 10)
    sent = []

    def endless():  # a PDS that never ends its body
        while True:
            sent.append(1)
            yield b"x" * 4

    def handler(request):
        return httpx.Response(200, headers={"content-type": "image/png"}, content=endless())

    with BlobFetcher(client=httpx.Client(transport=httpx.MockTransport(handler))) as fetcher:
        with pytest.raises(BlobFetchError, match="over 10 bytes"):
            fetcher.fetch(did="did:plc:a", cid="c", embed_kind="images")
    assert len(sent) <= 4


def test_blob_fetcher_does_not_read_rejected_bodies():
    read = []

    def body():
        read.append(1)
        yield b"<html>"

    def handler(request):
        if "cdn.bsky.app" in request.url.host:
            return httpx.Response(404, content=body())
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body())

    with BlobFetcher(client=httpx.Client(transport=httpx.MockTransport(handler))) as fetcher:
        with pytest.raises(BlobFetchError, match="HTTP 404, not an image"):
            fetcher.fetch(did="did:plc:a", cid="c", embed_kind="recordWithMedia")
    assert read == []


def test_worker_pass_survives_a_cluster_whose_read_raises(monkeypatch):
    import contextlib
    from types import SimpleNamespace

    import media.__main__ as cli
    from media.worker import MediaRun

    class Conn:
        rollbacks = 0

        def rollback(self):
            Conn.rollbacks += 1

    queried, reads, passes = [], [], [0]

    def candidates(conn, *, statuses, min_score, limit, done):
        queried.append(dict(done))
        return [{"id": i, "updated_at": "t"} for i in (1, 2) if done.get(i) != "t"]

    def read_cluster(conn, client, fetcher, cluster_id, **kw):
        reads.append(cluster_id)
        if cluster_id == 1:
            raise RuntimeError("bad row")
        return MediaRun()

    class Stop(Exception):
        pass

    def sleep(_):
        passes[0] += 1
        if passes[0] == 2:
            raise Stop

    monkeypatch.setattr(cli, "connect", lambda: contextlib.nullcontext(Conn()))
    monkeypatch.setattr(cli.store, "candidate_clusters", candidates)
    monkeypatch.setattr(cli, "read_cluster", read_cluster)
    monkeypatch.setattr(cli, "_receipt_clients", lambda s: contextlib.nullcontext(None))
    monkeypatch.setattr(cli.time, "sleep", sleep)
    settings = SimpleNamespace(
        media_statuses="coordinated", media_min_score=0.5, media_max_per_cluster=4
    )
    with pytest.raises(Stop):
        cli.run_worker(object(), settings, interval_s=0, batch=25)
    # cluster 2 is read in the same pass; cluster 1 is held back instead of retried at once
    assert reads == [1, 2] and Conn.rollbacks == 1
    assert queried[1] == {1: "t", 2: "t"}
