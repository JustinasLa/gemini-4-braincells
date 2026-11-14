"""Decision gate (analyst/decide.py) and its routing in analyst/runner.py, Gemini mocked."""

from types import SimpleNamespace

import pytest

from analyst.agent import GeminiAgent
from analyst.decide import API_VERSION, DecisionGate
from analyst.gemini import GeminiVerdictModel
from analyst.runner import Analyst
from tests.test_analyst import ctx
from tests.test_analyst_agent import FakeGemini, FakeWeb, call, reply, verdict_json


class FakeDecisions:
    def __init__(self, p_web: float | None = None, *, fail: bool = False):
        self.p_web, self.fail = p_web, fail
        self.requests: list[dict] = []
        self.decisions = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.fail:
            raise RuntimeError("404")
        return SimpleNamespace(
            answers={"needs_web": SimpleNamespace(noul=self.p_web)},
            usage=SimpleNamespace(total_input_tokens=1234),
        )


def analyst(decisions, *, agent_replies=(), fast_replies=(), tools=True):
    agent_gemini = FakeGemini(*agent_replies)
    fast_gemini = FakeGemini(*fast_replies)
    a = Analyst(
        agent=GeminiAgent("gemini-4-hydrogen", client=agent_gemini),
        batch=None,
        web=FakeWeb(),
        tools=tools,
        gate=DecisionGate("gemini-decision-1", client=decisions, threshold=0.1),
        fast=GeminiVerdictModel("", "gemini-4-hydrogen", thinking_level="LOW", client=fast_gemini),
    )
    return a, agent_gemini, fast_gemini


def test_gate_asks_decision_model_on_v1beta():
    d = FakeDecisions(0.05)
    g = DecisionGate("models/gemini-decision-1", client=d).check("sys", "prompt")
    req = d.requests[0]
    assert req["api_version"] == API_VERSION == "v1beta"
    assert req["model"] == "models/gemini-decision-1"
    assert req["input"] == "sys\n\nprompt"
    assert req["questions"]["needs_web"]["type"] == "noul"
    assert g.p_web == 0.05 and g.skip_tools and g.input_tokens == 1234


def test_low_p_web_skips_tool_loop_with_low_thinking():
    a, agent_gemini, fast_gemini = analyst(
        FakeDecisions(0.03), fast_replies=[reply(verdict_json())]
    )
    v = a.verdict(None, ctx())
    assert agent_gemini.configs == []  # agent never ran
    cfg = fast_gemini.configs[0]
    assert cfg.tools is None and cfg.thinking_config.thinking_level == "LOW"
    assert v.model == "gemini-4-hydrogen" and v.llm_assessment == "coordinated"
    assert v.gate["route"] == "single" and v.gate["p_web"] == 0.03
    assert len(v.model_calls) == 1 and v.model_calls[0]["function_calls"] == 0
    assert v.tool_calls == []


@pytest.mark.parametrize("decisions", [FakeDecisions(0.6), FakeDecisions(fail=True)])
def test_high_p_web_or_gate_failure_runs_the_agent(decisions):
    a, agent_gemini, fast_gemini = analyst(
        decisions,
        agent_replies=[reply(call("search_web", query="q", why="w")), reply(verdict_json())],
    )
    v = a.verdict(None, ctx())
    assert fast_gemini.configs == [] and len(agent_gemini.configs) == 2
    assert agent_gemini.configs[0].thinking_config is None  # agent keeps default thinking
    assert v.gate["route"] == "agent"
    assert v.gate.get("error") is (True if decisions.fail else None)
    assert [t["name"] for t in v.tool_calls] == ["search_web"]


def test_gate_is_skipped_without_tools_or_evidence():
    d = FakeDecisions(0.01)
    a, agent_gemini, _ = analyst(d, agent_replies=[reply(verdict_json())], tools=False)
    v = a.verdict(None, ctx())
    assert d.requests == [] and v.gate is None and len(agent_gemini.configs) == 1

    a, agent_gemini, fast_gemini = analyst(d)
    v = a.verdict(None, ctx(evidence=False))
    assert d.requests == [] and v.model == "none"
    assert agent_gemini.configs == fast_gemini.configs == []


def test_likely_organic_still_goes_to_batch_without_gate():
    d = FakeDecisions(0.01)
    a, *_ = analyst(d)
    a.batch = GeminiVerdictModel(
        "", "batch-model", client=FakeGemini(reply(verdict_json(status="likely_organic")))
    )
    v = a.verdict(None, ctx(status="likely_organic"))
    assert v.model == "batch-model" and d.requests == [] and v.gate is None


def test_from_settings_wires_gate(monkeypatch):
    from common.config import get_settings

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()
    try:
        a = Analyst.from_settings(None)
        assert a.gate.name == "gemini-decision-1" and a.gate.threshold == 0.1
        assert a.fast.name == a.agent.name and a.fast._thinking_level == "LOW"
        monkeypatch.setenv("GEMINI_DECISION_MODEL", "")
        get_settings.cache_clear()
        a = Analyst.from_settings(None)
        assert a.gate is None and a.fast is None
    finally:
        get_settings.cache_clear()
