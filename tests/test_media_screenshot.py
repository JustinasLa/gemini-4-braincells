import json
import logging
import math
from types import SimpleNamespace

import pytest

from media.nemotron import (
    MediaInput,
    NemotronClient,
    NemotronError,
    NotVisionModelError,
    parse_content,
)
from media.screenshot import (
    COUNT_KEYS,
    SCREENSHOT_PROMPT_VERSION,
    SCREENSHOT_SYSTEM_PROMPT,
    SCREENSHOT_USER_PROMPT,
    ScreenshotPost,
    normalise_counts,
    normalise_handle,
    normalise_platform,
    parse_count,
    screenshot_from_payload,
)

VL_MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"

FULL = {
    "is_post": True,
    "platform": "X (Twitter)",
    "display_name": "Jane Doe",
    "handle": "@janedoe",
    "shown_time": "3:14 PM · Nov 13, 2026",
    "text": "First line\nSecond line",
    "counts": {"replies": "37", "retweets": "1,204", "quotes": None, "likes": "2.5K"},
    "ui_oddities": ["Mixed fonts in the text block"],
    "confidence": 0.82,
}


def _post(payload, **kw):
    kw = {"media_hash": "h", "model": VL_MODEL, "latency_ms": 5, **kw}
    return screenshot_from_payload(payload, **kw)


# --- fake OpenAI client (same shape as tests/test_media.py) ---


def _response(content, model=VL_MODEL, finish="stop"):
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
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
    client = NemotronClient("k", client=fake, sleep=lambda s: None, rpm=0, **kw)
    return client, fake.chat.completions


def _media(media_hash="shot-hash"):
    return MediaInput(media_hash, "image/png", data=b"png")


# --- parse_count ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2.5K", 2500),
        ("2.5k", 2500),
        ("1.2M", 1_200_000),
        ("1.2B", 1_200_000_000),
        ("2,5K", 2500),
        ("1,204", 1204),
        ("1.204", 1204),
        ("1,204,567", 1_204_567),
        ("37", 37),
        (" 37 ", 37),
        (37, 37),
        (0, 0),
        ("0", 0),
        (4.0, 4),
        ("12K views", 12_000),
        ("12K Views", 12_000),
    ],
)
def test_parse_count_reads_shown_counts(value, expected):
    got = parse_count(value)
    assert got == expected
    assert type(got) is int


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "abc", True, False, -1, -2.0, math.nan, math.inf, "1.5", "1,5", "K", [3], {}],
)
def test_parse_count_rejects_non_counts(value):
    assert parse_count(value) is None


@pytest.mark.parametrize("value", ["1 204", "1 204", "1 204"])
def test_parse_count_space_thousands_separator(value):
    assert parse_count(value) == 1204


# --- normalise_counts ---


def test_normalise_counts_always_has_the_six_keys_and_missing_is_none():
    out = normalise_counts({"likes": "2.5K"})
    assert tuple(out) == COUNT_KEYS
    assert set(out) == {"replies", "reposts", "quotes", "likes", "views", "bookmarks"}
    assert out["likes"] == 2500
    for key in ("replies", "reposts", "quotes", "views", "bookmarks"):
        assert out[key] is None  # never 0 for a count that is not shown


@pytest.mark.parametrize(
    ("raw_key", "key"),
    [
        ("retweets", "reposts"),
        ("Retweets", "reposts"),
        ("comments", "replies"),
        ("quote tweets", "quotes"),
        ("Quote-Tweets", "quotes"),
        ("impressions", "views"),
        ("favorites", "likes"),
        ("saves", "bookmarks"),
    ],
)
def test_normalise_counts_maps_aliases(raw_key, key):
    out = normalise_counts({raw_key: "1,204"})
    assert out[key] == 1204
    assert sum(v is not None for v in out.values()) == 1


def test_normalise_counts_drops_unknown_keys_and_reads_unreadable_as_none():
    out = normalise_counts({"followers": "10K", "shares": 3, "likes": "lots"})
    assert out == dict.fromkeys(COUNT_KEYS)


@pytest.mark.parametrize("raw", [None, [], "12 likes", 5, "null"])
def test_normalise_counts_non_dict_is_all_none(raw):
    assert normalise_counts(raw) == dict.fromkeys(COUNT_KEYS)


def test_normalise_counts_keeps_an_explicit_zero():
    out = normalise_counts({"replies": 0, "likes": "0", "views": None})
    assert out["replies"] == 0
    assert out["likes"] == 0
    assert out["views"] is None


