"""One verdict for one cluster: prompt -> model draft -> guard.

`analyze` is the single-shot path (pre-gathered context only). `investigate` lets Gemini call
tools first (analyst/agent.py); evidence the tools store joins the cluster context before guard,
so the verdict may cite it and nothing else.
"""

from dataclasses import asdict, replace

from analyst.agent import GeminiAgent
from analyst.context import ClusterContext
from analyst.gemini import VerdictModel
from analyst.guard import enforce, no_evidence_verdict
from analyst.prompt import SYSTEM, build_prompt, fit_evidence
from analyst.tools import ToolBox
from analyst.verdict import Verdict


def analyze(ctx: ClusterContext, model: VerdictModel) -> Verdict:
    ctx = fit_evidence(ctx)  # the guard accepts only evidence the prompt showed
    if not ctx.evidence:
        return no_evidence_verdict(ctx)
    draft = model.draft(SYSTEM, build_prompt(ctx))
    return enforce(draft, ctx, model=model.name)


def investigate(ctx: ClusterContext, agent: GeminiAgent, tools: ToolBox) -> Verdict:
    ctx = fit_evidence(ctx)  # shown evidence + what the tools return, nothing else
    if not ctx.evidence:
        return no_evidence_verdict(ctx)
    result = agent.run(SYSTEM, build_prompt(ctx), tools)
    full = replace(ctx, evidence=[*ctx.evidence, *tools.new_evidence.values()])
    verdict = enforce(result.draft, full, model=agent.name)
    return verdict.model_copy(
        update={
            "tool_calls": [asdict(r) for r in tools.records],
            "model_calls": [asdict(c) for c in result.model_calls],
            "cost_usd": result.cost_usd,
        }
    )
