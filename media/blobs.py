"""Read-only retrieval of Bluesky media blobs from an account's PDS."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx


class BlobFetchError(RuntimeError):
    """Raised when a PDS blob cannot be retrieved."""


@dataclass(frozen=True, slots=True)
class Blob:
    data: bytes
    mime_type: str
    url: str


def blob_url(pds_host: str, did: str, cid: str) -> str:
    base = pds_host if pds_host.startswith(("http://", "https://")) else f"https://{pds_host}"
    return f"{base.rstrip('/')}/xrpc/com.atproto.sync.getBlob?{urlencode({'did': did, 'cid': cid})}"


class BlobFetcher:
    """Fetch media without publishing or using authenticated Bluesky APIs."""

    def __init__(self, *, timeout_s: float = 20.0, client: httpx.Client | None = None):
        self._http = client or httpx.Client(timeout=timeout_s, follow_redirects=True)
        self._owned = client is None

    def close(self) -> None:
        if self._owned:
            self._http.close()

    def __enter__(self) -> BlobFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch(self, *, pds_host: str | None, did: str, cid: str) -> Blob:
        if not pds_host:
            raise BlobFetchError(f"no PDS host for {did}; cannot fetch media {cid}")
        url = blob_url(pds_host, did, cid)
        try:
            response = self._http.get(url)
        except httpx.HTTPError as exc:
            raise BlobFetchError(f"PDS request failed for {cid}: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise BlobFetchError(f"PDS returned HTTP {response.status_code} for media {cid}")
        mime_type = response.headers.get(
            "content-type", "application/octet-stream"
        ).split(";", 1)[0]
        return Blob(data=response.content, mime_type=mime_type, url=url)