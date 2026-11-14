import base64
import hashlib
import io
import json
import logging
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import openai
import pytest
from PIL import Image

from media.imagediff import CHANGE_KINDS, DEFAULT_LAYOUT, PROMPT_VERSION, describe_changes

MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"
RETURNED_MODEL = "nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4"
FULL = {
    "changes": ["The caption changed from 'X' to 'Y'."],
    "kinds": ["caption_changed"],
    "text_before": ["X"],
    "text_after": ["Y"],
    "content_flags": [],
    "confidence": 0.87654,
}


def image_bytes(size=(320, 180), mode="RGB", color="white", format="PNG"):
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format=format)
    return buffer.getvalue()


@pytest.fixture
def images():
    return image_bytes(), image_bytes(color="gray")


@pytest.fixture
def clock(monkeypatch):
    """A controlled monotonic clock for media.imagediff (tests advance ``clock[0]``)."""
    import media.imagediff as imagediff

    now = [1000.0]
    monkeypatch.setattr(imagediff, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def slow_decoding(monkeypatch, clock, seconds):
    import media.imagediff as imagediff

    real = imagediff._image

    def decode(data):
        clock[0] += seconds
        return real(data)

    monkeypatch.setattr(imagediff, "_image", decode)


def response(content, model=RETURNED_MODEL, finish="stop"):
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=None,
    )


class FakeClient:
    def __init__(self, result):
        self.model = MODEL
        self.calls = 0
        self.requests = []
        self._limiter = SimpleNamespace(wait=Mock())
        self._client = SimpleNamespace(chat=SimpleNamespace(completions=self))
        self.result = result

    def create(self, **kwargs):
        self._limiter.wait.assert_called_once_with()
        assert self.calls == 1
        self.requests.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def compare(images, payload=None, **kwargs):
    client = FakeClient(response(json.dumps(FULL if payload is None else payload)))
    kwargs.setdefault("layout", "pair")
    result = describe_changes(client, *images, timeout_s=2.5, **kwargs)
    assert result is not None
    return result, client


def sent_images(client):
    parts = client.requests[0]["messages"][1]["content"]
    decoded = []
    for part in parts:
        if part["type"] == "image_url":
            url = part["image_url"]["url"]
            assert url.startswith("data:image/jpeg;base64,")
            image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
            image.load()
            assert image.format == "JPEG"
            decoded.append(image)
    return decoded


def red_pixels(image):
    pixels = image.convert("RGB").tobytes()
    return sum(
        red > 160 and green < 100 and blue < 100
        for red, green, blue in zip(pixels[::3], pixels[1::3], pixels[2::3], strict=True)
    )


def test_happy_path(images):
    result, client = compare(images)
    assert result.changes == FULL["changes"]
    assert result.kinds == FULL["kinds"]
    assert result.text_before == ["X"]
    assert result.text_after == ["Y"]
    assert result.confidence == 0.8765
    assert result.model == RETURNED_MODEL
    assert isinstance(result.latency_ms, int) and result.latency_ms >= 0
    assert client.calls == len(client.requests) == 1
    assert PROMPT_VERSION == "imagediff-v1"
    assert DEFAULT_LAYOUT == "composite"
    assert len(CHANGE_KINDS) == 10
    with pytest.raises(FrozenInstanceError):
        result.model = "changed"


@pytest.mark.parametrize(
    "wrapper",
    [
        "```json\n%s\n```",
        '<think>{"ignore": 1}</think>\n%s',
        "Here is the comparison:\n%s\nThat is all.",
    ],
)
def test_parse_wrappers(images, wrapper):
    client = FakeClient(response(wrapper % json.dumps(FULL)))
    result = describe_changes(client, *images, timeout_s=2)
    assert result is not None and result.changes == FULL["changes"]


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        (" Caption ", "caption_changed"),
        ("crop", "cropped"),
        ("COLOR", "recolored"),
        ("colour", "recolored"),
        ("recolor", "recolored"),
        ("face", "face_changed"),
        ("face_swap", "face_changed"),
        ("added_text", "text_added"),
        ("logo", "watermark"),
        ("handle", "watermark"),
        ("unrecognised", "other"),
    ],
)
def test_kind_aliases(images, alias, expected):
    result, _ = compare(images, {**FULL, "kinds": [alias]})
    assert result.kinds == [expected]


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        ([], ["other", "other"]),
        (["crop"], ["cropped", "other"]),
        (["crop", "face", "logo"], ["cropped", "face_changed"]),
        (["", "face"], ["other", "face_changed"]),
    ],
)
def test_kind_lengths(images, kinds, expected):
    result, _ = compare(
        images, {**FULL, "changes": ["One change", "Another change"], "kinds": kinds}
    )
    assert result.kinds == expected


