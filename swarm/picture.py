"""Picture identity for meme swarms: thumbnail URLs, perceptual hashes and "same picture?".

Equal Bluesky blob CIDs are the exact same file. Different files can still be the same picture
(re-encoded, resized, re-uploaded), so files are merged when their CDN thumbnails match:
1. pHash and dHash close (64-bit hashes from media.variants.fingerprint) and the same aspect ratio,
   a cheap prefilter; then
2. the pixels agree: both pictures squeezed to 64x64 grayscale, no 4x4 block may differ by more
   than PIXEL_BLOCK_LIMIT gray levels on average and no pixel by more than PIXEL_MAX_LIMIT.
Step 2 is what keeps templates apart. Measured on production (14 Nov, 1,450 candidate files, the
merged groups checked by eye): hash-close pairs that were really the same picture differed by at
most 3.8 levels in their worst 4x4 block and 14 in their worst pixel, while a supplement-bottle
template with 39 product names, NBA head shots on one background, court filing pages ("EXHIBIT
A/B/C" covers, text pages) and two flight-map screenshots minutes apart were hash-close (pHash 0-8
bits) but differed by more (4.1-110 in a block, 22-105 in a pixel).
Low-detail pictures (a flat colour, a blank card) are only ever matched by exact CID.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote

import httpx
import numpy as np
from PIL import Image, ImageOps, ImageStat

from media.variants import fingerprint

log = logging.getLogger(__name__)

CDN = "https://cdn.bsky.app/img"
MAX_THUMB_BYTES = 5 * 1024 * 1024
UA = {"User-Agent": "noise-radar-swarm/0.1 (read-only research)"}

# Grouping files seen in the firehose (both are thumbnails of the same CDN pipeline).
GROUP_PHASH = 8
GROUP_DHASH = 10
# An uploaded picture (any size or encoder) against stored groups. Close on both hashes and on
# pixels is a match; within the looser band it must pass media.variants.relate instead.
MATCH_PHASH = 6
MATCH_DHASH = 8
MAYBE_PHASH = 12
MAYBE_DHASH = 16
# Both pictures as 64x64 grayscale: the worst 4x4 block may differ by this much on average, and
# no single pixel by more than PIXEL_MAX_LIMIT (0-255).
PIXEL_BLOCK_LIMIT = 4.0
PIXEL_MAX_LIMIT = 20.0
# An uploaded picture in the looser hash band (MAYBE_*) is verified against the stored 64x64 pixels
# of the group's file, offline: a re-encode or resize by the uploader is let through, a crop or
# another picture on the same template is not.
VERIFY_BLOCK_LIMIT = 5.0
VERIFY_MAX_LIMIT = 28.0
TINY_SIDE = 64
# Grayscale standard deviation below this: too little detail for a hash to mean anything.
MIN_DETAIL = 8.0
# Aspect ratios further apart than this are different pictures (or a crop: not merged).
ASPECT_TOLERANCE = 0.04


def thumb_url(did: str, cid: str) -> str:
    return f"{CDN}/feed_thumbnail/plain/{quote(did, safe=':')}/{quote(cid)}@jpeg"


def full_url(did: str, cid: str) -> str:
    return f"{CDN}/feed_fullsize/plain/{quote(did, safe=':')}/{quote(cid)}@jpeg"


@dataclass(frozen=True, slots=True)
class PictureHash:
    phash: str  # 16 hex chars
    dhash: str
    detail: float  # grayscale standard deviation of the working image
    width: int
    height: int
    tiny: bytes = b""  # TINY_SIDE x TINY_SIDE grayscale pixels, row by row

    @property
    def detailed(self) -> bool:
        return self.detail >= MIN_DETAIL

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 1.0


def hash_picture(data: bytes) -> PictureHash:
    """Perceptual hashes of image bytes. Raises ValueError when they can't be decoded."""
    fp = fingerprint(data)
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.draft("L", (512, 512))
            gray = ImageOps.exif_transpose(im).convert("L")
            tiny = gray.resize((TINY_SIDE, TINY_SIDE), Image.Resampling.BOX)
            gray.thumbnail((256, 256))
            detail = float(ImageStat.Stat(gray).stddev[0])
    except Exception as exc:  # fingerprint decoded it, so this is unlikely
        raise ValueError("cannot decode image bytes") from exc
    return PictureHash(fp.phash, fp.dhash, detail, fp.width, fp.height, tiny.tobytes())


