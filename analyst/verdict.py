"""Verdict types. `Verdict` lands in evidence.detail (kind='analyst'); see docs/ARCHITECTURE.md."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["coordinated", "likely_organic", "insufficient"]


class CitedEvidence(BaseModel):
    evidence_id: int
    why: str


class VerdictDraft(BaseModel):
    """What the model is asked to return (also its response schema). Unchecked until `guard`."""

    status: Status
    confidence: float = Field(description="0.0 to 1.0")
    summary: str = Field(description="One or two plain sentences for the dashboard")
    evidence: list[CitedEvidence] = Field(description="3-5 strongest items, by evidence_id")
    counter_evidence: list[str] = Field(description="Strongest reasons it might be organic")
    would_change_verdict: list[str] = Field(description="Observations that would flip the verdict")
    disagreement_reason: str = Field(
        default="",
        description="If status differs from detector_status: the concrete reason. Else empty.",
    )


class Verdict(VerdictDraft):
    """`status` is always the detector's call (clusters.status); the model's own call is
    `llm_assessment`. Disagreements are shown and counted, never silently applied."""

    model: str
    detector_status: Status  # clusters.status when the verdict was written
    adjustments: list[str] = Field(default_factory=list)  # what guard changed, and why
    cluster_updated_at: datetime | None = None
    llm_assessment: Status | None = None  # the model's own call after guard; None = no model ran
    agrees_with_detector: bool | None = None
    # Function-calling trace (analyst/agent.py): every tool and model call of this verdict.
    tool_calls: list[dict] = Field(default_factory=list)
    model_calls: list[dict] = Field(default_factory=list)
    cost_usd: float | None = None  # model cost estimate from analyst/pricing.py
    # Decision gate (analyst/decide.py) for candidate clusters: P(web check matters), threshold,
    # decision-1 status, latency and the route: "nemotron" (its verdict agreed with decision-1),
    # "escalated" (Nemotron disagreed or failed: GEMINI_MODEL single-shot), "single" (no
    # Nemotron configured: GEMINI_MODEL single-shot) or "agent".
    gate: dict | None = None
