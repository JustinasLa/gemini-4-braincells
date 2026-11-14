from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from media import compare, compare_screenshot


@pytest.mark.parametrize(
    "module,variant,content",
    [
        (compare, "v3", '{"description":"A poster"}'),
        (compare_screenshot, "off", '{"is_post":false}'),
    ],
)
def test_empty_choices_are_counted_without_aborting_comparison(module, variant, content):
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=0)
    empty = SimpleNamespace(model="nemotron-3.5-super-vl", choices=[], usage=usage)
    valid = SimpleNamespace(
        model=empty.model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
        usage=None,
    )
    create = Mock(side_effect=[empty, valid])
    client = SimpleNamespace(
        model=empty.model,
        _client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    item = {"cid": "hash", "data_url": "data:image/png;base64,eA=="}

    failed = module._one(client, module.VARIANTS[variant], item)
    succeeded = module._one(client, module.VARIANTS[variant], item | {"cid": "next"})

    assert failed["error"] == "NoChoices"
    assert failed["cid"] == "hash"
    assert not failed["ok"]
    assert failed["model"] == empty.model
    assert failed["latency_ms"] >= 0
    assert failed["prompt_tokens"] == 10
    assert succeeded["ok"] and succeeded["cid"] == "next"
    rows = [failed, succeeded]
    metrics = module._metrics(rows) if module is compare else module._metrics(rows, {})
    assert metrics["n"] == 2
    assert metrics["errors"] == 1
    assert metrics["valid_json"] == "1/2"
    assert create.call_count == 2
