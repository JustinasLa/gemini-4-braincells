"""Everything the analyst may look at for one cluster. Built by analyst.store, read by prompt."""

from dataclasses import dataclass, field
from datetime import datetime

from analyst.verdict import Status


@dataclass(frozen=True)
class EvidenceItem:
    id: int
    kind: str  # signal | media | source
    summary: str
    detail: dict
    source_url: str | None
    model: str | None


@dataclass(frozen=True)
class ClusterContext:
    id: int
    status: Status  # the detector's call
    score: float
    confidence: float | None
    p_value: float | None
    size: int | None
    window_start: datetime
    window_end: datetime
    synthetic: bool
    updated_at: datetime | None
    signals: dict  # clusters.signals JSONB
    evidence: list[EvidenceItem] = field(default_factory=list)
    sample_posts: list[str] = field(default_factory=list)
    evidence_omitted: int = 0  # items prompt.fit_evidence left out for size

    @property
    def evidence_ids(self) -> set[int]:
        return {e.id for e in self.evidence}
