"""Gemini function-calling loop (analyst/agent.py + tools.py) with Gemini and Oxylabs mocked."""

import json

import pytest
from google.genai import types

from analyst.agent import FINAL_NUDGE, AgentError, GeminiAgent
from analyst.service import investigate
from analyst.tools import Budget, MemorySink, ToolBox, WebCache
from sources.models import PageFacts, SearchHit
from sources.oxylabs import OxylabsError
from tests.test_analyst import ctx

FIRST_ID = 900_000_000  # MemorySink's first id


class FakeWeb:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.searches: list[str] = []
        self.scrapes: list[str] = []

    def search(self, query, max_results=10):
        self.searches.append(query)
        if self.fail:
            raise OxylabsError("boom", status=500)
        return [
            SearchHit(url="https://origin.example/first", title="First", position=1),
            SearchHit(url="https://news.example/story", title="Story", position=2),
        ]

    def scrape_facts(self, url):
        self.scrapes.append(url)
        if self.fail:
            raise OxylabsError("boom", status=500)
        return PageFacts(url=url, final_url=url, http_status=200, headline="Water poisoned?")


def call(name, **args):
    return types.Part(function_call=types.FunctionCall(id=f"id-{name}", name=name, args=args))


def reply(*parts, prompt=100, out=20):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt, candidates_token_count=out
        ),
    )


def verdict_json(ids=(1, 2), status="coordinated"):
    return types.Part(
        text=json.dumps(
            {
                "status": status,
                "confidence": 0.8,
                "summary": "s",
                "evidence": [{"evidence_id": i, "why": "w"} for i in ids],
                "counter_evidence": ["news event"],
                "would_change_verdict": ["x"],
            }
        )
    )


