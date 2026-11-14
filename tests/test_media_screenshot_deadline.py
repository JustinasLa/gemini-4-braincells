"""NemotronClient.read_screenshot(deadline=...): the rate-limit wait, each request and every
retry end by the caller's deadline (lookup's screenshot step). No network."""

import json
import time

import openai
import pytest

from media.nemotron import MediaInput, NemotronTimeUp, RateLimiter
from tests.test_media_screenshot import FULL, _client, _response


def _media():
    return MediaInput("shot-hash", "image/png", data=b"png")


def test_request_timeout_is_clamped_to_the_deadline():
    client, calls = _client(_response(json.dumps(FULL)))
    post = client.read_screenshot(_media(), deadline=time.monotonic() + 5)
    assert post is not None
    assert 0 < calls.calls[0]["timeout"] <= 5


def test_no_deadline_keeps_the_client_default():
    client, calls = _client(_response(json.dumps(FULL)))
    client.read_screenshot(_media())
    assert "timeout" not in calls.calls[0]


def test_past_deadline_no_request_starts():
    client, calls = _client(_response(json.dumps(FULL)))
    with pytest.raises(NemotronTimeUp):
        client.read_screenshot(_media(), deadline=time.monotonic() + 0.5)
    assert calls.calls == []


def test_no_retry_when_the_backoff_would_pass_the_deadline():
    err = openai.APIConnectionError(request=None)
    client, calls = _client(err, _response(json.dumps(FULL)))
    with pytest.raises(NemotronTimeUp):
        client.read_screenshot(_media(), deadline=time.monotonic() + 1.8)
    assert len(calls.calls) == 1


def test_rate_limiter_wait_respects_the_deadline():
    now = [100.0]
    slept = []
    lim = RateLimiter(6, clock=lambda: now[0], sleep=slept.append)  # one call per 10 s
    lim.wait()
    with pytest.raises(NemotronTimeUp):
        lim.wait(deadline=105.0)  # the next slot is at 110
    assert slept == []
    lim.wait(deadline=120.0)  # the slot was not taken by the refused call
    assert slept == [10.0]
