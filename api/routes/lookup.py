"""POST /api/lookup + GET /api/lookup/{id}: trace one link with live data (lookup/README.md)."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

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
    from lookup.bsky import BlueskyClient
    from sources.oxylabs import OxylabsClient

    s = get_settings()
    nemotron = assessor = None
    if s.lookup_media and (s.verda_inference_key or s.nvidia_api_key):
        try:
            from media.nemotron import NemotronClient

            nemotron = NemotronClient.from_settings(s)
        except Exception as exc:  # optional: a lookup without media reading still works
            log.warning("lookup: Nemotron unavailable: %s", exc)
    if s.lookup_gemini and s.gemini_api_key:
        try:
            from lookup.assess import Assessor

            assessor = Assessor(s.gemini_api_key, s.gemini_model)
        except Exception as exc:
            log.warning("lookup: Gemini unavailable: %s", exc)
    return Deps(
        oxylabs=OxylabsClient(s.oxylabs_web_api_key, timeout_s=75, max_retries=1),
        bsky=BlueskyClient(),
        store=_db_store(),
        nemotron=nemotron,
        assessor=assessor,
        verify_max=s.lookup_verify_max,
        profiles_max=s.lookup_profiles_max,
        time_budget_s=s.lookup_time_budget_s,
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