def test_changes_cleaning_and_alignment(images):
    result, _ = compare(
        images,
        {
            **FULL,
            "changes": [
                " ",
                " A   hat\nwas added ",
                "A hat was added.",
                None,
                "The image was cropped",
            ],
            "kinds": ["face", "element_added", "logo", "face", "crop"],
        },
    )
    assert result.changes == ["A hat was added.", "The image was cropped."]
    assert result.kinds == ["element_added", "cropped"]


def test_five_changes_cap(images):
    result, _ = compare(
        images,
        {**FULL, "changes": [f"Change {index}" for index in range(9)], "kinds": ["crop"] * 9},
    )
    assert result.changes == [f"Change {index}." for index in range(5)]
    assert result.kinds == ["cropped"] * 5


@pytest.mark.parametrize(
    "word",
    [
        "deepfake",
        "deep fakes",
        "fake",
        "fakes",
        "faked",
        "real",
        "authentic",
        "genuine",
        "bots",
        "shills",
        "trolls",
        "propaganda",
    ],
)
def test_forbidden_words_drop_aligned_kind(images, word):
    result, _ = compare(
        images,
        {
            **FULL,
            "changes": [f"It is {word.upper()}", "A hat was added"],
            "kinds": ["face", "element_added"],
        },
    )
    assert result.changes == ["A hat was added."]
    assert result.kinds == ["element_added"]


def test_forbidden_guard_uses_word_boundaries(images):
    result, _ = compare(images, {**FULL, "changes": ["A surreal robot drawing was added"]})
    assert result.changes == ["A surreal robot drawing was added."]


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"content_flags": ["adult"]}, "Adult content."),
        ({"changes": ["A nude figure was added"]}, "Adult content."),
        ({"text_before": ["naked"]}, "Adult content."),
        ({"text_after": ["porn"]}, "Adult content."),
        ({"changes": ["A fake nude figure was added"]}, "Adult content."),
        ({"content_flags": ["graphic"]}, "Graphic content."),
        ({"content_flags": ["graphic", "adult"]}, "Adult content."),
    ],
)
def test_content_guards(images, updates, expected):
    result, _ = compare(images, {**FULL, **updates})
    assert result.changes == [expected]
    assert result.kinds == ["other"]
    assert result.text_before == result.text_after == []


def test_identical(images):
    result, _ = compare((images[0], images[0]), {**FULL, "changes": [], "kinds": ["crop"]})
    assert result.changes == result.kinds == []


def test_text_bounds_and_scalar_strings(images):
    result, _ = compare(
        images,
        {
            **FULL,
            "changes": "A hat was added",
            "kinds": "element_added",
            "text_before": ["z" * 400] * 30,
            "text_after": "  Hello  ",
        },
    )
    assert result.changes == ["A hat was added."]
    assert result.kinds == ["element_added"]
    assert result.text_before == ["z" * 300] * 20
    assert result.text_after == ["Hello"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("junk", 0.5),
        (None, 0.5),
        (True, 0.5),
        (False, 0.5),
        ({}, 0.5),
        (float("nan"), 0.5),
        (float("inf"), 0.5),
        (-3, 0),
        (8, 1),
        ("0.23456", 0.2346),
    ],
)
def test_confidence(images, value, expected):
    result, _ = compare(images, {**FULL, "confidence": value})
    assert result.confidence == expected


