"""Fixture-driven verification of stored media evidence through the API and dashboard contract."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.deps import get_store
from api.main import app
from api.schemas import ClusterDetail, Evidence
from media.blobs import BlobFetchError
from media.nemotron import MediaAnalysis, NemotronError
from media.worker import ClusterMedia, process_cluster_media

ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)


class FixtureFetcher:
    def __init__(self, missing=()):
        self.missing = set(missing)

    def fetch(self, *, did, cid, pds_host=None, embed_kind=None):
        if cid in self.missing:
            raise BlobFetchError(f"missing {cid}")

        class Blob:
            data = b"image"
            mime_type = "image/jpeg"
            url = "https://cdn.example/" + cid
            media_kind = "image"

        return Blob()


class FixtureClient:
    def __init__(self, failed=()):
        self.failed = set(failed)

    def analyze(self, media, *, context=None):
        if media.media_hash in self.failed:
            raise NemotronError(f"failed {media.media_hash}")
        return MediaAnalysis(
            media_hash=media.media_hash,
            description=f"Fixture {media.media_hash}",
            extracted_text=[],
            reuse_cues=[],
            manipulation_cues=[],
            confidence=0.8,
            model="nemotron-3.5-super-vl",
            latency_ms=1,
        )


def post(number, did, media_hash):
    return ClusterMedia(
        42,
        f"at://{did}/app.bsky.feed.post/{number}",
        did,
        media_hash,
        embed_kind="images",
        text=f"fixture {number}",
    )


@pytest.fixture
def media_cases():
    return {
        "missing": {
            "posts": [post(1, "did:missing", "missing")],
            "fetcher": FixtureFetcher(missing={"missing"}),
            "client": FixtureClient(),
            "findings": 0,
            "failures": ["missing"],
        },
        "failed": {
            "posts": [post(2, "did:failed", "failed")],
            "fetcher": FixtureFetcher(),
            "client": FixtureClient(failed={"failed"}),
            "findings": 0,
            "failures": ["failed"],
        },
        "duplicate": {
            "posts": [post(3, "did:a", "same"), post(4, "did:b", "same")],
            "fetcher": FixtureFetcher(),
            "client": FixtureClient(),
            "findings": 1,
            "failures": [],
        },
        "multiple": {
            "posts": [post(5, "did:a", "one"), post(6, "did:b", "two")],
            "fetcher": FixtureFetcher(),
            "client": FixtureClient(),
            "findings": 2,
            "failures": [],
        },
    }


def test_media_fixtures_cover_worker_outcomes(media_cases):
    for name, case in media_cases.items():
        run = process_cluster_media(case["posts"], case["client"], case["fetcher"])
        assert len(run.findings) == case["findings"], name
        assert [failure.media_hash for failure in run.failures] == case["failures"], name
        if name == "duplicate":
            assert run.findings[0].detail()["n_accounts"] == 2
            assert run.findings[0].detail()["n_posts"] == 2


class ApiFixtureStore:
    def __init__(self, detail):
        self.detail = detail

    def get_cluster(self, cluster_id):
        return self.detail if cluster_id == 42 else None


def test_stored_media_reaches_api_and_dashboard(media_cases):
    run = process_cluster_media(
        media_cases["multiple"]["posts"],
        media_cases["multiple"]["client"],
        media_cases["multiple"]["fetcher"],
    )
    evidence = [
        Evidence(
            id=index,
            kind="media",
            summary=f.summary(),
            detail=f.detail(),
            source_url=f.source_url(),
            model=f.analysis.model,
            created_at=T0,
        )
        for index, f in enumerate(run.findings, 1)
    ]
    detail = ClusterDetail(
        id=42,
        status="coordinated",
        score=0.9,
        window_start=T0,
        window_end=T0,
        evidence=evidence,
    )
    store = ApiFixtureStore(detail)
    app.dependency_overrides[get_store] = lambda: store
    try:
        response = TestClient(app).get("/api/clusters/42")
        assert response.status_code == 200
        payload = response.json()
        assert [item["detail"]["media_hash"] for item in payload["evidence"]] == ["one", "two"]
        assert payload["evidence"][0]["detail"]["media_url"].startswith("https://cdn.example/")
        assert payload["evidence"][0]["detail"]["n_accounts"] == 1
    finally:
        app.dependency_overrides.clear()

