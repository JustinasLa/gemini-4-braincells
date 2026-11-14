"""Meme swarms: pictures many Bluesky accounts posted, and whether those posts act like a swarm.

GET /api/swarms?limit=24&verdict=all      -> SwarmWall (safe, fresh groups; memes first)
GET /api/swarms/{id}                      -> SwarmDetail (404 when missing, stale or not safe)
GET /api/swarms/{id}/picture              -> the picture's bytes (SwarmDetail.picture points here)

Written by the swarm worker (python -m swarm run). A verdict describes how a group of posts
behaved, never one account; posts are listed as links without labels on their authors.
"""

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from api.deps import get_swarm_sessions, get_swarm_store
from swarm.model import SwarmDetail, SwarmWall
from swarm.picture import CDN, full_url
from swarm.store import SwarmStore

router = APIRouter(prefix="/api/swarms", tags=["swarms"])

PICTURE_MAX_BYTES = 6_000_000
PICTURE_TIMEOUT_S = 8.0  # the whole download, not per read
PICTURE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
PICTURE_HEADERS = {"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"}
# Pictures by blob CID (a CID's bytes never change). Safety is checked against the database on
# every request before anything, cached or not, is served.
CACHE_MAX_BYTES = 48_000_000
CACHE_MAX_ITEMS = 64
# Concurrent CDN downloads; a request that can't get a slot quickly is a 503, so a crowd opening
# detail pages cannot use up the API's worker threads.
_fetch_slots = threading.BoundedSemaphore(4)
_cache: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
_cache_bytes = 0
_cache_lock = threading.Lock()

Sessions = Callable[[], AbstractContextManager[SwarmStore]]


@router.get("", response_model=SwarmWall)
def list_swarms(
    store: Annotated[SwarmStore, Depends(get_swarm_store)],
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    verdict: Literal["swarm", "organic", "unclear", "all"] = "all",
) -> SwarmWall:
    """Safe groups a recent pass saw: memes first, then swarm verdicts, by score."""
    return store.wall(limit=limit, verdict=verdict)


@router.get("/{swarm_id}", response_model=SwarmDetail)
def get_swarm(swarm_id: str, store: Annotated[SwarmStore, Depends(get_swarm_store)]) -> SwarmDetail:
    detail = store.detail(swarm_id) if len(swarm_id) <= 64 else None
    if detail is None:
        raise HTTPException(404, "swarm not found")
    # Bluesky's image CDN sends no CORS header, so a page cannot read its pictures as bytes;
    # served from here, the picture can be handed to an image lookup ("Trace it")
    return detail.model_copy(update={"picture": f"/api/swarms/{detail.id}/picture"})


def fetch_picture(url: str) -> tuple[bytes, str]:
    """The picture from Bluesky's CDN, within PICTURE_TIMEOUT_S overall. Tests replace this."""
    started = time.monotonic()
    timeout = httpx.Timeout(PICTURE_TIMEOUT_S, connect=4.0)
    with httpx.Client(timeout=timeout, follow_redirects=False) as http:
        with http.stream("GET", url) as r:
            r.raise_for_status()
            mime = r.headers.get("content-type", "").split(";")[0].strip().lower()
            data = bytearray()
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > PICTURE_MAX_BYTES:
                    raise ValueError("picture too large")
                if time.monotonic() - started > PICTURE_TIMEOUT_S:
                    raise TimeoutError("picture download too slow")
    return bytes(data), mime


def _cached(cid: str) -> tuple[bytes, str] | None:
    with _cache_lock:
        hit = _cache.get(cid)
        if hit is not None:
            _cache.move_to_end(cid)
        return hit


def _remember(cid: str, data: bytes, mime: str) -> None:
    global _cache_bytes
    if len(data) > CACHE_MAX_BYTES // 4:
        return
    with _cache_lock:
        if cid in _cache:
            return
        _cache[cid] = (data, mime)
        _cache_bytes += len(data)
        while _cache and (_cache_bytes > CACHE_MAX_BYTES or len(_cache) > CACHE_MAX_ITEMS):
            _, (old, _) = _cache.popitem(last=False)
            _cache_bytes -= len(old)


def clear_picture_cache() -> None:
    global _cache_bytes
    with _cache_lock:
        _cache.clear()
        _cache_bytes = 0


@router.get("/{swarm_id}/picture")
def get_swarm_picture(swarm_id: str, sessions: Annotated[Sessions, Depends(get_swarm_sessions)]):
    """Only a stored, safe, fresh group's own picture: the URL is built from the group's row
    (swarm.picture.full_url), never from the request. The database connection is released
    before the CDN is asked."""
    source = None
    if len(swarm_id) <= 64:
        with sessions() as store:
            source = store.picture_source(swarm_id)
    if source is None:
        raise HTTPException(404, "swarm not found")
    did, cid = source
    hit = _cached(cid)
    if hit is None:
        url = full_url(did, cid)
        if not url.startswith(CDN + "/"):
            raise HTTPException(404, "swarm not found")
        if not _fetch_slots.acquire(timeout=3.0):
            raise HTTPException(503, "busy", headers={"Retry-After": "5"})
        try:
            data, mime = fetch_picture(url)
        except Exception as exc:
            raise HTTPException(502, "picture unavailable") from exc
        finally:
            _fetch_slots.release()
        if mime not in PICTURE_TYPES:
            raise HTTPException(502, "picture unavailable")
        _remember(cid, data, mime)
        hit = (data, mime)
    data, mime = hit
    return Response(data, media_type=mime, headers=PICTURE_HEADERS)