def test_normalise_counts_first_readable_alias_wins():
    out = normalise_counts({"reposts": "5", "retweets": "9"})
    assert out["reposts"] == 5


# --- handle / platform ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("@name", "name"),
        ("name", "name"),
        (" @name ", "name"),
        ("name.bsky.social", "name.bsky.social"),
        ("@Name.bsky.social", "Name.bsky.social"),
        (None, None),
        ("", None),
        ("@", None),
        ("not a handle", None),
        (123, None),
    ],
)
def test_normalise_handle(value, expected):
    assert normalise_handle(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Twitter", "x"),
        ("X (Twitter)", "x"),
        ("x", "x"),
        ("X", "x"),
        ("bsky", "bluesky"),
        ("Bluesky", "bluesky"),
        ("threads.net", "threads"),
        (None, "unknown"),
        ("", "unknown"),
        (42, "unknown"),
        ("unknown", "unknown"),
        ("myspace", "other"),
    ],
)
def test_normalise_platform(value, expected):
    assert normalise_platform(value) == expected


# --- screenshot_from_payload ---


def test_full_post_is_read_and_normalised():
    post = _post(FULL, media_hash="abc", latency_ms=42)
    assert post == ScreenshotPost(
        media_hash="abc",
        platform="x",
        display_name="Jane Doe",
        handle="janedoe",
        shown_time="3:14 PM · Nov 13, 2026",
        text="First line\nSecond line",
        counts={
            "replies": 37,
            "reposts": 1204,
            "quotes": None,
            "likes": 2500,
            "views": None,
            "bookmarks": None,
        },
        ui_oddities=["Mixed fonts in the text block"],
        confidence=0.82,
        model=VL_MODEL,
        latency_ms=42,
    )
    assert post.prompt_version == SCREENSHOT_PROMPT_VERSION


@pytest.mark.parametrize("is_post", [False, "false", "no", " False "])
def test_not_a_post_reads_as_none(is_post):
    # e.g. a chat screenshot: the model says is_post false, other fields are ignored
    assert _post({**FULL, "is_post": is_post}) is None


def test_chat_screenshot_is_none():
    chat = {"is_post": False, "platform": "other", "text": "hey are you coming tonight?"}
    assert _post(chat) is None


@pytest.mark.parametrize("is_post", ["true", "True", " yes "])
def test_is_post_string_is_accepted(is_post):
    assert _post({**FULL, "is_post": is_post}) is not None


@pytest.mark.parametrize("is_post", [None, 0, 1, "1", "maybe", [True]])
def test_missing_or_invalid_is_post_raises_so_the_read_is_retried(is_post):
    with pytest.raises(ValueError):
        _post({**FULL, "is_post": is_post})
    with pytest.raises(ValueError):
        _post({k: v for k, v in FULL.items() if k != "is_post"})


def test_text_is_verbatim_even_when_it_reads_like_null_or_is_long():
    assert _post({**FULL, "text": "None"}).text == "None"
    long = "word " * 3000
    assert _post({**FULL, "text": long}).text == long.strip()


def test_post_with_nothing_findable_is_none():
    payload = {**FULL, "handle": None, "display_name": "  ", "text": None}
    assert _post(payload) is None
    assert _post({"is_post": True, "display_name": None, "handle": None, "text": None}) is None


@pytest.mark.parametrize("missing", ["display_name", "handle", "text"])
def test_post_reply_missing_a_findable_key_raises_so_it_is_retried(missing):
    with pytest.raises(ValueError):
        _post({k: v for k, v in FULL.items() if k != missing})
    with pytest.raises(ValueError):
        _post({"is_post": True})


@pytest.mark.parametrize("bad", [3, ["a"], {"x": 1}, True])
def test_post_reply_with_a_non_string_findable_field_raises(bad):
    with pytest.raises(ValueError):
        _post({**FULL, "handle": bad})


def test_null_handle_with_text_is_still_a_post():
    payload = {**FULL, "handle": None}
    post = _post(payload)
    assert post is not None
    assert post.handle is None
    assert post.text == "First line\nSecond line"


def test_unreadable_handle_alone_does_not_make_a_post():
    empty = {"is_post": True, "display_name": None, "text": None}
    assert _post({**empty, "handle": "not a handle"}) is None
    assert _post({**empty, "handle": "@someone"}).handle == "someone"


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (0.82, 0.82),
        ("0.9", 0.9),
        (1.7, 1.0),
        (-0.3, 0.0),
        ("garbage", 0.5),
        (None, 0.5),
        ([0.9], 0.5),
        (math.nan, 0.5),
        (math.inf, 0.5),
    ],
)
def test_confidence_is_clamped_with_default(confidence, expected):
    assert _post({**FULL, "confidence": confidence}).confidence == expected


