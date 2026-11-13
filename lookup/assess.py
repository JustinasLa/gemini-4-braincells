"""Gemini reads a lookup's receipts and writes a short, cited reading. It never sets status."""

from __future__ import annotations

import json
import logging
import time
from typing import Literal

from pydantic import BaseModel, Field

from lookup.models import Assessment, LookupResult

log = logging.getLogger(__name__)

SYSTEM = """You read the receipts of a link lookup for "Shill or Chill", a tool that checks
whether a message is being amplified by coordinated accounts. You get facts that live tools
collected (X and Bluesky posts with exact times and counts, author profiles, web pages, image
readings by Nemotron), each with a receipt id.

Write a short, plain reading for a general audience:
- summary: 2-4 sentences: what the post is, where it comes from, how it spread.
- origin: the earliest known source and how sure that is (search results are a sample).
- corroboration: whether independent outlets report the same thing (name them), or "none found".
- spread_pattern: describe the copies: how many accounts, how fast, verbatim or quoted,
  established or new accounts. Facts only.
- counter_evidence: reasons the spread may be ordinary (famous author, news event, people
  quoting rather than copying, old accounts), or reasons it may not be.
- would_change: what new facts would change the reading.
- cites: the receipt ids your statements rest on. Cite only ids that appear in the input.
- llm_reading: "organic_looking", "copy_campaign_looking" or "unclear". This is your reading,
  shown as such; it is NOT the verdict. Many accounts posting the same words in minutes, new or
  templated accounts, and no organic explanation lean to copy_campaign_looking; a well-known
  author whose words are quoted and replied to by established accounts leans organic.

Rules: never call any account or person a shill, bot or liar; describe behaviour. Never invent
facts, numbers, dates or sources not in the input. If data is thin, say so and use "unclear".
Times are UTC."""


class _Draft(BaseModel):
    summary: str
    origin: str
    corroboration: str
    spread_pattern: str
    counter_evidence: list[str] = Field(default_factory=list)
    would_change: list[str] = Field(default_factory=list)
    cites: list[int] = Field(default_factory=list)
    llm_reading: Literal["organic_looking", "copy_campaign_looking", "unclear"]


def brief(r: LookupResult, max_posts: int = 30, max_pages: int = 12) -> str:
    """The receipts as compact JSON for the prompt."""

    def post(p):
        return {
            "id": p.id,
            "role": p.role,
            "platform": p.platform,
            "author": p.author_handle,
            "time": p.created_at.isoformat() if p.created_at else None,
            "text": (p.text or "")[:400],
            "similarity_to_submitted": p.similarity,
            "metrics": p.metrics.model_dump(exclude_none=True) if p.metrics else None,
            "read_from_own_page": p.verified,
            "receipt": p.receipt_id,
        }

    order = {"submitted": 0, "origin": 1, "copy": 2, "quote": 3, "bluesky": 4, "reply": 5}
    posts = sorted(r.posts, key=lambda p: (order.get(p.role, 9), p.created_at or r.started_at))
    data = {
        "input": r.input.model_dump(exclude_none=True),
        "kind": r.kind,
        "submitted_id": r.subject_id,
        "earliest_known_source_id": r.origin_id,
        "posts": [post(p) for p in posts[:max_posts]],
        "accounts": [a.model_dump(exclude_none=True) for a in r.accounts],
        "pages": [
            pg.model_dump(exclude_none=True, exclude={"final_url"}) for pg in r.pages[:max_pages]
        ],
        "media": [m.model_dump(exclude_none=True, exclude={"latency_ms"}) for m in r.media],
        "spread": r.spread.model_dump(mode="json") if r.spread else None,
        "monitored_clusters": [
            {"id": c.get("id"), "detector_status": c.get("status"), "score": c.get("score")}
            for c in r.clusters
        ],
        "receipts": [
            {"id": x.id, "tool": x.tool, "endpoint": x.endpoint, "summary": x.summary}
            for x in r.receipts
        ],
        "notes": r.notes,
    }
    return json.dumps(data, default=str, ensure_ascii=False)


class Assessor:
    def __init__(self, api_key: str, model: str, *, cap=None):
        from google import genai

        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set")
        self._client = genai.Client(api_key=api_key)
        if cap is not None:
            self._client = cap.wrap(self._client)
        self.model = model

    def assess(self, r: LookupResult) -> Assessment:
        from google.genai import types

        from analyst.pricing import cost_usd

        started = time.monotonic()
        res = self._client.models.generate_content(
            model=self.model,
            contents=brief(r),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM,
                response_mime_type="application/json",
                response_schema=_Draft,
                temperature=0.2,
            ),
        )
        latency = int((time.monotonic() - started) * 1000)
        usage = res.usage_metadata
        prompt = getattr(usage, "prompt_token_count", None)
        out = (getattr(usage, "candidates_token_count", None) or 0) + (
            getattr(usage, "thoughts_token_count", None) or 0
        )
        log.info(
            "gemini call model=%s purpose=lookup latency_ms=%d prompt_tokens=%s output_tokens=%s",
            self.model,
            latency,
            prompt,
            out,
        )
        draft = (
            res.parsed
            if isinstance(res.parsed, _Draft)
            else _Draft.model_validate_json(res.text or "")
        )
        return guard(draft, r, model=self.model, latency_ms=latency,
                     cost=cost_usd(self.model, prompt, out))  # fmt: skip


def guard(d: _Draft, r: LookupResult, *, model: str, latency_ms: int | None = None,
          cost: float | None = None) -> Assessment:  # fmt: skip
    """Keep only receipt ids that exist; with no valid citation the reading is 'unclear'."""
    valid = {x.id for x in r.receipts}
    cites = [c for c in dict.fromkeys(d.cites) if c in valid]
    reading = d.llm_reading if cites else "unclear"
    return Assessment(
        model=model,
        summary=d.summary.strip(),
        origin=d.origin.strip() or None,
        corroboration=d.corroboration.strip() or None,
        spread_pattern=d.spread_pattern.strip() or None,
        counter_evidence=[c.strip() for c in d.counter_evidence if c.strip()][:5],
        would_change=[c.strip() for c in d.would_change if c.strip()][:4],
        cites=cites,
        llm_reading=reading,
        latency_ms=latency_ms,
        cost_usd=cost,
    )
