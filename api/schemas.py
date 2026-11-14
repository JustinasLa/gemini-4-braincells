"""Response and request models: the JSON contract the dashboard reads (see README.md)."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Status = Literal["coordinated", "likely_organic", "insufficient"]

# C0 control characters (and DEL) except tab, newline and carriage return. PostgreSQL text
# cannot hold NUL, and none of these carry meaning in a handle, link or post text.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SignalBrief(BaseModel):
    family: str | None = None
    name: str | None = None
    score: float | None = None
    polarity: str | None = None
    summary: str | None = None
    evidence_id: int | None = None


class VerdictBrief(BaseModel):
    status: Status  # the detector's call (older verdicts may hold a downgraded value)
    confidence: float
    summary: str | None = None
    model: str | None = None
    created_at: datetime
    llm_assessment: Status | None = None  # the analyst model's own call
    agrees_with_detector: bool | None = None
    disagreement_reason: str | None = None
    tool_calls: int = 0  # live tool calls Gemini made for this verdict (details in `analyst`)


class ClusterSummary(BaseModel):
    id: int
    status: Status  # the detector's call
    score: float
    confidence: float | None = None
    p_value: float | None = None
    size: int | None = None
    families_fired: int | None = None
    window_kind: str | None = None
    window_start: datetime
    window_end: datetime
    campaign_id: int | None = None
    synthetic: bool = False
    updated_at: datetime | None = None
    top_signals: list[SignalBrief] = Field(default_factory=list)
    verdict: VerdictBrief | None = None  # the analyst's latest verdict, if any


class Member(BaseModel):
    did: str
    role: str | None = None
    handle: str | None = None
    account_created_at: datetime | None = None
    followers: int | None = None
    profile_url: str


class Post(BaseModel):
    uri: str
    did: str
    text: str | None = None
    created_at: datetime | None = None
    seen_at: datetime | None = None
    embed_kind: str | None = None
    embed_urls: list[str] = Field(default_factory=list)
    media_hashes: list[str] = Field(default_factory=list)
    url: str | None = None  # bsky.app link


class Evidence(BaseModel):
    id: int
    kind: Literal["signal", "media", "source", "analyst"]
    summary: str | None = None
    detail: dict = Field(default_factory=dict)
    source_url: str | None = None
    model: str | None = None
    created_at: datetime


class ClusterDetail(ClusterSummary):
    signals: dict = Field(default_factory=dict)  # clusters.signals as written by signals/
    members: list[Member] = Field(default_factory=list)
    posts: list[Post] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)  # signal, media, source
    analyst: dict | None = None  # full latest verdict (evidence.detail of kind='analyst')


class EvalRun(BaseModel):
    id: int
    dataset: str
    split: str
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    fpr: float | None = None
    n: int | None = None
    details: dict = Field(default_factory=dict)
    created_at: datetime


class AnalyzeRequest(BaseModel):
    text: str | None = Field(default=None, max_length=5000)
    url: str | None = Field(default=None, max_length=2000)
    handle: str | None = Field(default=None, max_length=300)

    @field_validator("text", "url", "handle")
    @classmethod
    def _strip_control(cls, v: str | None) -> str | None:
        return _CONTROL.sub("", v) if v else v

    @model_validator(mode="after")
    def _one_input(self) -> "AnalyzeRequest":
        if not any(v and v.strip() for v in (self.text, self.url, self.handle)):
            raise ValueError("give at least one of text, url, handle")
        return self


class WebFinding(BaseModel):
    summary: str
    detail: dict
    source_url: str | None = None
    model: str


class AnalyzeResponse(BaseModel):
    # "matched": the input appears in clusters we monitor; their verdicts apply.
    # "insufficient": not in our window; web findings below are weaker and are not a verdict.
    status: Literal["matched", "insufficient"]
    clusters: list[ClusterSummary] = Field(default_factory=list)
    # Clusters the author of a submitted Bluesky post belongs to, when that post itself is in
    # none of them. Context about the account only: not a verdict on the post, never "matched".
    author_clusters: list[ClusterSummary] = Field(default_factory=list)
    web_findings: list[WebFinding] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