def test_confidence_missing_defaults_to_half():
    payload = {k: v for k, v in FULL.items() if k != "confidence"}
    assert _post(payload).confidence == 0.5


def test_ui_oddities_drop_claims_about_people_and_duplicates():
    oddities = [
        "Mixed fonts in the text block",
        "This account is a bot",
        "Looks like a paid shill",
        "Posted by spammers",
        "Mixed fonts in the text block",
        "Verified badge is misplaced",
        "Robot emoji badge next to the name",
        "none",
        "",
        42,
    ]
    post = _post({**FULL, "ui_oddities": oddities})
    assert post.ui_oddities == [
        "Mixed fonts in the text block",
        "Verified badge is misplaced",
        "Robot emoji badge next to the name",
    ]


def test_ui_oddities_single_string_and_garbage():
    assert _post({**FULL, "ui_oddities": "Misaligned text"}).ui_oddities == ["Misaligned text"]
    assert _post({**FULL, "ui_oddities": None}).ui_oddities == []
    assert _post({**FULL, "ui_oddities": {"a": 1}}).ui_oddities == []


def test_text_is_verbatim_with_line_breaks_and_stripped_ends():
    text = "  \n Hello   world!\r\nSecond  line\n\nThird \n  "
    post = _post({**FULL, "text": text})
    assert post.text == "Hello   world!\nSecond  line\n\nThird"


# --- end to end through parse_content ---

_REPLY_JSON = json.dumps(FULL)


@pytest.mark.parametrize(
    "content",
    [
        _REPLY_JSON,
        f"<think>The image shows a tweet {{maybe}}. Let me read it.</think>\n{_REPLY_JSON}",
        f"```json\n{_REPLY_JSON}\n```",
        f"Here is the JSON object:\n{_REPLY_JSON}\nLet me know if you need more.",
        f"<think>reasoning</think>\nSure:\n```json\n{_REPLY_JSON}\n```\nDone.",
    ],
)
def test_wrapped_replies_parse_to_the_same_post(content):
    assert _post(parse_content(content)) == _post(FULL)


# --- NemotronClient.read_screenshot ---