@pytest.mark.parametrize("model", ["nemotron-3.5-super", "other-vl", None])
def test_wrong_model(images, model, caplog):
    client = FakeClient(response(json.dumps(FULL), model=model))
    assert describe_changes(client, *images, timeout_s=2) is None
    assert client.calls == 1
    assert "outcome=NotVisionModelError" in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        openai.APITimeoutError(request=httpx.Request("POST", "https://example.test")),
        openai.OpenAIError("unavailable"),
        RuntimeError("broken"),
    ],
)
def test_call_failure_is_not_retried(images, error, caplog):
    client = FakeClient(error)
    assert describe_changes(client, *images, timeout_s=2) is None
    assert len(client.requests) == client.calls == 1
    records = [
        record.nemotron_call for record in caplog.records if hasattr(record, "nemotron_call")
    ]
    assert len(records) == 1
    assert records[0]["task"] == "imagediff"
    assert records[0]["outcome"] == records[0]["error_type"] == type(error).__name__


@pytest.mark.parametrize("bad_index", [0, 1])
def test_garbage_images_make_no_call(images, bad_index, caplog):
    inputs = list(images)
    inputs[bad_index] = b"not an image"
    client = FakeClient(response(json.dumps(FULL)))
    assert describe_changes(client, *inputs, timeout_s=2) is None
    assert client.calls == 0 and client.requests == []
    client._limiter.wait.assert_not_called()
    assert "image comparison unavailable" in caplog.text


@pytest.mark.parametrize(
    "content",
    ["", None, "not JSON", "{}", '{"changes": {}}', '{"changes": null}', '{"changes": ["broken"'],
)
def test_unusable_content(images, content):
    client = FakeClient(response(content))
    assert describe_changes(client, *images, timeout_s=2) is None


def test_no_choices(images):
    client = FakeClient(SimpleNamespace(model=MODEL, choices=[], usage=None))
    assert describe_changes(client, *images, timeout_s=2) is None


@pytest.mark.parametrize(
    "content",
    [
        '{"changes": ["A hat was added"], "confidence":',
        '{"changes": [], "confidence":',
        '{"changes": ["An exposed wound was added."], "content_flags": ["graphic"',
        json.dumps(FULL),  # even complete JSON: a cut reply is a malfunction
    ],
)
def test_truncated_reply_is_never_used(images, content):
    """A repaired cut could drop an unfinished content_flags, i.e. the safety guard."""
    client = FakeClient(response(content, finish="length"))
    assert describe_changes(client, *images, timeout_s=2) is None


