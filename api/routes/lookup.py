"""POST /api/lookup + GET /api/lookup/{id}: trace one link with live data (lookup/README.md)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from common.config import get_settings
from lookup.jobs import JobStore, RateLimited
from lookup.models import LookupInput
from lookup.service import Deps

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/lookup", tags=["lookup"])

_store: JobStore | None = None


def _db_store():
    from api.store import Store
    from common.db import connect

    @contextmanager
    def factory():
        with connect() as conn:
            yield Store(conn)

    return factory


def make_deps() -> Deps:
    """Clients for one lookup. Missing keys turn the matching steps into 'skipped'."""
    from lookup import cache
    from lookup.bsky import BlueskyClient
    from sources.oxylabs import OxylabsClient

    s = get_settings()
    nemotron = reader = second = None
    # the dedicated Verda Super VL endpoint when keyed, else NVIDIA's build API
    key, base, model = (
        (s.verda_inference_key, s.verda_base_url, s.verda_model)
        if s.verda_inference_key
        else (s.nvidia_api_key, s.nvidia_base_url, s.nvidia_media_model)
    )
    if s.lookup_media and key:
        try:
            from media.nemotron import NemotronClient

            # the lookup has a time budget: one attempt, bounded wait (the worker retries more)
            nemotron = NemotronClient(
                key,
                base_url=base,
                model=model,
                rpm=s.media_rpm,
                max_tokens=s.media_max_tokens,
                reasoning=s.media_reasoning,
                timeout_s=20,
                max_retries=1,
            )
        except Exception as exc:  # optional: a lookup without media reading still works
            log.warning("lookup: Nemotron unavailable: %s", exc)
    if s.lookup_nemotron_reading and key:
        try:
            from lookup.assess import NemotronAssessor

            reader = NemotronAssessor(
                key,
                base_url=base,
                model=model,
                reasoning=s.lookup_reading_reasoning,
                max_tokens=s.lookup_reading_max_tokens,
                timeout_s=s.lookup_reading_timeout_s,
            )
        except Exception as exc:
            log.warning("lookup: Nemotron reading unavailable: %s", exc)
    if s.lookup_gemini and s.gemini_api_key:
        try:
            from lookup.assess import Assessor

            # each call is bounded by the reading's own deadline (lookup/assess.py)
            second = Assessor(s.gemini_api_key, s.gemini_model)
        except Exception as exc:
            log.warning("lookup: Gemini unavailable: %s", exc)
    oxylabs = OxylabsClient(s.oxylabs_web_api_key, timeout_s=75, max_retries=1)
    if s.lookup_provider_cache_ttl_s > 0:
        oxylabs = cache.CachedOxylabs(oxylabs, cache.oxylabs_cache(s.lookup_provider_cache_ttl_s))
    return Deps(
        oxylabs=oxylabs,
        bsky=BlueskyClient(),
        store=_db_store(),
        nemotron=nemotron,
        # Nemotron's reading first; with no Nemotron, Gemini's is the only (primary) one
        assessor=reader or second,
        second_assessor=second if reader else None,
        media_cache=cache.MEDIA,
        media_urls=cache.MEDIA_URLS,
        verify_max=s.lookup_verify_max,
        profiles_max=s.lookup_profiles_max,
        time_budget_s=s.lookup_time_budget_s,
        second_reading_s=s.lookup_second_reading_s,
    )


def get_jobs() -> JobStore:
    global _store
    s = get_settings()
    if not s.oxylabs_web_api_key:
        raise HTTPException(503, "Live lookups are not configured on this server (no Oxylabs key).")
    if _store is None:
        _store = JobStore(
            make_deps,
            max_per_hour=s.lookup_max_per_hour,
            max_per_client_per_hour=s.lookup_max_per_ip_per_hour,
            cache_ttl_s=s.lookup_cache_ttl_s,
            job_ttl_s=s.lookup_job_ttl_s,
        )
    return _store


def _client(request: Request) -> str:
    # Behind Traefik + Caddy: the first X-Forwarded-For hop is the visitor.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


@router.post("", status_code=202)
def start_lookup(
    body: LookupInput, request: Request, jobs: Annotated[JobStore, Depends(get_jobs)]
) -> JSONResponse:
    url = (body.url or "").strip()
    text = (body.text or "").strip()
    if "\x00" in url or "\x00" in text:
        raise HTTPException(400, "input contains a NUL character")
    if url:
        if len(url) > 2000 or not url.lower().startswith(("http://", "https://")):
            raise HTTPException(400, "url must be an http(s) link")
        inp = LookupInput(url=url)
    elif text:
        if len(text.split()) < 5:
            raise HTTPException(400, "text needs at least 5 words to search for")
        inp = LookupInput(text=text[:2000])
    else:
        raise HTTPException(400, "give a url or a text")
    try:
        job = jobs.start(inp, _client(request))
    except RateLimited as exc:
        raise HTTPException(429, str(exc)) from exc
    snap = job.snapshot()
    return JSONResponse(snap, status_code=200 if snap["state"] != "running" else 202)


@router.get("/{job_id}")
def get_lookup(job_id: str, jobs: Annotated[JobStore, Depends(get_jobs)]) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown or expired lookup id")
    return job.snapshot()


SSE_TICK_S = 0.15  # how often the stream checks the job for news
SSE_PING_S = 10.0
SSE_MAX_S = 300.0  # a lookup is over long before this; the stream never outlives it


@router.get("/{job_id}/events")
async def lookup_events(
    job_id: str, request: Request, jobs: Annotated[JobStore, Depends(get_jobs)]
) -> StreamingResponse:
    """The same LookupResult as GET /api/lookup/{id}, pushed as server-sent events: one
    `snapshot` event at once and one whenever the job changes (`ping` events in between),
    ending after the snapshot whose state is no longer "running". Clients that can't stream
    fall back to polling (web/trackdown/src/data/lookup.ts)."""
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "unknown or expired lookup id")

    async def stream():
        last, sent_at, opened = None, 0.0, time.monotonic()
        while True:
            snap = job.snapshot()
            body = json.dumps(snap, ensure_ascii=False, separators=(",", ":"))
            now = time.monotonic()
            if body != last:
                yield f"event: snapshot\ndata: {body}\n\n"
                last, sent_at = body, now
            elif now - sent_at >= SSE_PING_S:
                yield "event: ping\ndata: {}\n\n"  # an event, so EventSource sees it
                sent_at = now
            if snap["state"] != "running" or now - opened > SSE_MAX_S:
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(SSE_TICK_S)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )
