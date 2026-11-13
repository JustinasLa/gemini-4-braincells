"""Read-only retrieval of Bluesky media blobs.

Images come from Bluesky's public image CDN and videos as their thumbnail frame from the video
CDN, both addressed by (did, blob CID), so no PDS lookup is needed (most accounts in our DB have
no `pds_host` yet). The author's PDS (`com.atproto.sync.getBlob`) is a fallback for images.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlencode

import httpx

MAX_BYTES = 10 * 1024 * 1024


class BlobFetchError(RuntimeError):
    """Raised when a media blob cannot be retrieved."""


@dataclass(frozen=True, slots=True)
class Blob:
    data: bytes
    mime_type: str
    url: str
    media_kind: str = "image"  # image | video_thumbnail


def blob_url(pds_host: str, did: str, cid: str) -> str:
    base = pds_host if pds_host.startswith(("http://", "https://")) else f"https://{pds_host}"
    return f"{base.rstrip('/')}/xrpc/com.atproto.sync.getBlob?{urlencode({'did': did, 'cid': cid})}"


def image_cdn_url(did: str, cid: str) -> str:
    return f"https://cdn.bsky.app/img/feed_fullsize/plain/{quote(did, safe=':')}/{quote(cid)}@jpeg"


def video_thumbnail_url(did: str, cid: str) -> str:
    return f"https://video.bsky.app/watch/{quote(did, safe=':')}/{quote(cid)}/thumbnail.jpg"


def media_urls(did: str, cid: str, embed_kind: str | None, pds_host: str | None) -> list[tuple]:
    """Candidate (url, media_kind) pairs in the order to try them."""
    video = (video_thumbnail_url(did, cid), "video_thumbnail")
    if embed_kind == "video":
        return [video]
    urls = [(image_cdn_url(did, cid), "image")]
    if embed_kind != "images":  # recordWithMedia etc.: could be either
        urls.append(video)
    if pds_host:
        urls.append((blob_url(pds_host, did, cid), "image"))
    return urls


class BlobFetcher:
    """Fetch media without publishing or using authenticated Bluesky APIs."""

    def __init__(self, *, timeout_s: float = 20.0, client: httpx.Client | None = None):
        self._http = client or httpx.Client(
            timeout=timeout_s,
            follow_redirects=True,
            headers={"User-Agent": "noise-radar-media/0.1 (read-only research)"},
        )
        self._owned = client is None

    def close(self) -> None:
        if self._owned:
            self._http.close()

    def __enter__(self) -> BlobFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch(
        self, *, did: str, cid: str, pds_host: str | None = None, embed_kind: str | None = None
    ) -> Blob:
        errors = []
        for url, media_kind in media_urls(did, cid, embed_kind, pds_host):
            try:
                response = self._http.get(url)
            except httpx.HTTPError as exc:
                errors.append(f"{type(exc).__name__}")
                continue
            mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
            if response.status_code != 200:
                errors.append(f"HTTP {response.status_code}")
                continue
            if not mime_type.startswith("image/"):
                errors.append(f"not an image ({mime_type or 'no content-type'})")
                continue
            if len(response.content) > MAX_BYTES:
                errors.append(f"too large ({len(response.content)} bytes)")
                continue
            return Blob(response.content, mime_type, url, media_kind)
        raise BlobFetchError(f"media {cid} of {did} not retrievable: {', '.join(errors)}")
