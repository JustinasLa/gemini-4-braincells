import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from media.nemotron import (
    ADULT_DESCRIPTION,
    MediaAnalysis,
    MediaInput,
    NemotronClient,
    NemotronError,
    NotVisionModelError,
    RateLimiter,
    is_vl_model,
    parse_content,
    safe_description,
)

GOOD = (
    '{"description":"A reused poster","media_type":"meme","extracted_text":["READ"],'
    '"reuse_cues":["same layout"],"manipulation_cues":[],"content_flags":["promo_or_ad"],'
    '"confidence":0.8}'
)


def _response(content, model="nemotron-3.5-super-vl", finish="stop"):
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=12),
        ),
    )


class FakeCompletions:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r


class FakeClient:
    def __init__(self, *results):
        self.chat = SimpleNamespace(completions=FakeCompletions(*results))


def _client(*results, **kw):
    fake = FakeClient(*results)
    sleeps = []
    client = NemotronClient("test", client=fake, sleep=sleeps.append, rpm=0, **kw)
    return client, fake.chat.completions, sleeps


def _status_error(code):
    request = httpx.Request("POST", "https://x.test/v1/chat/completions")
    cls = openai.RateLimitError if code == 429 else openai.APIStatusError
    return cls("boom", response=httpx.Response(code, request=request), body=None)


# --- parsing ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        GOOD,
        f"```json\n{GOOD}\n```",
        f"<think>let me look {{not json}}</think>\n{GOOD}",
        f"Here is the analysis:\n{GOOD}\nHope this helps.",
        [{"type": "text", "text": GOOD}],
    ],
)
def test_parse_content_tolerates_wrappers(content):
    assert parse_content(content)["description"] == "A reused poster"


@pytest.mark.parametrize("content", [None, "", "no json here", "[1, 2]"])
def test_parse_content_rejects_non_objects(content):
    with pytest.raises(ValueError):
        parse_content(content)


def test_null_content_is_an_error_that_names_the_finish_reason():
    client, _, _ = _client(_response(None, finish="length"))
    with pytest.raises(NemotronError, match="finish_reason=length"):
        client.analyze(MediaInput("h", "image/png", data=b"x"))


def test_fields_are_normalised_leniently():
    content = (
        '{"description":"Poster","extracted_text":"ONE LINE","media_type":"Selfie",'
        '"content_flags":["ADULT","made-up"],"confidence":"7"}'
    )
    client, _, _ = _client(_response(content))
    a = client.analyze(MediaInput("h", "image/png", data=b"x"))
    assert a.extracted_text == ["ONE LINE"]
    assert a.media_type == "other"
    assert a.content_flags == ["adult"]
    assert a.confidence == 1.0
    assert a.reuse_cues == [] and a.manipulation_cues == []


def test_missing_description_is_rejected():
    client, _, _ = _client(_response('{"description":"","confidence":0.4}'))
    with pytest.raises(NemotronError, match="invalid analysis"):
        client.analyze(MediaInput("h", "image/jpeg", url="https://example.test/a.jpg"))


# --- model check, request shape, retries ------------------------------------------------------


@pytest.mark.parametrize(
    "model_id, ok",
    [
        ("nemotron-3.5-super-vl", True),
        ("nvidia/nemotron-3.5-super-vl-120b-a12b", True),
        ("nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4", True),
        ("private/nvidia/nemotron-3.5-super-120b-a12b", False),
        ("nvidia/nemotron-3-super-120b-a12b", False),
        ("", False),
        (None, False),
    ],
)
def test_is_vl_model(model_id, ok):
    assert is_vl_model(model_id) is ok


def test_non_vl_response_fails_loudly():
    client, _, _ = _client(_response(GOOD, model="private/nvidia/nemotron-3.5-super-120b-a12b"))
    with pytest.raises(NotVisionModelError, match="not a VL model"):
        client.analyze(MediaInput("h", "image/png", data=b"x"))


def test_request_sends_image_and_reasoning_budget_and_records_model():
    client, calls, _ = _client(_response(GOOD), max_tokens=4096)
    a = client.analyze(MediaInput("h", "image/png", data=b"png", source_uri="https://cdn/x"))
    sent = calls.calls[0]
    assert sent["max_tokens"] == 4096
    assert "extra_body" not in sent  # reasoning stays on
    assert sent["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert a.model == "nemotron-3.5-super-vl"
    assert a.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "reasoning_tokens": 12,
    }
    assert a.finish_reason == "stop"


def test_reasoning_off_uses_chat_template_switch():
    client, calls, _ = _client(_response(GOOD), reasoning=False)
    client.analyze(MediaInput("h", "image/png", data=b"x"))
    assert calls.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_retries_429_and_5xx_with_exponential_backoff():
    client, calls, sleeps = _client(
        _status_error(429), _status_error(503), _response(GOOD), max_retries=4
    )
    client.analyze(MediaInput("h", "image/png", data=b"x"))
    assert len(calls.calls) == 3
    assert len(sleeps) == 2 and 1 <= sleeps[0] <= 1.5 and 2 <= sleeps[1] <= 3


def test_client_errors_are_not_retried():
    client, calls, sleeps = _client(_status_error(400))
    with pytest.raises(NemotronError, match="request failed"):
        client.analyze(MediaInput("h", "image/png", data=b"x"))
    assert len(calls.calls) == 1 and sleeps == []


def test_gives_up_after_max_retries():
    client, calls, _ = _client(_status_error(429), max_retries=3)
    with pytest.raises(NemotronError):
        client.analyze(MediaInput("h", "image/png", data=b"x"))
    assert len(calls.calls) == 3


def test_rate_limiter_spaces_calls():
    now = [100.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    limiter = RateLimiter(30, clock=lambda: now[0], sleep=sleep)  # 2 s apart
    limiter.wait()
    limiter.wait()
    now[0] += 5
    limiter.wait()
    assert slept == [2.0]


# --- caching ---------------------------------------------------------------------------------


def test_analyze_caches_by_media_hash():
    client, calls, _ = _client(_response(GOOD))
    media = MediaInput("hash-1", "image/png", data=b"png")
    assert client.analyze(media) == client.analyze(media)
    assert len(calls.calls) == 1 and client.calls == 1


def test_analysis_round_trips_through_stored_detail():
    client, _, _ = _client(_response(GOOD))
    a = client.analyze(MediaInput("hash-1", "image/png", data=b"png"))
    detail = a.evidence_detail() | {"post_urls": ["https://bsky.app/x"], "cached": False}
    assert MediaAnalysis.from_detail(detail) == a


def test_media_requires_a_url_or_bytes():
    with pytest.raises(ValueError, match="either url or data"):
        MediaInput("hash-3", "image/jpeg").image_url()


@pytest.mark.parametrize(
    "description, flags",
    [
        ("A man holding his penis in a chair.", []),  # word list catches an unflagged reply
        ("A person on a beach.", ["adult"]),  # the model's own flag is enough
    ],
)
def test_adult_media_is_never_described(description, flags):
    content = json.dumps({"description": description, "content_flags": flags, "confidence": 0.9})
    client, _, _ = _client(_response(content))
    a = client.analyze(MediaInput("h", "image/png", data=b"x"))
    assert a.description == ADULT_DESCRIPTION
    assert a.content_flags == ["adult"]


def test_ordinary_descriptions_pass_through():
    assert safe_description("A promotional graphic for a law firm.", ["promo_or_ad"]) == (
        "A promotional graphic for a law firm.",
        ["promo_or_ad"],
    )
