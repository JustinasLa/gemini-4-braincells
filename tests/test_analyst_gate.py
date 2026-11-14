"""Decision gate (analyst/decide.py) and its routing in analyst/runner.py, models mocked."""

from types import SimpleNamespace

import pytest

from analyst.agent import GeminiAgent
from analyst.decide import API_VERSION, DecisionGate
from analyst.gemini import GeminiVerdictModel
from analyst.nemotron import NemotronVerdictModel
from analyst.runner import Analyst
from tests.test_analyst import ctx
from tests.test_analyst_agent import FakeGemini, FakeWeb, call, reply, verdict_json

NEMOTRON_ID = "nemotron-3.5-super-vl"


class FakeDecisions:
    def __init__(self, p_web: float | None = None, status="coordinated", *, fail: bool = False):
        self.p_web, self.status, self.fail = p_web, status, fail
        self.requests: list[dict] = []
        self.decisions = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.fail:
            raise RuntimeError("404")
        return SimpleNamespace(
            answers={
                "needs_web": SimpleNamespace(noul=self.p_web),
                "status": SimpleNamespace(choice=self.status, confidence=0.7),
            },
            usage=SimpleNamespace(total_input_tokens=1234),
        )


class FakeNemotron:
    """OpenAI-style chat client returning one verdict (or raising)."""

    def __init__(self, status="coordinated", *, model=NEMOTRON_ID, fail=False):
        self.status, self.model, self.fail = status, model, fail
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.fail:
            raise TimeoutError("slow")
        msg = SimpleNamespace(content=verdict_json(status=self.status).text)
        return SimpleNamespace(
            model=self.model,
            choices=[SimpleNamespace(message=msg, finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=900, completion_tokens=200),
        )


def analyst(decisions, *, nemotron=None, agent_replies=(), fast_replies=(), tools=True):
    agent_gemini = FakeGemini(*agent_replies)
    fast_gemini = FakeGemini(*fast_replies)
    a = Analyst(
        agent=GeminiAgent("gemini-4-hydrogen", client=agent_gemini),
        batch=None,
        web=FakeWeb(),
        tools=tools,
        gate=DecisionGate("gemini-decision-1", client=decisions, threshold=0.1),
        fast=GeminiVerdictModel("", "gemini-4-hydrogen", client=fast_gemini),
        nemotron=(
            NemotronVerdictModel("", base_url="x", model=NEMOTRON_ID, client=nemotron)
            if nemotron is not None
            else None
        ),
    )
    return a, agent_gemini, fast_gemini


def test_gate_asks_both_questions_on_v1beta():
    d = FakeDecisions(0.05, "insufficient")
    g = DecisionGate("models/gemini-decision-1", client=d).check("sys", "prompt")
    req = d.requests[0]
    assert req["api_version"] == API_VERSION == "v1beta"
    assert req["model"] == "models/gemini-decision-1"
    assert req["input"] == "sys\n\nprompt"
    assert req["questions"]["needs_web"]["type"] == "noul"
    assert req["questions"]["status"]["type"] == "choice"
    assert g.p_web == 0.05 and g.skip_tools and g.input_tokens == 1234
    assert g.status == "insufficient" and g.status_confidence == 0.7


def test_gated_nemotron_verdict_stands_when_decision_agrees():
    nemo = FakeNemotron("coordinated")
    a, agent_gemini, fast_gemini = analyst(FakeDecisions(0.03, "coordinated"), nemotron=nemo)
    v = a.verdict(None, ctx())
    assert agent_gemini.configs == fast_gemini.configs == []  # no Gemini verdict call at all
    req = nemo.requests[0]
    assert req["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert req["response_format"]["type"] == "json_schema"
    assert v.model == NEMOTRON_ID and v.llm_assessment == "coordinated"
    assert v.gate["route"] == "nemotron" and v.gate["status"] == "coordinated"
    assert [c["model"] for c in v.model_calls] == [NEMOTRON_ID]
    assert v.cost_usd is None and v.tool_calls == []


@pytest.mark.parametrize(
    "nemo", [FakeNemotron("insufficient"), FakeNemotron(fail=True), FakeNemotron(model="llama")]
)
def test_disagreement_or_nemotron_failure_escalates_to_hydrogen(nemo):
    a, agent_gemini, fast_gemini = analyst(
        FakeDecisions(0.03, "coordinated"), nemotron=nemo, fast_replies=[reply(verdict_json())]
    )
    v = a.verdict(None, ctx())
    assert agent_gemini.configs == [] and len(fast_gemini.configs) == 1
    assert fast_gemini.configs[0].tools is None
    assert fast_gemini.configs[0].thinking_config is None  # default thinking
    assert v.model == "gemini-4-hydrogen" and v.gate["route"] == "escalated"
    assert len(v.model_calls) == 2 and v.model_calls[1]["model"] == "gemini-4-hydrogen"
    assert v.cost_usd is not None


def test_without_nemotron_the_gated_path_is_hydrogen_single_shot():
    a, agent_gemini, fast_gemini = analyst(
        FakeDecisions(0.03), fast_replies=[reply(verdict_json())]
    )
    v = a.verdict(None, ctx())
    assert agent_gemini.configs == [] and len(fast_gemini.configs) == 1
    assert v.gate["route"] == "single" and len(v.model_calls) == 1


@pytest.mark.parametrize("decisions", [FakeDecisions(0.6), FakeDecisions(fail=True)])
def test_high_p_web_or_gate_failure_runs_the_agent(decisions):
    nemo = FakeNemotron()
    a, agent_gemini, fast_gemini = analyst(
        decisions,
        nemotron=nemo,
        agent_replies=[reply(call("search_web", query="q", why="w")), reply(verdict_json())],
    )
    v = a.verdict(None, ctx())
    assert fast_gemini.configs == [] and nemo.requests == [] and len(agent_gemini.configs) == 2
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


def test_from_settings_wires_gate_and_nemotron(monkeypatch):
    from common.config import get_settings

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("VERDA_INFERENCE_KEY", "verda-key")
    get_settings.cache_clear()
    try:
        a = Analyst.from_settings(None)
        assert a.gate.name == "gemini-decision-1" and a.gate.threshold == 0.1
        assert a.fast.name == a.agent.name and isinstance(a.nemotron, NemotronVerdictModel)
        monkeypatch.setenv("VERDA_INFERENCE_KEY", "")
        get_settings.cache_clear()
        assert Analyst.from_settings(None).nemotron is None
        monkeypatch.setenv("GEMINI_DECISION_MODEL", "")
        get_settings.cache_clear()
        a = Analyst.from_settings(None)
        assert a.gate is None and a.fast is None and a.nemotron is None
    finally:
        get_settings.cache_clear()
