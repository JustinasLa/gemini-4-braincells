"""One verdict for one cluster: prompt -> model draft -> guard."""

from analyst.context import ClusterContext
from analyst.gemini import VerdictModel
from analyst.guard import enforce, no_evidence_verdict
from analyst.prompt import SYSTEM, build_prompt
from analyst.verdict import Verdict


def analyze(ctx: ClusterContext, model: VerdictModel) -> Verdict:
    if not ctx.evidence:
        return no_evidence_verdict(ctx)
    draft = model.draft(SYSTEM, build_prompt(ctx))
    return enforce(draft, ctx, model=model.name)