def test_read_screenshot_sends_screenshot_prompt_and_image():
    client, calls = _client(_response(_REPLY_JSON))
    client.read_screenshot(_media())
    sent = calls.calls[0]
    assert sent["messages"][0] == {"role": "system", "content": SCREENSHOT_SYSTEM_PROMPT}
    user = sent["messages"][1]["content"]
    assert user[0] == {"type": "text", "text": SCREENSHOT_USER_PROMPT}
    assert user[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert sent["model"] == client.model


def test_read_screenshot_defaults_reasoning_off_and_small_budget_even_when_main_is_on():
    client, calls = _client(_response(_REPLY_JSON), reasoning=True, max_tokens=4096)
    assert client.reasoning is True
    client.read_screenshot(_media())
    sent = calls.calls[0]
    assert sent["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert sent["max_tokens"] == client.screenshot_max_tokens
    assert sent["max_tokens"] != client.max_tokens


def test_read_screenshot_reasoning_on_sends_no_extra_body():
    client, calls = _client(
        _response(_REPLY_JSON), screenshot_reasoning=True, screenshot_max_tokens=900
    )
    client.read_screenshot(_media())
    sent = calls.calls[0]
    assert "extra_body" not in sent
    assert sent["max_tokens"] == 900


def test_read_screenshot_returns_post_with_response_model_and_media_hash():
    model = "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4"
    client, _ = _client(_response(_REPLY_JSON, model=model))
    post = client.read_screenshot(_media("the-hash"))
    assert isinstance(post, ScreenshotPost)
    assert post.model == model
    assert post.media_hash == "the-hash"
    assert post.handle == "janedoe"
    assert post.counts["likes"] == 2500
    assert isinstance(post.latency_ms, int)


def test_read_screenshot_non_post_is_none_and_cached():
    client, calls = _client(_response('{"is_post": false, "platform": "other"}'))
    assert client.read_screenshot(_media()) is None
    assert client.read_screenshot(_media()) is None
    assert len(calls.calls) == 1


@pytest.mark.parametrize("content", ["", None, "no json here", "{bad json", "[1, 2]"])
def test_read_screenshot_invalid_reply_is_nemotron_error(content):
    client, _ = _client(_response(content, finish="length"))
    with pytest.raises(NemotronError, match="invalid screenshot read") as exc:
        client.read_screenshot(_media())
    assert "finish_reason=length" in str(exc.value)
    assert not isinstance(exc.value, NotVisionModelError)


def test_read_screenshot_no_choices_is_nemotron_error():
    response = SimpleNamespace(model=VL_MODEL, choices=[], usage=None)
    client, _ = _client(response)
    with pytest.raises(NemotronError, match="no choices"):
        client.read_screenshot(_media())


@pytest.mark.parametrize(
    "model", ["private/nvidia/nemotron-3.5-super-120b-a12b", "nemotron-nano-vl", None]
)
def test_read_screenshot_rejects_non_super_vl_model(model):
    client, _ = _client(_response(_REPLY_JSON, model=model))
    with pytest.raises(NotVisionModelError):
        client.read_screenshot(_media())


def test_read_screenshot_is_cached_by_media_hash():
    client, calls = _client(_response(_REPLY_JSON))
    first = client.read_screenshot(_media("same"))
    second = client.read_screenshot(_media("same"))
    assert first is second
    assert len(calls.calls) == 1
    assert client.calls == 1
    client.read_screenshot(_media("other"))
    assert len(calls.calls) == 2


def test_read_screenshot_cache_is_separate_from_analyze_cache():
    client, calls = _client(_response(_REPLY_JSON))
    client.read_screenshot(_media("h"))
    assert "h" not in client._cache  # analyze's MediaAnalysis cache is untouched
    assert len(calls.calls) == 1


def test_read_screenshot_logs_task_screenshot(caplog):
    caplog.set_level(logging.INFO, logger="media.nemotron")
    client, _ = _client(_response(_REPLY_JSON))
    client.read_screenshot(_media("logged"))
    [record] = [r.nemotron_call for r in caplog.records if hasattr(r, "nemotron_call")]
    assert record["task"] == "screenshot"
    assert record["hash"] == "logged"
    assert record["outcome"] == "ok"
    assert record["returned_model"] == VL_MODEL


# --- ScreenshotPost detail round trip ---


def test_detail_round_trip():
    post = _post(FULL)
    detail = post.to_detail()
    assert json.loads(json.dumps(detail)) == detail  # JSON-ready
    assert detail["prompt_version"] == SCREENSHOT_PROMPT_VERSION
    assert set(detail["counts"]) == set(COUNT_KEYS)
    back = ScreenshotPost.from_detail(json.loads(json.dumps(detail)))
    assert back == post
    assert back.prompt_version == post.prompt_version


def test_from_detail_ignores_unknown_keys():
    detail = {**_post(FULL).to_detail(), "future_field": 1, "source": "x"}
    assert ScreenshotPost.from_detail(detail) == _post(FULL)


def test_parse_count_space_in_a_suffixed_count_is_rejected():
    assert parse_count("1 2K") is None


def test_ui_oddities_judging_dates_against_the_model_are_dropped():
    payload = {
        "is_post": True,
        "handle": "a",
        "display_name": None,
        "text": None,
        "ui_oddities": ["The date is in the future", "Mixed fonts in the second line"],
    }
    post = screenshot_from_payload(payload, media_hash="h", model="m", latency_ms=1)
    assert post.ui_oddities == ["Mixed fonts in the second line"]


def test_prompt_maps_unlisted_platforms_to_other_not_x():
    from media.screenshot import SCREENSHOT_SYSTEM_PROMPT

    assert "Truth Social" in SCREENSHOT_SYSTEM_PROMPT and 'never "x"' in SCREENSHOT_SYSTEM_PROMPT


@pytest.mark.parametrize("value", [0.4, 1.5, 2.999])
def test_fractional_numeric_count_is_not_rounded_into_a_count(value):
    assert parse_count(value) is None


def test_huge_suffixed_count_is_none_not_overflow():
    assert parse_count("9" * 309 + "K") is None


def test_literal_think_tags_inside_the_post_text_are_kept():
    reply = (
        '<think>reading {it}</think>\n{"is_post": true, "handle": "a", "display_name": null, '
        '"text": "Example <think>working</think> answer"}'
    )
    post = screenshot_from_payload(parse_content(reply), media_hash="h", model="m", latency_ms=1)
    assert post.text == "Example <think>working</think> answer"


def test_count_over_the_int_digit_limit_is_none_and_keeps_the_read():
    assert parse_count("1" * 5000) is None
    payload = {**FULL, "counts": {"likes": "1" * 5000, "replies": "12"}}
    post = _post(payload)
    assert post.counts["likes"] is None and post.counts["replies"] == 12
