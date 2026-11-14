"""Lookup URL validation at the API boundary, without provider calls."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import lookup as lookup_route


@pytest.fixture
def client():
    started = []

    class Job:
        def snapshot(self):
            return {"state": "done"}

    class Jobs:
        def start(self, inp, client):
            started.append(inp)
            return Job()

    app = FastAPI()
    app.include_router(lookup_route.router)
    app.dependency_overrides[lookup_route.get_jobs] = lambda: Jobs()
    with TestClient(app) as http:
        yield http, started


@pytest.mark.parametrize(
    "url",
    [
        "https://[",
        "https://[not-an-ip]/",
        "http://",
        "https:///path",
        "https://example.com:not-a-port/path",
        "https://example.com:65536/path",
        "https://example.com\uff0fpath",
    ],
)
def test_malformed_url_is_rejected_before_starting_job(client, url):
    http, started = client
    response = http.post("/api/lookup", json={"url": url})
    assert response.status_code == 400
    assert response.json()["detail"] == "url must be an http(s) link"
    assert started == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/path?query=one#section",
        "HTTPS://example.com/path",
        "http://example.com:8080/path",
        "https://example.com:443/path",
        "https://b\u00fccher.example/path",
    ],
)
def test_valid_url_reaches_job_store_unchanged(client, url):
    http, started = client
    response = http.post("/api/lookup", json={"url": f"  {url}  "})
    assert response.status_code == 200
    assert len(started) == 1
    assert started[0].url == url
