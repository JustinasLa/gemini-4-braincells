"""Read-only retrieval of Bluesky media blobs.

Images come from Bluesky's public image CDN and videos as their thumbnail frame from the video
CDN, both addressed by (did, blob CID), so no PDS lookup is needed (most accounts in our DB have
no `pds_host` yet). The author's PDS (`com.atproto.sync.getBlob`) is a fallback for images.
"""

from __future__ import annotations

import time
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
        self._timeout_s = timeout_s

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
                # streamed: pds_host comes from the account's DID document, so the body is read
                # only after status and type pass, and is cut at MAX_BYTES and timeout_s in total
                with self._http.stream("GET", url) as response:
                    mime_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                    if response.status_code != 200:
                        errors.append(f"HTTP {response.status_code}")
                        continue
                    if not mime_type.startswith("image/"):
                        errors.append(f"not an image ({mime_type or 'no content-type'})")
                        continue
                    data = self._read_capped(response)
            except httpx.HTTPError as exc:
                errors.append(f"{type(exc).__name__}")
                continue
            if data is None:
                errors.append(f"over {MAX_BYTES} bytes or {self._timeout_s:g} s")
                continue
            return Blob(data, mime_type, url, media_kind)
        raise BlobFetchError(f"media {cid} of {did} not retrievable: {', '.join(errors)}")

    def _read_capped(self, response: httpx.Response) -> bytes | None:
        """The body, or None once it passes MAX_BYTES or timeout_s (the client's timeout is per
        read, so a server dripping bytes would otherwise never time out)."""
        deadline = time.monotonic() + self._timeout_s
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES or time.monotonic() > deadline:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
