from types import SimpleNamespace

import httpx

from media.blobs import BlobFetcher, blob_url
from media.nemotron import MediaAnalysis
from media.worker import ClusterMedia, process_cluster_media


def test_blob_url_supports_plain_pds_host():
    assert blob_url("pds.example", "did:plc:a", "bafy cid") == (
        "https://pds.example/xrpc/com.atproto.sync.getBlob?did=did%3Aplc%3Aa&cid=bafy+cid"
    )


def test_blob_fetcher_returns_content_type():
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "image/png; charset=binary"}, content=b"png"
        )

    transport = httpx.MockTransport(handler)
    with BlobFetcher(client=httpx.Client(transport=transport)) as fetcher:
        blob = fetcher.fetch(pds_host="https://pds.example", did="did:plc:a", cid="cid")
    assert blob.data == b"png"
    assert blob.mime_type == "image/png"


def test_worker_deduplicates_cids_and_keeps_failures():
    class FakeFetcher:
        def fetch(self, *, pds_host, did, cid):
            if cid == "missing":
                from media.blobs import BlobFetchError

                raise BlobFetchError("not found")
            return SimpleNamespace(data=b"image", mime_type="image/png", url="https://pds/blob")

    class FakeClient:
        def analyze(self, media, *, context=None):
            return MediaAnalysis(
                media_hash=media.media_hash,
                description="A poster",
                extracted_text=[],
                reuse_cues=[],
                manipulation_cues=[],
                confidence=0.7,
                model="test-model",
                latency_ms=1,
                source_uri=media.source_uri,
            )

    posts = [
        ClusterMedia(1, "post-1", "did:plc:a", "same", "pds.example"),
        ClusterMedia(1, "post-2", "did:plc:b", "same", "pds.example"),
        ClusterMedia(1, "post-3", "did:plc:c", "missing", "pds.example"),
    ]
    run = process_cluster_media(posts, FakeClient(), FakeFetcher())
    assert [a.media_hash for a in run.analyses] == ["same"]
    assert [f.media_hash for f in run.failures] == ["missing"]
    assert run.attempted == 2