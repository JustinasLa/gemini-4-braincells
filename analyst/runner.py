"""Picks how a cluster gets its verdict and wires models, tools and storage together.

- Candidate clusters (detector status coordinated or insufficient): the verdict model runs as an
  agent with tools (live Oxylabs search/scrape + DB lookups), within `Budget`.
- Clusters the detector already calls likely_organic: the cheap batch model, single shot, no web
  calls (they are the bulk; Oxylabs spend goes to clusters where a source check can matter).
"""

import logging

import psycopg

from analyst.agent import GeminiAgent
from analyst.context import ClusterContext
from analyst.gemini import GeminiVerdictModel, VerdictModel
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
    ):
        self.agent = agent
        self.batch = batch
        self.web = web
        self.tools = tools
        self._budget = budget or {}
        self.cache = cache or WebCache()

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
        return cls(
            agent=GeminiAgent.from_settings(),
            batch=batch,
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
