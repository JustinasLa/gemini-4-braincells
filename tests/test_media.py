from types import SimpleNamespace

import pytest

from media.nemotron import MediaInput, NemotronClient, NemotronError


class FakeCompletions:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )


class FakeClient:
    def __init__(self, content):
        self.chat = SimpleNamespace(completions=FakeCompletions(content))


def test_analyze_uses_data_url_and_caches_by_hash():
    fake = FakeClient(
        '{"description":"A reused poster","extracted_text":["READ"],'
        '"reuse_cues":["same layout"],"manipulation_cues":[],"confidence":0.8}'
    )
    client = NemotronClient("test", client=fake)
    media = MediaInput("hash-1", "image/png", data=b"png", source_uri="at://post")

    first = client.analyze(media)
    second = client.analyze(media)

    assert first == second
    assert first.evidence_detail()["model"] == client.model
    assert fake.chat.completions.calls[0]["messages"][1]["content"][1]["image_url"]["url"].startswith( # noqa: E501
        "data:image/png;base64,"
    )
    assert len(fake.chat.completions.calls) == 1


def test_analyze_rejects_malformed_model_output():
    fake = FakeClient('{"description":"","confidence":2}')
    client = NemotronClient("test", client=fake)

    with pytest.raises(NemotronError, match="invalid analysis"):
        client.analyze(MediaInput("hash-2", "image/jpeg", url="https://example.test/a.jpg"))


def test_media_requires_a_url_or_bytes():
    with pytest.raises(ValueError, match="either url or data"):
        MediaInput("hash-3", "image/jpeg").image_url()