class FakeGemini:
    """Plays back scripted replies and records what the agent sent."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self.configs: list[types.GenerateContentConfig] = []
        self.contents: list[list] = []
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.configs.append(config)
        self.contents.append(list(contents))
        return self._replies.pop(0)


def toolbox(c=None, *, web=None, cache=None, sink=None, **budget):
    return ToolBox(
        c or ctx(),
        web=web if web is not None else FakeWeb(),
        sink=sink or MemorySink(),
        cache=cache or WebCache(),
        budget=Budget(**budget),
    )


def responses(fake: FakeGemini, turn: int) -> list[dict]:
    """Function responses the agent sent back in the request of `turn`."""
    last = fake.contents[turn][-1]
    return [p.function_response.response for p in last.parts if p.function_response]


def test_tool_evidence_is_stored_and_citable():
    gemini = FakeGemini(
        reply(call("search_web", query='"supply was poisoned"', why="origin")),
        reply(verdict_json(ids=(1, FIRST_ID))),
    )
    box = toolbox()
    v = investigate(ctx(), GeminiAgent("gemini-4-hydrogen", client=gemini), box)

    assert [c.evidence_id for c in v.evidence] == [1, FIRST_ID]
    assert v.adjustments == []
    stored = box.new_evidence[FIRST_ID]
    assert stored.kind == "source" and stored.source_url == "https://origin.example/first"
    assert stored.detail["requested_by"] == "gemini" and stored.detail["why"] == "origin"
    [tc] = v.tool_calls
    assert tc["name"] == "search_web" and tc["endpoint"] == "/search"
    assert tc["evidence_id"] == FIRST_ID and tc["source_url"] == "https://origin.example/first"
    assert responses(gemini, 1)[0]["evidence_id"] == FIRST_ID
    assert [c["turn"] for c in v.model_calls] == [0, 1]
    assert v.model_calls[0]["prompt_tokens"] == 100 and v.cost_usd > 0


def test_citation_guard_drops_ids_no_tool_returned():
    gemini = FakeGemini(
        reply(call("search_web", query="q", why="w")),
        reply(verdict_json(ids=(FIRST_ID, FIRST_ID + 1, 999))),
    )
    v = investigate(ctx(), GeminiAgent("m", client=gemini), toolbox())
    assert [c.evidence_id for c in v.evidence] == [FIRST_ID]
    assert any(str(FIRST_ID + 1) in a and "999" in a for a in v.adjustments)
    assert v.llm_assessment == "insufficient" and v.status == "coordinated"


def test_total_call_budget_refuses_extra_calls_and_forces_verdict():
    web = FakeWeb()
    gemini = FakeGemini(
        reply(
            call("search_web", query="a", why="w"),
            call("search_web", query="b", why="w"),
            call("search_web", query="c", why="w"),
        ),
        reply(verdict_json()),
    )
    box = toolbox(web=web, max_tool_calls=2)
    v = investigate(ctx(), GeminiAgent("m", client=gemini), box)

    assert web.searches == ["a", "b"]
    third = responses(gemini, 1)[2]
    assert "budget used up" in third["error"]
    final = gemini.configs[1]
    assert final.tool_config.function_calling_config.mode == "NONE"
    assert gemini.contents[1][-1].parts[-1].text == FINAL_NUDGE
    assert [t["ok"] for t in v.tool_calls] == [True, True, False]


def test_web_budget_counts_only_live_calls():
    web = FakeWeb()
    box = toolbox(web=web, max_web_calls=1)
    first = box.call("search_web", {"query": "same"})
    again = box.call("search_web", {"query": "same"})  # cached: free
    other = box.call("search_web", {"query": "other"})
    assert web.searches == ["same"]
    assert first["evidence_id"] == again["evidence_id"]
    assert "web call budget" in other["error"]
    assert [r.cached for r in box.records] == [False, True, False]


def test_time_budget_skips_tools_entirely():
    gemini = FakeGemini(reply(verdict_json()))
    v = investigate(ctx(), GeminiAgent("m", client=gemini), toolbox(max_seconds=0))
    assert len(gemini.configs) == 1
    assert gemini.configs[0].tool_config.function_calling_config.mode == "NONE"
    assert v.tool_calls == [] and v.llm_assessment == "coordinated"


def test_cache_is_shared_across_clusters_but_each_cluster_gets_its_own_row():
    web, cache = FakeWeb(), WebCache()
    a = toolbox(ctx(), web=web, cache=cache, sink=MemorySink(start_id=100))
    b = toolbox(ctx(), web=web, cache=cache, sink=MemorySink(start_id=200))
    ra = a.call("scrape_url", {"url": "https://news.example/story"})
    rb = b.call("scrape_url", {"url": "https://news.example/story"})
    assert web.scrapes == ["https://news.example/story"]
    assert (ra["evidence_id"], rb["evidence_id"]) == (100, 200)
    assert rb["headline"] == "Water poisoned?"
    assert b.records[0].cached and b.budget.web_calls == 0


def test_already_stored_row_is_reused_without_oxylabs():
    web, sink = FakeWeb(), MemorySink(start_id=50)
    toolbox(web=web, sink=sink).call("scrape_url", {"url": "https://news.example/story"})
    web2 = FakeWeb()
    again = toolbox(web=web2, sink=sink).call("scrape_url", {"url": "https://news.example/story"})
    assert web2.scrapes == [] and again["evidence_id"] == 50


def test_scrape_only_allows_known_urls():
    web = FakeWeb()
    box = toolbox(web=web)
    refused = box.call("scrape_url", {"url": "https://attacker.example/x"})
    assert "not allowed" in refused["error"] and web.scrapes == []
    box.call("search_web", {"query": "q"})  # its result URLs become fetchable
    ok = box.call("scrape_url", {"url": "https://origin.example/first"})
    assert ok["evidence_id"] and web.scrapes == ["https://origin.example/first"]


def test_oxylabs_failure_is_reported_and_not_retried():
    web, cache = FakeWeb(fail=True), WebCache()
    box = toolbox(web=web, cache=cache)
    assert "do not retry" in box.call("search_web", {"query": "q"})["error"]
    assert "do not retry" in box.call("search_web", {"query": "q"})["error"]
    assert web.searches == ["q"] and box.new_evidence == {}


def test_unknown_or_undeclared_tool_is_refused():
    box = toolbox()  # no DB connection: lookups are not declared
    assert "unknown tool" in box.call("get_account", {"account": "x"})["error"]
    assert "unknown tool" in box.call("rm_rf", {})["error"]
    assert {d["name"] for d in box.declarations()} == {"search_web", "scrape_url"}


def test_model_that_ignores_disabled_calling_raises():
    gemini = FakeGemini(reply(call("search_web", query="q", why="w")))
    with pytest.raises(AgentError):
        GeminiAgent("m", client=gemini).run("sys", "prompt", toolbox(max_seconds=0))


def test_no_tools_without_web_or_db():
    gemini = FakeGemini(reply(verdict_json()))
    box = ToolBox(ctx(), web=None, sink=MemorySink(), cache=WebCache(), budget=Budget())
    investigate(ctx(), GeminiAgent("m", client=gemini), box)
    assert gemini.configs[0].tools is None
