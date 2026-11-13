"""Verdict types. `Verdict` is what lands in evidence.detail (kind='analyst'); see README.md."""

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


class Verdict(VerdictDraft):
    model: str
    detector_status: Status  # clusters.status when the verdict was written
    adjustments: list[str] = Field(default_factory=list)  # what guard changed, and why
    cluster_updated_at: datetime | None = None