@pytest.mark.parametrize("layout", ["pair", "composite"])
def test_request_contract(images, layout, clock):
    _, client = compare(images, layout=layout, max_tokens=1234)
    request = client.requests[0]
    assert request["model"] == MODEL
    assert request["timeout"] == 2.5
    assert request["max_tokens"] == 1234
    assert request["temperature"] == 0.1
    assert request["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    schema = request["response_format"]
    assert schema["type"] == "json_schema" and schema["json_schema"]["name"] == "image_diff"
    assert set(schema["json_schema"]["schema"]["required"]) == set(FULL)
    assert len(sent_images(client)) == (2 if layout == "pair" else 1)
    text = request["messages"][1]["content"][0]["text"]
    assert "red box" not in text  # no edited_region: the model is not told about a box
    system = request["messages"][0]["content"]
    assert "rectangle" not in system and "red box" not in system
    assert "earlier/original" in text and "later/variant" in text


@pytest.mark.parametrize(
    ("region", "told"), [((0.2, 0.2, 0.8, 0.8), True), ((0.5, 0.5, 0.5, 0.7), False)]
)
def test_marker_note_only_when_box_drawn(images, region, told):
    _, client = compare(images, edited_region=region)
    text = client.requests[0]["messages"][1]["content"][0]["text"]
    assert ("red box" in text and "not part of the image" in text) is told


def test_default_layout(images):
    client = FakeClient(response(json.dumps(FULL)))
    assert describe_changes(client, *images, timeout_s=2) is not None
    assert len(sent_images(client)) == 1  # composite: one side-by-side image


@pytest.mark.parametrize("size", [(1024, 1), (1024, 1024), (1, 1024)])
def test_composite_width_bound(size):
    data = image_bytes(size)
    _, client = compare((data, data), layout="composite")
    assert sent_images(client)[0].width <= 2048


def test_composite_panels_share_height():
    _, client = compare(
        (image_bytes((640, 320), color="black"), image_bytes((240, 240), color="blue")),
        layout="composite",
    )
    composite = sent_images(client)[0]
    assert composite.size == (480 + 16 + 240, 240 + 40)
    for vertical in (45, composite.height - 1):
        assert composite.getpixel((200, vertical)) == (0, 0, 0)
        assert composite.getpixel((600, vertical))[2] > 240


def test_composite_dimensions_and_labels():
    _, client = compare((image_bytes((2000, 400)), image_bytes((1000, 100))), layout="composite")
    composite = sent_images(client)[0]
    assert composite.width <= 2048 and composite.mode == "RGB"
    left_width = 5 * (composite.height - 40)
    left_label = composite.crop((0, 0, left_width, 40))
    right_label = composite.crop((left_width + 16, 0, composite.width, 40))
    assert left_label.convert("L").getextrema()[0] < 100
    assert right_label.convert("L").getextrema()[0] < 100
    assert (
        composite.crop((0, 45, composite.width, composite.height)).getextrema() == ((255, 255),) * 3
    )


@pytest.mark.parametrize("layout", ["pair", "composite"])
@pytest.mark.parametrize(
    "region",
    [
        None,
        (0.2, 0.2, 0.8, 0.8),
        (-1, -1, 2, 2),
        (0.5, 0.5, 0.5, 0.7),
        (0.8, 0.2, 0.1, 0.7),
        (float("nan"), 0, 1, 1),
        ("bad", 0, 1, 1),
    ],
)
def test_red_box_only_on_variant(images, layout, region):
    _, client = compare(images, layout=layout, edited_region=region)
    sent = sent_images(client)
    expected = region in [(0.2, 0.2, 0.8, 0.8), (-1, -1, 2, 2)]
    assert (red_pixels(sent[-1]) > 0) is expected
    original = sent[0] if layout == "pair" else sent[0].crop((0, 0, 320, sent[0].height))
    assert red_pixels(original) == 0


@pytest.mark.parametrize(
    ("mode", "color", "format"),
    [
        ("RGBA", (255, 0, 0, 0), "PNG"),
        ("LA", (0, 0), "PNG"),
        ("P", 0, "GIF"),
        ("RGB", "white", "WEBP"),
    ],
)
def test_input_formats(images, mode, color, format):
    data = image_bytes(mode=mode, color=color, format=format)
    _, client = compare((data, images[1]))
    image = sent_images(client)[0]
    assert image.mode == "RGB"
    if mode in ("RGBA", "LA"):
        assert image.getpixel((100, 100)) == (255, 255, 255)


@pytest.mark.parametrize("format", ["GIF", "WEBP"])
def test_animated_input_uses_first_frame(images, format):
    buffer = io.BytesIO()
    Image.new("RGB", (80, 80), "white").save(
        buffer,
        format=format,
        save_all=True,
        append_images=[Image.new("RGB", (80, 80), "black")],
        duration=100,
        loop=0,
    )
    _, client = compare((buffer.getvalue(), images[1]))
    assert sent_images(client)[0].getpixel((40, 40)) == (255, 255, 255)


def test_pair_downscales():
    _, client = compare((image_bytes((3000, 2000)), image_bytes((1600, 3200))))
    assert [image.size for image in sent_images(client)] == [(1024, 683), (512, 1024)]


def test_success_log_matches_result(images, caplog):
    caplog.set_level(logging.INFO)
    result, _ = compare(images)
    records = [
        record.nemotron_call for record in caplog.records if hasattr(record, "nemotron_call")
    ]
    assert len(records) == 1
    record = records[0]
    assert record["task"] == "imagediff" and record["outcome"] == "ok"
    assert record["latency_ms"] == result.latency_ms
    assert record["model"] == MODEL and record["returned_model"] == RETURNED_MODEL
    assert len(record["hash"]) == 16


def test_limiter_exception_is_contained(images):
    client = FakeClient(response(json.dumps(FULL)))
    client._limiter.wait.side_effect = RuntimeError("limiter failed")
    assert describe_changes(client, *images, timeout_s=2) is None
    assert client.calls == 0


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_makes_no_call(images, timeout):
    client = FakeClient(response(json.dumps(FULL)))
    assert describe_changes(client, *images, timeout_s=timeout) is None
    assert client.calls == 0


class SequenceClient(FakeClient):
    """Returns (or raises) one queued result per request."""

    def create(self, **kwargs):
        self.requests.append(kwargs)
        result = self.result.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def server_error():
    request = httpx.Request("POST", "https://example.test")
    return openai.InternalServerError(
        "Internal server error", response=httpx.Response(500, request=request), body=None
    )


def call_records(caplog):
    return [r.nemotron_call for r in caplog.records if hasattr(r, "nemotron_call")]


def test_server_error_retried_once_within_budget(images, caplog):
    caplog.set_level(logging.INFO)
    client = SequenceClient([server_error(), response(json.dumps(FULL))])
    result = describe_changes(client, *images, timeout_s=20)
    assert result is not None and result.changes == FULL["changes"]
    assert client.calls == len(client.requests) == 2
    assert client.requests[1]["timeout"] < 20
    assert client._limiter.wait.call_count == 2  # the retry takes its own limiter slot
    records = call_records(caplog)
    assert [(r["attempt"], r["outcome"]) for r in records] == [(1, "retry"), (2, "ok")]


def test_second_server_error_returns_none(images, caplog):
    client = SequenceClient([server_error(), server_error()])
    assert describe_changes(client, *images, timeout_s=20) is None
    assert client.calls == 2
    outcomes = [r["outcome"] for r in call_records(caplog)]
    assert outcomes == ["retry", "InternalServerError"]


def test_server_error_not_retried_without_budget(images):
    client = SequenceClient([server_error(), response(json.dumps(FULL))])
    assert describe_changes(client, *images, timeout_s=2) is None
    assert client.calls == 1


@pytest.mark.parametrize(
    ("region", "drawn"),
    [
        (None, False),
        ((0.1, 0.1, 0.5, 0.5), True),
        ((0.5, 0.5, 0.5, 0.5), False),
        ((float("nan"), 0, 1, 1), False),
    ],
)
def test_marker_box_change_dropped_only_when_drawn(images, region, drawn):
    payload = {
        **FULL,
        "changes": ["A red rectangular outline was added around the caption.", *FULL["changes"]],
        "kinds": ["element_added", "caption_changed"],
    }
    result, _ = compare(images, payload, edited_region=region)
    if drawn:
        assert result.changes == FULL["changes"] and result.kinds == ["caption_changed"]
    else:
        assert result.kinds == ["element_added", "caption_changed"]


@pytest.mark.parametrize(
    "sentence",
    ["A red hat was added inside a blue frame.", "The red car near the border was removed."],
)
def test_marker_guard_keeps_other_red_edits(images, sentence):
    payload = {**FULL, "changes": [sentence], "kinds": ["element_added"]}
    result, _ = compare(images, payload, edited_region=(0.1, 0.1, 0.5, 0.5))
    assert result.changes == [sentence]


def test_budget_covers_limiter_wait(images, clock):
    """timeout_s starts at the call: a limiter wait that would use it up means no request."""
    client = FakeClient(response(json.dumps(FULL)))
    client._limiter._next = clock[0] + 5
    assert describe_changes(client, *images, timeout_s=1) is None
    assert client.requests == [] and client.calls == 0
    client._limiter.wait.assert_not_called()


def test_request_timeout_is_what_is_left(images, monkeypatch, clock):
    slow_decoding(monkeypatch, clock, 0.4)  # two images: 0.8 s of a 2.5 s budget
    _, client = compare(images)
    assert client.requests[0]["timeout"] == pytest.approx(1.7)


def test_spent_budget_reserves_no_slot_and_logs_nothing(images, monkeypatch, clock, caplog):
    slow_decoding(monkeypatch, clock, 1.0)
    client = FakeClient(response(json.dumps(FULL)))
    assert describe_changes(client, *images, timeout_s=1.5) is None
    assert client.requests == [] and client.calls == 0
    client._limiter.wait.assert_not_called()
    assert call_records(caplog) == []


def test_reply_after_deadline_is_dropped(images, clock, caplog):
    class LateClient(FakeClient):
        def create(self, **kwargs):
            self.requests.append(kwargs)
            clock[0] += 3.0  # a reply that trickled in past the 2 s budget
            return self.result

    client = LateClient(response(json.dumps(FULL)))
    assert describe_changes(client, *images, timeout_s=2) is None
    records = call_records(caplog)
    assert len(records) == 1 and records[0]["outcome"] == "TimeoutError"


def test_client_without_model_returns_none(images):
    client = FakeClient(response(json.dumps(FULL)))
    del client.model
    assert describe_changes(client, *images, timeout_s=2) is None


def test_malformed_choices_do_not_break_failure_log(images, caplog):
    bad = SimpleNamespace(model=RETURNED_MODEL, choices={"invalid": 1}, usage=None)
    client = FakeClient(bad)
    assert describe_changes(client, *images, timeout_s=2) is None
    records = call_records(caplog)
    assert len(records) == 1 and records[0]["outcome"] == "KeyError"


def test_oversized_image_refused_before_decoding(images):
    buffer = io.BytesIO()
    Image.new("1", (6000, 5000)).save(buffer, format="PNG")  # 30 MP, a few KB compressed
    client = FakeClient(response(json.dumps(FULL)))
    assert describe_changes(client, buffer.getvalue(), images[1], timeout_s=2) is None
    assert client.requests == []


def test_large_jpeg_is_drafted_down(images, monkeypatch):
    """JPEG draft decodes at a reduced scale before any full-size copy is made."""
    import media.imagediff as imagediff

    seen = []
    real = imagediff.ImageOps.exif_transpose

    def record(image):
        out = real(image)
        seen.append(out.size)
        return out

    monkeypatch.setattr(imagediff.ImageOps, "exif_transpose", record)
    buffer = io.BytesIO()
    Image.new("RGB", (4000, 3000), "gray").save(buffer, format="JPEG")
    _, client = compare((buffer.getvalue(), images[1]), layout="pair")
    assert seen[0][0] <= 2000  # drafted (JPEG scales by 1/2..1/8), not 4000 wide
    assert max(sent_images(client)[0].size) <= 1024


@pytest.mark.parametrize("mode", ["L", "RGB"])
def test_colour_key_transparency_survives_downscaling(images, mode):
    """A tRNS key (no alpha band) must not blend into visible pixels when resized."""
    image = Image.new(mode, (2048, 2048), "white")
    pixels = image.load()
    black = 0 if mode == "L" else (0, 0, 0)
    for y in range(0, 2048, 2):
        for x in range(0, 2048, 2):
            pixels[x, y] = black  # transparent: the whole image should read as white
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", transparency=black)
    _, client = compare((buffer.getvalue(), images[1]), layout="pair")
    sent = sent_images(client)[0].convert("L")
    assert sent.getextrema()[0] > 230


@pytest.mark.parametrize(
    "sentence",
    [
        "The caption changed from 'REAL NEWS' to 'FAKE NEWS'.",
        "The caption changed from 'DON'T TRUST BOTS' to 'TRUST THE PROPAGANDA'.",
        'The text "deepfake alert" was added at the top.',
        "The caption changed from \u2018REAL\u2019 to \u2018FAKE\u2019.",
    ],
)
def test_forbidden_words_inside_quoted_image_text_are_kept(images, sentence):
    payload = {
        **FULL,
        "changes": [sentence],
        "kinds": ["caption_changed"],
        "text_before": ["REAL NEWS", "DON'T TRUST BOTS", "REAL"],
        "text_after": ["FAKE NEWS", "TRUST THE", "PROPAGANDA", "deepfake alert!", "FAKE"],
    }
    result, _ = compare(images, payload)
    assert result.changes == [sentence] and result.kinds == ["caption_changed"]


@pytest.mark.parametrize(
    "sentence",
    [
        "The face was replaced with a fake one.",
        "The dog's 'GOOD BOY' caption looks fake.",
        "A real 'WARNING' label was added.",
        'The face was replaced with a "fake" one.',  # scare quotes, not image text
        '"The face was replaced with a fake one."',  # quoted narration
        "The caption changed to 'FAKE'.",  # 'FAKE' is not in the transcription
        "The caption 'GOOD BOY' became a 'fake' label.",  # one word of a line is not a line
    ],
)
def test_forbidden_words_outside_quotes_still_drop(images, sentence):
    payload = {**FULL, "changes": [sentence], "kinds": ["other"], "text_after": ["GOOD BOY"]}
    result, _ = compare(images, payload)
    assert result.changes == []


def test_composite_width_capped_with_padded_narrow_panel():
    tall, wide = image_bytes((20, 1000)), image_bytes((1000, 300))
    _, client = compare((tall, wide), layout="composite")
    assert sent_images(client)[0].width <= 2048


def test_red_box_drawn_after_composite_scaling():
    """B is shrunk to A's height in the composite; the box must stay visible (>= 3 px)."""
    short, tall = image_bytes((1000, 250)), image_bytes((800, 1000), color="gray")
    _, client = compare((short, tall), layout="composite", edited_region=(0.1, 0.1, 0.9, 0.9))
    composite = sent_images(client)[0].convert("RGB")
    row = composite.height // 2
    reds = [
        x
        for x in range(composite.width)
        if (lambda p: p[0] > 160 and p[1] < 100 and p[2] < 100)(composite.getpixel((x, row)))
    ]
    assert len(reds) >= 6  # two vertical edges, each at least 3 px wide


def test_media_hash_separates_inputs(caplog):
    caplog.set_level(logging.INFO)
    a, b = image_bytes(color="white"), image_bytes(color="gray")
    compare((a, b))  # hashed per input, so (a+x, y) and (a, x+y) can't collide
    hashes = [r["hash"] for r in call_records(caplog)]
    assert len(hashes) == 1

    joined = hashlib.sha256(hashlib.sha256(a).digest() + hashlib.sha256(b).digest()).hexdigest()[
        :16
    ]
    assert hashes[0] == joined


def test_not_vision_model_is_logged_as_error(images, caplog):
    client = FakeClient(response(json.dumps(FULL), model="other-model"))
    assert describe_changes(client, *images, timeout_s=2) is None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors and "NotVisionModelError" in errors[0].getMessage()


def test_retry_skipped_when_limiter_slot_does_not_fit(images, clock):
    client = SequenceClient([server_error(), response(json.dumps(FULL))])
    client._limiter.wait.side_effect = lambda: setattr(client._limiter, "_next", clock[0] + 30)
    assert describe_changes(client, *images, timeout_s=20) is None
    assert client.calls == 1


def test_no_request_with_less_than_min_budget(images, monkeypatch, clock):
    slow_decoding(monkeypatch, clock, 0.8)  # 1.6 s of 2.0 s: 0.4 s left (< 0.5 s)
    client = FakeClient(response(json.dumps(FULL)))
    assert describe_changes(client, *images, timeout_s=2.0) is None
    assert client.requests == []


def test_quoted_marker_not_exempt(images):
    payload = {
        **FULL,
        "changes": ["A 'red box' was added around the caption.", *FULL["changes"]],
        "kinds": ["element_added", "caption_changed"],
    }
    result, _ = compare(images, payload, edited_region=(0.1, 0.1, 0.5, 0.5))
    assert result.changes == FULL["changes"]


def test_ico_is_refused_before_decoding(images, monkeypatch):
    """ICO decodes inside Image.open (before any size check): not an accepted format."""
    from PIL import IcoImagePlugin

    decoded = []
    real = IcoImagePlugin.IcoFile.getimage

    def record(self, *args, **kwargs):
        decoded.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(IcoImagePlugin.IcoFile, "getimage", record)
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buffer, format="ICO")
    client = FakeClient(response(json.dumps(FULL)))
    assert describe_changes(client, buffer.getvalue(), images[1], timeout_s=2) is None
    assert decoded == [] and client.requests == []


@pytest.mark.parametrize("format", ["JPEG", "PNG", "GIF", "WEBP"])
def test_meme_formats_accepted(images, format):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buffer, format=format)
    compare((buffer.getvalue(), images[1]))


def test_min_budget_rechecked_after_limiter_wait(images, clock):
    """The limiter's real wait can overshoot its plan: re-check right before the request."""
    client = FakeClient(response(json.dumps(FULL)))
    client._limiter.wait.side_effect = lambda: clock.__setitem__(0, clock[0] + 1.7)
    assert describe_changes(client, *images, timeout_s=2.0) is None  # 0.3 s left after it
    assert client.requests == [] and client.calls == 0
