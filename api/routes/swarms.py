"""Meme swarms: pictures many Bluesky accounts posted, and whether those posts act like a swarm.

GET /api/swarms?limit=24&verdict=all      -> SwarmWall (safe groups only; swarm verdicts first)
GET /api/swarms/{id}                      -> SwarmDetail (404 when missing or not safe to show)
GET /api/swarms/{id}/picture              -> the picture's bytes (SwarmDetail.picture points here)

Written by the swarm worker (python -m swarm run). A verdict describes how a group of posts
behaved, never one account; posts are listed as links without labels on their authors.
"""

from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from api.deps import get_swarm_store
from swarm.model import SwarmDetail, SwarmWall
from swarm.picture import CDN
from swarm.store import SwarmStore

router = APIRouter(prefix="/api/swarms", tags=["swarms"])

PICTURE_MAX_BYTES = 6_000_000
PICTURE_TIMEOUT_S = 8.0


@router.get("", response_model=SwarmWall)
def list_swarms(
    store: Annotated[SwarmStore, Depends(get_swarm_store)],
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    verdict: Literal["swarm", "organic", "unclear", "all"] = "all",
) -> SwarmWall:
    """Safe groups seen in the last day. `all`: swarm verdicts by score, then the rest by score."""
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
    """The picture from Bluesky's CDN. Tests replace this."""
    with httpx.Client(timeout=PICTURE_TIMEOUT_S, follow_redirects=False) as http:
        with http.stream("GET", url) as r:
            r.raise_for_status()
            mime = r.headers.get("content-type", "").split(";")[0].strip().lower()
            data = b""
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > PICTURE_MAX_BYTES:
                    raise ValueError("picture too large")
    return data, mime


@router.get("/{swarm_id}/picture")
def get_swarm_picture(
    swarm_id: str, store: Annotated[SwarmStore, Depends(get_swarm_store)]
) -> Response:
    """Only a stored, safe group's own picture is fetched: the URL is built from the group's
    row (swarm.picture.full_url), never from the request."""
    detail = store.detail(swarm_id) if len(swarm_id) <= 64 else None
    if detail is None or not detail.picture.startswith(CDN + "/"):
        raise HTTPException(404, "swarm not found")
    try:
        data, mime = fetch_picture(detail.picture)
    except Exception as exc:
        raise HTTPException(502, "picture unavailable") from exc
    if not mime.startswith("image/"):
        raise HTTPException(502, "picture unavailable")
    return Response(data, media_type=mime, headers={"Cache-Control": "public, max-age=3600"})
