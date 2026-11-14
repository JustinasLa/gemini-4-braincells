"""Picks how a cluster gets its verdict and wires models, tools and storage together.

- Candidate clusters (detector status coordinated or insufficient): the verdict model runs as an
  agent with tools (live Oxylabs search/scrape + DB lookups), within `Budget`.
- Clusters the detector already calls likely_organic: the cheap batch model, single shot, no web
  calls (they are the bulk; Oxylabs spend goes to clusters where a source check can matter).
- Decision gate (analyst/decide.py): before a candidate cluster's agent run, gemini-decision-1
  estimates whether a web check could change the call. Below the threshold the verdict model
  answers once with low thinking instead (no tool loop); on a gate failure the agent runs.
"""

import logging
from dataclasses import asdict

import psycopg

from analyst.agent import GeminiAgent
from analyst.context import ClusterContext
from analyst.decide import DecisionGate, GateResult
from analyst.gemini import GeminiVerdictModel, VerdictModel
from analyst.prompt import SYSTEM, build_prompt, fit_evidence
from analyst.service import analyze, investigate
from analyst.tools import Budget, DbSink, EvidenceSink, MemorySink, ToolBox, WebCache
from analyst.verdict import Verdict
from common.config import get_settings
from sources.checker import WebClient

log = logging.getLogger(__name__)

TOOL_STATUSES = frozenset({"coordinated", "insufficient"})


class Analyst:
    def __init__(
        self,
        *,
        agent: GeminiAgent,
        batch: VerdictModel | None,
        web: WebClient | None,
        tools: bool = True,
        budget: dict | None = None,
        cache: WebCache | None = None,
        gate: DecisionGate | None = None,
        fast: GeminiVerdictModel | None = None,
    ):
        self.agent = agent
        self.batch = batch
        self.web = web
        self.tools = tools
        self._budget = budget or {}
        self.cache = cache or WebCache()
        self.gate = gate  # with `fast`: skips the tool loop where the web cannot matter
        self.fast = fast  # the verdict model, single shot with low thinking

    @property
    def name(self) -> str:
        return self.agent.name

    @classmethod
    def from_settings(cls, web: WebClient | None, *, tools: bool | None = None) -> "Analyst":
        s = get_settings()
        batch = (
            GeminiVerdictModel(s.gemini_api_key, s.gemini_batch_model)
            if s.gemini_batch_model
            else None
        )
        gate = fast = None
        if s.gemini_decision_model:
            gate = DecisionGate(
                s.gemini_decision_model,
                api_key=s.gemini_api_key,
                threshold=s.analyst_gate_max_p_web,
            )
            fast = GeminiVerdictModel(
                s.gemini_api_key, s.gemini_model, thinking_level=s.analyst_fast_thinking
            )
        return cls(
            agent=GeminiAgent.from_settings(),
            batch=batch,
            gate=gate,
            fast=fast,
            web=web,
            tools=s.analyst_tools if tools is None else tools,
            budget={
                "max_tool_calls": s.analyst_max_tool_calls,
                "max_web_calls": s.analyst_max_web_calls,
                "max_seconds": s.analyst_time_budget_s,
            },
        )

    def verdict(
        self, conn: psycopg.Connection | None, ctx: ClusterContext, *, dry_run: bool = False
    ) -> Verdict:
        if self.batch is not None and ctx.status not in TOOL_STATUSES:
            return analyze(ctx, self.batch)
        fitted = fit_evidence(ctx)
        # no evidence: investigate writes the no-model verdict, nothing to gate
        if not (self.tools and self.gate and self.fast and fitted.evidence):
            return self._agent_verdict(conn, ctx, dry_run=dry_run)
        gate = self.gate.check(SYSTEM, build_prompt(fitted))
        if gate is not None and gate.skip_tools:
            return self._fast_verdict(ctx, gate)
        info = asdict(gate) if gate is not None else {"model": self.gate.name, "error": True}
        verdict = self._agent_verdict(conn, ctx, dry_run=dry_run)
        return verdict.model_copy(update={"gate": {**info, "route": "agent"}})

    def _fast_verdict(self, ctx: ClusterContext, gate: GateResult) -> Verdict:
        assert self.fast is not None
        verdict = analyze(ctx, self.fast)
        c = self.fast.last_call
        call = {
            "model": self.fast.name,
            "turn": 0,
            "latency_ms": c.get("latency_ms"),
            "prompt_tokens": c.get("prompt_tokens"),
            "output_tokens": c.get("output_tokens"),
            "thoughts_tokens": c.get("thoughts_tokens"),
            "function_calls": 0,
            "cost_usd": c.get("cost_usd"),
        }
        return verdict.model_copy(
            update={
                "model_calls": [call],
                "cost_usd": c.get("cost_usd"),
                "gate": {**asdict(gate), "route": "single"},
            }
        )

    def _agent_verdict(
        self, conn: psycopg.Connection | None, ctx: ClusterContext, *, dry_run: bool
    ) -> Verdict:
        sink: EvidenceSink
        if conn is None:
            sink = MemorySink()
        elif dry_run:  # writes nothing, but reuses rows already stored (no repeat Oxylabs calls)
            sink = MemorySink(read_through=DbSink(conn, ctx.id).existing)
        else:
            sink = DbSink(conn, ctx.id)
        box = ToolBox(
            ctx,
            web=self.web if self.tools else None,
            sink=sink,
            cache=self.cache,
            budget=Budget(**self._budget),
            conn=conn if self.tools else None,
            requested_by=f"gemini:{self.agent.name}",
        )
        return investigate(ctx, self.agent, box)