def pixel_distance(a: PictureHash, b: PictureHash) -> tuple[float, float] | None:
    """(worst 4x4-block mean, worst single pixel) absolute difference of the 64x64 grayscale
    pictures, 0-255; None when either has no pixels stored."""
    n = TINY_SIDE * TINY_SIDE
    if len(a.tiny) != n or len(b.tiny) != n:
        return None
    x = np.frombuffer(a.tiny, dtype=np.uint8).astype(np.float32)
    y = np.frombuffer(b.tiny, dtype=np.uint8).astype(np.float32)
    d = np.abs(x - y).reshape(TINY_SIDE // 4, 4, TINY_SIDE // 4, 4)
    return float(d.mean(axis=(1, 3)).max()), float(d.max())


def pixels_close(
    a: PictureHash,
    b: PictureHash,
    block: float = PIXEL_BLOCK_LIMIT,
    worst: float = PIXEL_MAX_LIMIT,
) -> bool:
    dist = pixel_distance(a, b)
    return dist is not None and dist[0] <= block and dist[1] <= worst


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def aspect_close(a: PictureHash, b: PictureHash) -> bool:
    return abs(a.aspect / b.aspect - 1) <= ASPECT_TOLERANCE


def same_picture(
    a: PictureHash, b: PictureHash, *, phash: int = GROUP_PHASH, dhash: int = GROUP_DHASH
) -> bool:
    if not (a.detailed and b.detailed and aspect_close(a, b)):
        return False
    if hamming(a.phash, b.phash) > phash or hamming(a.dhash, b.dhash) > dhash:
        return False
    return pixels_close(a, b)


def group_files(files: list[str], hashes: dict[str, PictureHash | None]) -> list[list[str]]:
    """Union of files that are the same picture (single link over verified pairs).

    `files` keeps its order inside each group and groups come out in order of their first file.
    A file without a hash (fetch failed, not decodable) stays alone.
    """
    idx = [i for i, f in enumerate(files) if (h := hashes.get(f)) is not None and h.detailed]
    parent = list(range(len(files)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if len(idx) > 1:
        hs = [hashes[files[i]] for i in idx]
        p = np.array([int(h.phash, 16) for h in hs], dtype=np.uint64)
        d = np.array([int(h.dhash, 16) for h in hs], dtype=np.uint64)
        asp = np.array([h.aspect for h in hs])
        for k in range(len(idx) - 1):
            dp = np.bitwise_count(p[k + 1 :] ^ p[k])
            dd = np.bitwise_count(d[k + 1 :] ^ d[k])
            da = np.abs(asp[k + 1 :] / asp[k] - 1)
            for j in np.nonzero(
                (dp <= GROUP_PHASH) & (dd <= GROUP_DHASH) & (da <= ASPECT_TOLERANCE)
            )[0]:
                ra, rb = find(idx[k]), find(idx[k + 1 + int(j)])
                if ra != rb and pixels_close(hs[k], hs[k + 1 + int(j)]):
                    parent[max(ra, rb)] = min(ra, rb)
    groups: dict[int, list[str]] = {}
    for i, f in enumerate(files):
        groups.setdefault(find(i), []).append(f)
    return list(groups.values())


Fetch = Callable[[str], bytes | None]


class ThumbFetcher:
    """GET thumbnails from the public Bluesky CDN, a few at a time. None when unavailable.
    A body over MAX_THUMB_BYTES is cut off while it streams, not after."""

    def __init__(self, client: httpx.Client | None = None, *, workers: int = 16):
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=True,
            headers=UA,
            limits=httpx.Limits(max_connections=32),
        )
        self.workers = workers

    def _get(self, url: str) -> tuple[int, bytes | None]:
        with self._client.stream("GET", url) as r:
            if r.status_code != 200:
                return r.status_code, None
            data = bytearray()
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > MAX_THUMB_BYTES:
                    return 200, None
            return 200, bytes(data)

    def __call__(self, url: str) -> bytes | None:
        for _ in range(2):
            try:
                status, data = self._get(url)
            except httpx.HTTPError:
                continue
            if status == 200:
                return data
            if status < 500 and status != 429:
                return None
        return None

    def close(self) -> None:
        self._client.close()


def hash_files(
    files: list[tuple[str, str]], fetch: Fetch | ThumbFetcher, workers: int = 16
) -> dict[str, PictureHash | None]:
    """{cid: hash or None} for (cid, did) pairs, fetching each thumbnail once."""

    def one(item: tuple[str, str]) -> PictureHash | None:
        cid, did = item
        data = fetch(thumb_url(did, cid))
        if not data:
            return None
        try:
            return hash_picture(data)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return dict(zip([c for c, _ in files], pool.map(one, files), strict=True))
