"""LookupResult and its parts. The JSON contract is documented in lookup/README.md."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Kind = Literal["x_post", "x_profile", "bsky_post", "bsky_profile", "web", "text", "image"]
StepState = Literal["pending", "running", "done", "failed", "skipped"]


class ImageMatch(BaseModel):
    id: str
    image_url: str
    thumb_url: str | None = None
    page_url: str | None = None
    domain: str | None = None
    engine: Literal["tineye", "yandex"]
    first_seen: datetime | None = None
    time_source: Literal["crawl_date", "page", "snowflake"] | None = None
    width: int | None = None
    height: int | None = None
    relation: Literal["same", "resized", "cropped", "edited", "different", "unknown"] = "unknown"
    distance: float | None = None
    # (left, top, right, bottom) fractions 0..1 of the uploaded image (relate()'s base);
    # crop_box is None when the match is larger than the upload
    crop_box: tuple[float, float, float, float] | None = None
    edited_region: tuple[float, float, float, float] | None = None
    relation_note: str = ""
    changes: list[str] = Field(default_factory=list)
    change_kinds: list[
        Literal[
            "caption_changed",
            "text_added",
            "text_removed",
            "element_added",
            "element_removed",
            "face_changed",
            "cropped",
            "recolored",
            "watermark",
            "other",
        ]
    ] = Field(default_factory=list)
    # which side `changes` start from: "match" = the changes turn this copy into the upload (the
    # copy is the earlier one), "upload" = they turn the upload into this copy
    changes_from: Literal["match", "upload"] | None = None
    receipt_id: int | None = None


class Card(BaseModel):
    title: str | None = None
    domain: str | None = None
    url: str | None = None


class Metrics(BaseModel):
    views: int | None = None
    replies: int | None = None
    reposts: int | None = None
    likes: int | None = None
    bookmarks: int | None = None
    quotes: int | None = None


class Post(BaseModel):
    id: str
    platform: Literal["x", "bluesky", "web"]
    url: str | None = None
    author_handle: str | None = None
    author_name: str | None = None
    text: str = ""
    created_at: datetime | None = None
    time_source: Literal["snowflake", "api", "page", "search_hint"] | None = None
    role: Literal[
        "submitted",
        "origin",
        "copy",
        "quote",
        "reply",
        "mention",
        "author_recent",
        "bluesky",
        "screenshotted",  # the real post a screenshot in the submitted post shows
    ] = "mention"
    similarity: float | None = None
    metrics: Metrics | None = None
    metrics_as_of: datetime | None = None
    card: Card | None = None
    media_urls: list[str] = Field(default_factory=list)
    receipt_id: int | None = None
    verified: bool = False  # True when the post's own page/API was read (not only a search snippet)


class Account(BaseModel):
    platform: Literal["x", "bluesky"]
    handle: str
    name: str | None = None
    url: str | None = None
    avatar_url: str | None = None
    bio: str | None = None
    location: str | None = None
    joined: str | None = None
    age_days: int | None = None
    followers: int | None = None
    following: int | None = None
    posts_count: int | None = None
    recent_posts_per_day: float | None = None
    receipt_id: int | None = None


class Page(BaseModel):
    id: str
    url: str
    final_url: str | None = None
    role: Literal["linked", "coverage", "reference"] = "coverage"
    publisher: str | None = None
    author: str | None = None
    published_at: str | None = None
    published_hint: str | None = None
    headline: str | None = None
    claims: list[str] = Field(default_factory=list)
    snippet: str | None = None
    time: datetime | None = None
    time_source: Literal["page", "search_hint"] | None = None
    # the true time lies in [time, time + time_uncertainty_s]; None when unknown
    time_uncertainty_s: float | None = None
    receipt_id: int | None = None


class MediaFinding(BaseModel):
    media_url: str
    post_id: str | None = None
    description: str = ""
    extracted_text: list[str] = Field(default_factory=list)
    reuse_cues: list[str] = Field(default_factory=list)
    manipulation_cues: list[str] = Field(default_factory=list)
    confidence: float | None = None
    model: str | None = None
    latency_ms: int | None = None
    receipt_id: int | None = None


class Edge(BaseModel):
    from_: str = Field(alias="from", serialization_alias="from")
    to: str
    rel: Literal[
        "repost", "quote", "copy", "media", "link", "reply", "cites", "screenshot_of", "variant"
    ]
    sim: float | None = None
    tool: Literal["oxylabs", "nemotron", "gemini", "detector", "bluesky", "tineye", "yandex"] = (
        "oxylabs"
    )
    note: str | None = None

    model_config = {"populate_by_name": True}


class Spread(BaseModel):
    n_posts: int
    n_accounts: int
    n_verbatim: int
    first_at: datetime | None = None
    last_at: datetime | None = None
    span_s: float | None = None
    first_hour_accounts: int | None = None
    min_gap_s: float | None = None
    note: str = "Search results are a sample of where the text appears, not every copy."


class Receipt(BaseModel):
    id: int
    tool: Literal[
        "oxylabs", "nemotron", "gemini", "bluesky", "detector", "x-id", "tineye", "yandex", "fetch"
    ]
    endpoint: str
    summary: str
    source_url: str | None = None
    query: str | None = None
    request_id: str | None = None
    latency_ms: int | None = None
    cached: bool = False
    ok: bool = True
    at: datetime


class ClaimCheck(BaseModel):
    claim: str
    finding: Literal["supported", "contradicted", "mixed", "unverified"] = "unverified"
    explanation: str
    cites: list[int] = Field(default_factory=list)


class DiffPart(BaseModel):
    """One run of words in a screenshot-vs-real-post diff."""

    op: Literal["same", "shot_only", "post_only"]  # shot_only: only in the screenshot
    text: str


class ScreenshotReceipt(BaseModel):
    """What a screenshot of a post claims, and the real post it was checked against.

    The label describes the screenshot, never a person. Counts are context only: posts gain
    and lose likes, so they never decide the label."""

    media_url: str | None = None
    media_hash: str
    post_id: str | None = None  # the submitted post whose image is the screenshot
    # what Nemotron read in the screenshot (media/screenshot.py ScreenshotPost)
    platform: str = "unknown"
    handle: str | None = None
    display_name: str | None = None
    shown_time: str | None = None
    shown_text: str | None = None
    shown_counts: dict[str, int | None] = Field(default_factory=dict)
    ui_oddities: list[str] = Field(default_factory=list)
    read_confidence: float | None = None
    read_model: str | None = None
    # the check
    label: Literal["matches", "differs", "not_found", "unchecked"] = "unchecked"
    reason: str = ""
    original_id: str | None = None  # key into posts ("x:<id>" or the at:// uri)
    original_url: str | None = None
    original_text: str | None = None
    original_created_at: datetime | None = None
    original_time_source: Literal["snowflake", "api"] | None = None
    original_receipt_id: int | None = None  # the receipt of the fetch that read the real post
    similarity: float | None = None
    diff: list[DiffPart] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    time_check: Literal["consistent", "inconsistent", "unknown"] = "unknown"
    # the shown time allows any real time in [earliest, latest]; None = open on that side
    shown_time_earliest: datetime | None = None
    shown_time_latest: datetime | None = None
    time_note: str | None = None
    counts_now: Metrics | None = None
    counts_note: str | None = None
    candidates_checked: int = 0
    receipt_ids: list[int] = Field(default_factory=list)
    latency_ms: int | None = None


class Assessment(BaseModel):
    model: str
    summary: str
    origin: str | None = None
    corroboration: str | None = None
    spread_pattern: str | None = None
    claim_checks: list[ClaimCheck] = Field(default_factory=list)
    media_provenance: str | None = None
    media_provenance_cites: list[int] = Field(default_factory=list)
    counter_evidence: list[str] = Field(default_factory=list)
    would_change: list[str] = Field(default_factory=list)
    cites: list[int] = Field(default_factory=list)
    llm_reading: Literal["organic_looking", "copy_campaign_looking", "unclear"] = "unclear"
    latency_ms: int | None = None
    cost_usd: float | None = None


class Step(BaseModel):
    id: str
    tool: str
    label: str
    state: StepState = "pending"
    detail: str | None = None
    found: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None


class ImageRef(BaseModel):
    """An uploaded image (POST /api/lookup/image). Its bytes stay in the job, in memory, and are
    served back at GET /api/lookup/{id}/image until the job expires; they are never stored."""

    sha256: str
    mime: str
    bytes: int


class LookupBody(BaseModel):
    """The JSON body of POST /api/lookup (an image goes to POST /api/lookup/image)."""

    url: str | None = None
    text: str | None = None


class LookupInput(BaseModel):
    url: str | None = None
    text: str | None = None
    image: ImageRef | None = None


class LookupResult(BaseModel):
    id: str
    state: Literal["running", "done", "failed"] = "running"
    error: str | None = None
    input: LookupInput
    kind: Kind
    started_at: datetime
    finished_at: datetime | None = None
    status: Literal["matched", "insufficient"] = "insufficient"
    subject_id: str | None = None
    origin_id: str | None = None
    posts: list[Post] = Field(default_factory=list)
    accounts: list[Account] = Field(default_factory=list)
    pages: list[Page] = Field(default_factory=list)
    media: list[MediaFinding] = Field(default_factory=list)
    screenshots: list[ScreenshotReceipt] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    clusters: list[dict[str, Any]] = Field(default_factory=list)
    spread: Spread | None = None
    receipts: list[Receipt] = Field(default_factory=list)
    # the primary reading (Nemotron's; Gemini's when Nemotron gave none)
    assessment: Assessment | None = None
    # every reading in arrival order; a second one (Gemini) can arrive while state is running
    readings: list[Assessment] = Field(default_factory=list)
    # the readings' llm_reading differ: both are shown, neither sets the status
    readings_disagree: bool = False
    steps: list[Step] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # why the submitted post couldn't be read ("Account suspended"); the UI shows it in place of
    # "Not enough evidence", since there was nothing to judge
    unreadable: str | None = None
    image_matches: list[ImageMatch] = Field(default_factory=list)
    image_origin_id: str | None = None
