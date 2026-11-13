"""Checks applied to every model draft before it is stored.

The detector decides; the model explains (PLAN section 5 proposal, implemented here):
`Verdict.status` is always the detector's status. The model's own call goes to `llm_assessment`,
with `agrees_with_detector` and `disagreement_reason`, so a disagreement is visible and counted
instead of overriding the detector. Each change guard makes is recorded in `Verdict.adjustments`.
"""

from analyst.context import ClusterContext
from analyst.verdict import Verdict, VerdictDraft

MIN_CITATIONS = 2


def enforce(draft: VerdictDraft, ctx: ClusterContext, *, model: str) -> Verdict:
    notes: list[str] = []
    assessment = draft.status

    known = ctx.evidence_ids
    unique = {c.evidence_id: c for c in reversed(draft.evidence)}  # first "why" wins
    cited = [unique[i] for i in dict.fromkeys(c.evidence_id for c in draft.evidence) if i in known]
    dropped = sorted({c.evidence_id for c in draft.evidence} - known)
    if dropped:
        notes.append(f"Dropped citations to unknown evidence ids {dropped}.")

    reason = draft.disagreement_reason.strip()
    if assessment != "insufficient" and len(cited) < MIN_CITATIONS:
        notes.append(
            f"Fewer than {MIN_CITATIONS} valid citations; the model's assessment "
            f"'{assessment}' was downgraded to 'insufficient'."
        )
        assessment = "insufficient"
        reason = reason or f"Fewer than {MIN_CITATIONS} valid citations."

    agrees = assessment == ctx.status
    if agrees:
        reason = ""
    elif not reason:
        reason = "The model gave no reason."
        notes.append("The model disagreed with the detector without giving a reason.")

    confidence = min(max(draft.confidence, 0.0), 1.0)
    if confidence != draft.confidence:
        notes.append("Confidence clamped to [0, 1].")

    if assessment == "coordinated" and not draft.counter_evidence:
        notes.append("The model offered no counter-evidence.")

    return Verdict(
        status=ctx.status,
        confidence=confidence,
        summary=draft.summary,
        evidence=cited,
        counter_evidence=draft.counter_evidence,
        would_change_verdict=draft.would_change_verdict,
        disagreement_reason=reason,
        model=model,
        detector_status=ctx.status,
        adjustments=notes,
        cluster_updated_at=ctx.updated_at,
        llm_assessment=assessment,
        agrees_with_detector=agrees,
    )


def no_evidence_verdict(ctx: ClusterContext) -> Verdict:
    """Written without a model call when there is nothing to cite."""
    return Verdict(
        status=ctx.status,
        confidence=1.0,
        summary="No evidence is stored for this cluster yet, so no verdict can be given.",
        evidence=[],
        counter_evidence=[],
        would_change_verdict=["Detector or source evidence for this cluster."],
        model="none",
        detector_status=ctx.status,
        adjustments=["No evidence items; the model was not called."],
        cluster_updated_at=ctx.updated_at,
    )
