"""Checks applied to every model draft before it is stored. The model can only add caution.

Each change is recorded in `Verdict.adjustments`, so the dashboard can show what was overridden.
"""

from analyst.context import ClusterContext
from analyst.verdict import Status, Verdict, VerdictDraft

MIN_CITATIONS = 2

# Detector status -> statuses the analyst may return.
ALLOWED: dict[Status, set[Status]] = {
    "coordinated": {"coordinated", "insufficient"},
    "likely_organic": {"likely_organic", "insufficient"},
    "insufficient": {"insufficient"},
}


def enforce(draft: VerdictDraft, ctx: ClusterContext, *, model: str) -> Verdict:
    notes: list[str] = []
    status = draft.status

    known = ctx.evidence_ids
    cited = [c for c in draft.evidence if c.evidence_id in known]
    dropped = sorted({c.evidence_id for c in draft.evidence} - known)
    if dropped:
        notes.append(f"Dropped citations to unknown evidence ids {dropped}.")

    if status not in ALLOWED[ctx.status]:
        notes.append(
            f"Model said '{status}' but the detector said '{ctx.status}'; "
            "the analyst may only agree or be more cautious."
        )
        status = "insufficient"

    if status != "insufficient" and len(cited) < MIN_CITATIONS:
        notes.append(f"Fewer than {MIN_CITATIONS} valid citations; downgraded to 'insufficient'.")
        status = "insufficient"

    confidence = min(max(draft.confidence, 0.0), 1.0)
    if confidence != draft.confidence:
        notes.append("Confidence clamped to [0, 1].")

    if status == "coordinated" and not draft.counter_evidence:
        notes.append("The model offered no counter-evidence.")

    return Verdict(
        status=status,
        confidence=confidence,
        summary=draft.summary,
        evidence=cited,
        counter_evidence=draft.counter_evidence,
        would_change_verdict=draft.would_change_verdict,
        model=model,
        detector_status=ctx.status,
        adjustments=notes,
        cluster_updated_at=ctx.updated_at,
    )


def no_evidence_verdict(ctx: ClusterContext) -> Verdict:
    """Written without a model call when there is nothing to cite."""
    return Verdict(
        status="insufficient",
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
