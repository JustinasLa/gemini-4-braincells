import httpx
import pytest

from media.blobs import BlobFetcher, BlobFetchError, blob_url, media_urls
from media.nemotron import MediaAnalysis, NotVisionModelError
from media.worker import (
    TEMPLATE_KEY,
    ClusterMedia,
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
    assert detail["n_accounts"] == 2 and detail["n_posts"] == 2
    assert detail["post_urls"][0] == "https://bsky.app/profile/did:a/post/r1"
    assert "2 accounts" in run.findings[0].summary()
    assert run.findings[0].source_url() == "https://bsky.app/profile/did:a/post/r1"


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


def test_non_vl_error_is_not_swallowed():
    client = FakeClient(error=NotVisionModelError("text model"))
    with pytest.raises(NotVisionModelError):
        process_cluster_media([_post(1, "did:a", "x")], client, FakeFetcher())


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
