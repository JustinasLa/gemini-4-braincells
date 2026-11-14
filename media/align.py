"""Does one image contain the same photograph as another? Feature matching that survives the
edits memes are made of: captions over the picture, poster frames around it, crops, resizes and
re-encodes, and a face pasted over part of it.

`media.variants.relate` compares two renditions pixel by pixel and calls a re-captioned crop
"different" (17 of 18 TinEye copies of a captioned Disaster Girl, 2026-11-14). This module asks
a looser, separate question: ORB keypoints plus a RANSAC similarity transform (scale, rotation,
shift; no shear) between the two pictures. Enough geometrically consistent keypoints mean the
same photograph is in both, wherever it sits and whatever was drawn over it.

Both images are scaled to a 512 px long side first, so 100 px search-engine thumbnails (upscaled)
and 2000 px originals meet at one scale. A match takes 20-90 ms on one CPU core.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageOps

_SIDE = 512
_FEATURES = 2000
_RATIO = 0.8  # Lowe's ratio test
_REPROJECT_PX = 6.0
# Measured 2026-11-14 on Disaster Girl copies (captioned, poster-framed, face-swapped, 120 px
# thumbnails): 95-226 inliers at 0.81-0.95 of the ratio-tested matches. Unrelated photos give
# a handful of chance inliers at a low ratio (tests/test_align.py).
MIN_INLIERS = 30
MIN_RATIO = 0.45
_SCALE_RANGE = (0.2, 5.0)
_MAX_ROTATION_DEG = 12.0


@dataclass(frozen=True, slots=True)
class Placement:
    """Where the base photograph sits inside the other image.

    `box`: the base's corners mapped into the other image, as (left, top, right, bottom)
    fractions of the OTHER image (may run past 0..1 when the other is a crop of the base).
    `coverage`: the share of the other image the base's photo covers (1.0 = nothing around it;
    a poster frame leaves 0.5-0.75). `scale`: other px per base px at the 512 px working size.
    """

    inliers: int
    ratio: float
    scale: float
    rotation: float
    box: tuple[float, float, float, float]
    coverage: float

    @property
    def same_photo(self) -> bool:
        return self.inliers >= MIN_INLIERS and self.ratio >= MIN_RATIO


def _gray(data: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA", "P"):
            image = image.convert("RGBA")
            backdrop = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(backdrop, image)
        gray = image.convert("L")
        if min(gray.size) < 16:
            raise ValueError("image too small to match")
        scale = _SIDE / max(gray.size)
        size = (max(1, round(gray.width * scale)), max(1, round(gray.height * scale)))
        gray = gray.resize(
            size, Image.Resampling.LANCZOS if scale < 1 else Image.Resampling.BICUBIC
        )
        return np.asarray(gray, dtype=np.uint8)


class Matcher:
    """The base image's keypoints, computed once and matched against many candidates."""

    def __init__(self, base: bytes):
        self._orb = cv2.ORB_create(_FEATURES, fastThreshold=10, edgeThreshold=15)
        self.gray = _gray(base)
        self.keypoints, self.descriptors = self._orb.detectAndCompute(self.gray, None)
        self._bf = cv2.BFMatcher(cv2.NORM_HAMMING)

    def locate(self, other: bytes) -> Placement | None:
        """Where the base photograph is in `other`, or None when it isn't found there.

        Raises ValueError (and PIL errors) for undecodable images."""
        if self.descriptors is None or len(self.keypoints) < 8:
            return None
        gray = _gray(other)
        keypoints, descriptors = self._orb.detectAndCompute(gray, None)
        if descriptors is None or len(keypoints) < 8:
            return None
        pairs = self._bf.knnMatch(self.descriptors, descriptors, k=2)
        good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < _RATIO * n.distance]
        if len(good) < 8:
            return None
        src = np.float32([self.keypoints[m.queryIdx].pt for m in good])
        dst = np.float32([keypoints[m.trainIdx].pt for m in good])
        transform, mask = cv2.estimateAffinePartial2D(
            src,
            dst,
            method=cv2.RANSAC,
            ransacReprojThreshold=_REPROJECT_PX,
            maxIters=4000,
            confidence=0.995,
        )
        if transform is None or mask is None:
            return None
        inliers = int(mask.sum())
        scale = float(np.hypot(transform[0, 0], transform[1, 0]))
        rotation = float(np.degrees(np.arctan2(transform[1, 0], transform[0, 0])))
        if not (_SCALE_RANGE[0] <= scale <= _SCALE_RANGE[1]) or abs(rotation) > _MAX_ROTATION_DEG:
            return None
        h, w = self.gray.shape
        corners = np.float32([[0, 0, 1], [w, 0, 1], [w, h, 1], [0, h, 1]])
        mapped = corners @ transform.T
        oh, ow = gray.shape
        left, top = mapped.min(axis=0)
        right, bottom = mapped.max(axis=0)
        inside = max(0.0, min(ow, right) - max(0.0, left)) * max(
            0.0, min(oh, bottom) - max(0.0, top)
        )
        return Placement(
            inliers=inliers,
            ratio=round(inliers / len(good), 3),
            scale=round(scale, 3),
            rotation=round(rotation, 2),
            box=(
                round(float(left / ow), 4),
                round(float(top / oh), 4),
                round(float(right / ow), 4),
                round(float(bottom / oh), 4),
            ),
            coverage=round(float(inside / (ow * oh)), 3),
        )


def photo_crop(other: bytes, placement: Placement, *, side: int = 512) -> bytes:
    """The part of `other` where the base photograph sits (its poster frame or the page around
    it cut away), as a JPEG: what to compare with another copy of the photograph."""
    with Image.open(io.BytesIO(other)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        left, top, right, bottom = placement.box
        box = (
            max(0, round(left * image.width)),
            max(0, round(top * image.height)),
            min(image.width, round(right * image.width)),
            min(image.height, round(bottom * image.height)),
        )
        if box[2] - box[0] < 16 or box[3] - box[1] < 16:
            raise ValueError("photo area too small")
        crop = image.crop(box)
        crop.thumbnail((side, side), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        crop.save(out, "JPEG", quality=90)
        return out.getvalue()
