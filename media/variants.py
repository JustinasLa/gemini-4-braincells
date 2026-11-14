"""CPU-only comparison of image encodings, localized edits, and rectangular crops.

Edited-region boxes use (left, top, right, bottom) fractions of the oriented BASE.
Crop boxes locate the other picture inside the base, in fractions of BASE, and
are None when the other picture is larger; the note identifies that direction.
Crops with a small additional edit can still be classified as cropped. Unknown means
a decoded image has a side below 16 pixels or insufficient grayscale variation.

The 300 ms budget targets 2048-pixel photo/meme pairs, not full-entropy noise
stored as quality-95, 4:4:4 progressive JPEGs: entropy decoding alone can exceed
that budget even with luminance-only draft decoding.
"""

import hashlib
import io
from dataclasses import dataclass
from typing import Literal

import imagehash
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError
from scipy import ndimage, signal

RelationKind = Literal["same", "resized", "cropped", "edited", "different", "unknown"]

# A 512-pixel working image keeps decoding/reduction and hashing inexpensive.
_WORK_SIDE = 512
# A 256-pixel alignment view keeps caption strokes visible at low cost.
_ALIGN_SIDE = 256
# 32 blocks per axis averages 8x8 pixels to suppress individual JPEG artifacts.
_GRID_SIDE = 32
# A 512-pixel, 64-block fallback resolves small text before declaring re-encoding.
_FINE_GRID_SIDE = 64
# Below 16 original pixels per side, there is too little spatial evidence.
_MIN_SIDE = 16
# Less than two grayscale levels of deviation is effectively blank at this scale.
_MIN_STD = 2.0
# A 1.5% aspect tolerance admits integer rounding during small resizes.
_ASPECT_TOLERANCE = 0.015
# Synthetic q=30..95 re-encodes differ by 0..2 bits; eight allows extra codec drift.
_RESIZE_DISTANCE = 8
# Real caption/patch edits reach 12 bits; twenty allows edits but needs alignment.
_EDIT_DISTANCE = 20
# Normalized differences below .24 in most blocks indicate global alignment.
_ALIGN_MEDIAN = 0.24
# Check the textured half too: a gull's 85% crop had .15 overall but .38 here.
_TEXTURE_QUANTILE = 0.50
# Two gray levels exclude blank background blocks from that alignment check.
_TEXTURE_MIN_STD = 2.0
# Coarse JPEG residuals stay below .10; .28 leaves headroom for noisy resizes.
_CHANGE_FLOOR = 0.28
# A .18 margin above median residuals rejects global brightness/contrast shifts.
_CHANGE_MARGIN = 0.18
# Three adjacent blocks reject noise unless a component contains a strong change.
_MIN_COMPONENT = 3
# Twice the threshold (.56 minimum) exceeds coarse JPEG noise (<.10) and fine
# resize/CMYK residuals (up to .52); fine fallback components must meet it too.
_STRONG_CHANGE_FACTOR = 2.0
# Forty percent admits localized edits but rejects most whole-picture changes.
_MAX_EDIT_FRACTION = 0.40
# A box spanning 65% of the frame can be a near-full crop, not a local edit.
_LOCAL_BOX_AREA = 0.65
# A crop must improve correlation by .02 over a deferred edit (98% crop: +.056).
_CROP_ALIGNMENT_GAIN = 0.02
# 160 pixels balances crop localization (about 1%) and inexpensive FFT searches.
_SEARCH_SIDE = 160
# Start at 20% of the largest fitting template to retain tight subject crops.
_CROP_MIN_SCALE = 0.20
# Four-percent coarse steps give twenty-one inexpensive scales through 100%.
_CROP_COARSE_STEP = 0.04
# Refine to 1% then .25% scale steps, finer than the 160-pixel search resolution.
_CROP_REFINE_STEPS = (0.01, 0.0025)
# Synthetic crops score .989..1; .87 leaves room for resampling and small edits.
_CROP_NCC = 0.87
# Templates below 40% need .94 NCC, plus the same hash and residual verification.
_SMALL_CROP_SCALE = 0.40
_SMALL_CROP_NCC = 0.94
# Twelve bits admits imperfect crop registration; NCC provides the main safeguard.
_CROP_DISTANCE = 12
# .35 allows more registration residual than the .24 aligned-image threshold.
_CROP_MEDIAN = 0.35
# Ignore full-frame matches within 2%; those belong to the aligned-image path.
_CROP_MAX_AREA = 0.98


@dataclass(frozen=True, slots=True)
class Fingerprint:
    sha256: str
    phash: str
    dhash: str
    whash: str
    width: int
    height: int
    format: str | None


@dataclass(frozen=True, slots=True)
class Relation:
    kind: RelationKind
    distance: int | None
    crop_box: tuple[float, float, float, float] | None
    edited_region: tuple[float, float, float, float] | None
    note: str


def _decode(data: bytes) -> tuple[Image.Image, tuple[int, int], str | None]:
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.seek(0)
            image_format = source.format
            size = source.size
            orientation = source.getexif().get(274, 1)
            if orientation in (5, 6, 7, 8):
                size = size[::-1]
            source.draft("L", (_WORK_SIDE, _WORK_SIDE))
            reduction = max(1, max(source.size) // _WORK_SIDE)
            working = source
            if source.mode.startswith("I;16") or source.mode in ("I", "F"):
                values = np.asarray(source, dtype=np.float32)
                if source.mode.startswith("I;16"):
                    values = values / 257
                else:
                    values = np.nan_to_num(values)
                    low, high = float(values.min()), float(values.max())
                    values = (values - low) * (255 / (high - low) if high > low else 0)
                working = Image.fromarray(np.clip(values, 0, 255).astype(np.uint8))
            elif working.mode in ("RGBA", "LA", "PA") or "transparency" in working.info:
                rgba = working.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                working = Image.alpha_composite(background, rgba)
            elif working.mode in ("CMYK", "YCbCr", "LAB"):
                working = working.convert("RGB")
            working = working.convert("L")
            working.info.update(source.info)
            if reduction > 1:
                working = working.reduce(reduction)
            working = ImageOps.exif_transpose(working)
            working.thumbnail((_WORK_SIDE, _WORK_SIDE), Image.Resampling.LANCZOS)
            return working, size, image_format
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
    ) as exc:
        raise ValueError("Cannot decode image bytes") from exc


def fingerprint(data: bytes) -> Fingerprint:
    """Hash raw bytes and downscaled, white-composited, EXIF-oriented first frame.

    Dimensions describe the oriented original, not the working thumbnail.
    Undecodable bytes raise ValueError; perceptual hashes are 64-bit imagehash hex.
    """
    image, size, image_format = _decode(data)
    return Fingerprint(
        hashlib.sha256(data).hexdigest(),
        str(imagehash.phash(image)),
        str(imagehash.dhash(image)),
        str(imagehash.whash(image)),
        *size,
        image_format,
    )


def _gray(image: Image.Image, size: tuple[int, int]) -> np.ndarray:
    return np.asarray(image.convert("L").resize(size, Image.Resampling.BILINEAR), dtype=np.float32)


def _blocks(
    base: Image.Image, other: Image.Image, side: int = _ALIGN_SIDE, grid: int = _GRID_SIDE
) -> np.ndarray:
    first = _gray(base, (side, side))
    second = _gray(other, (side, side))
    first = (first - first.mean()) / max(float(first.std()), _MIN_STD)
    second = (second - second.mean()) / max(float(second.std()), _MIN_STD)
    cell = side // grid
    return np.abs(first - second).reshape(grid, cell, grid, cell).mean(axis=(1, 3))


def _changed_box(
    blocks: np.ndarray, *, strong_only: bool = False
) -> tuple[tuple[float, float, float, float] | None, float]:
    threshold = max(_CHANGE_FLOOR, float(np.median(blocks)) + _CHANGE_MARGIN)
    labels, _ = ndimage.label(blocks > threshold, structure=np.ones((3, 3)))
    counts = np.bincount(labels.ravel())
    keep = (counts >= _MIN_COMPONENT) & (not strong_only)
    keep[np.unique(labels[blocks >= threshold * _STRONG_CHANGE_FACTOR])] = True
    keep[0] = False
    changed = keep[labels]
    rows, columns = np.nonzero(changed)
    if not len(rows):
        return None, 0.0
    box = (
        float(columns.min() / blocks.shape[1]),
        float(rows.min() / blocks.shape[0]),
        float((columns.max() + 1) / blocks.shape[1]),
        float((rows.max() + 1) / blocks.shape[0]),
    )
    return box, float(changed.mean())


def _aligned(base: Image.Image, blocks: np.ndarray) -> bool:
    if float(np.median(blocks)) > _ALIGN_MEDIAN:
        return False
    gray = _gray(base, (_ALIGN_SIDE, _ALIGN_SIDE))
    cell = _ALIGN_SIDE // _GRID_SIDE
    texture = gray.reshape(_GRID_SIDE, cell, _GRID_SIDE, cell).std(axis=(1, 3))
    informative = texture >= max(_TEXTURE_MIN_STD, float(np.quantile(texture, _TEXTURE_QUANTILE)))
    return not informative.any() or float(np.median(blocks[informative])) <= _ALIGN_MEDIAN


def _edit_note(box: tuple[float, float, float, float]) -> str:
    left, top, right, bottom = box
    if top >= 0.65:
        location = f"bottom {round((1 - top) * 100)}%"
    elif bottom <= 0.35:
        location = f"top {round(bottom * 100)}%"
    elif right <= 0.55 and bottom <= 0.60:
        location = "upper-left area"
    elif left >= 0.45 and bottom <= 0.60:
        location = "upper-right area"
    else:
        location = "marked region"
    return f"Same picture with changes in the {location}."


def _window_sums(values: np.ndarray, height: int, width: int) -> np.ndarray:
    integral = np.pad(values, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return (
        integral[height:, width:]
        - integral[:-height, width:]
        - integral[height:, :-width]
        + integral[:-height, :-width]
    )


def _crop_match(
    larger: Image.Image, smaller: Image.Image
) -> tuple[float, tuple[float, float, float, float]] | None:
    ratio = _SEARCH_SIDE / max(larger.size)
    width, height = (max(1, round(dimension * ratio)) for dimension in larger.size)
    field = _gray(larger, (width, height)).astype(np.float64)
    fit = min(width / smaller.width, height / smaller.height)
    best_score, best_scale, best_box = -1.0, 1.0, None
    visited = set()

    def search(scales: np.ndarray) -> None:
        nonlocal best_score, best_scale, best_box
        for scale in scales:
            template_width = max(2, round(smaller.width * fit * float(scale)))
            template_height = max(2, round(smaller.height * fit * float(scale)))
            shape = (template_width, template_height)
            if shape in visited or template_width > width or template_height > height:
                continue
            visited.add(shape)
            if template_width * template_height / (width * height) >= _CROP_MAX_AREA:
                continue
            template = _gray(smaller, shape).astype(np.float64)
            template -= template.mean()
            energy = float(np.square(template).sum())
            if energy < _MIN_STD**2 * template.size:
                continue
            sums = _window_sums(field, template_height, template_width)
            squares = _window_sums(field * field, template_height, template_width)
            variance = np.maximum(squares - sums * sums / template.size, 0)
            correlation = signal.fftconvolve(field, template[::-1, ::-1], mode="valid")
            denominator = np.sqrt(variance * energy)
            scores = np.divide(
                correlation, denominator, out=np.zeros_like(correlation), where=denominator > 0
            )
            row, column = np.unravel_index(np.argmax(scores), scores.shape)
            score = float(scores[row, column])
            if score > best_score:
                best_score, best_scale = score, float(scale)
                best_box = (
                    column / width,
                    row / height,
                    (column + template_width) / width,
                    (row + template_height) / height,
                )

    search(np.arange(_CROP_MIN_SCALE, 1.001, _CROP_COARSE_STEP))
    radius = _CROP_COARSE_STEP
    for step in _CROP_REFINE_STEPS:
        search(
            np.arange(
                max(_CROP_MIN_SCALE, best_scale - radius),
                min(1, best_scale + radius) + step / 2,
                step,
            )
        )
        radius = step
    minimum_score = _SMALL_CROP_NCC if best_scale < _SMALL_CROP_SCALE else _CROP_NCC
    if best_box is None or best_score < minimum_score:
        return None
    return best_score, tuple(float(coordinate) for coordinate in best_box)


def relate(base: bytes, other: bytes) -> Relation:
    """Compare image bytes, raising ValueError if either input cannot be decoded.

    Identical valid bytes are same, even if tiny or blank; otherwise images with
    a side below 16 pixels or grayscale standard deviation below 2 are unknown.
    Edited boxes are fractions of BASE. Crop boxes locate the other picture in
    the base, in fractions of BASE, and are None when the other picture is larger.
    Crops can include a small localized edit. All boxes are left/top/right/bottom.
    """
    identical = hashlib.sha256(base).digest() == hashlib.sha256(other).digest()
    first, first_size, _ = _decode(base)
    if identical:
        return Relation("same", 0, None, None, "Identical image bytes.")
    second, second_size, _ = _decode(other)
    first_hash, second_hash = imagehash.phash(first), imagehash.phash(second)
    distance = int(first_hash - second_hash)
    if (
        min(*first_size, *second_size) < _MIN_SIDE
        or np.asarray(first.convert("L")).std() < _MIN_STD
        or np.asarray(second.convert("L")).std() < _MIN_STD
    ):
        return Relation(
            "unknown", distance, None, None, "Too little image detail to compare reliably."
        )
    aspect = (first_size[0] / first_size[1]) / (second_size[0] / second_size[1])
    deferred_edit = None
    aligned_correlation = None
    if abs(aspect - 1) <= _ASPECT_TOLERANCE:
        blocks = _blocks(first, second)
        region, fraction = _changed_box(blocks)
        aligned = _aligned(first, blocks)
        if aligned and region is None and distance <= _RESIZE_DISTANCE:
            shared_size = (min(first.width, second.width), min(first.height, second.height))
            region, fraction = _changed_box(
                _blocks(
                    first.resize(shared_size, Image.Resampling.LANCZOS),
                    second.resize(shared_size, Image.Resampling.LANCZOS),
                    _WORK_SIDE,
                    _FINE_GRID_SIDE,
                ),
                strong_only=True,
            )
        if aligned and region is None and distance <= _RESIZE_DISTANCE:
            if first_size == second_size:
                note = f"Same picture, re-encoded at {second_size[0]}x{second_size[1]}."
            else:
                note = (
                    f"Same picture, rescaled from {first_size[0]}x{first_size[1]} "
                    f"to {second_size[0]}x{second_size[1]}."
                )
            return Relation("resized", distance, None, None, note)
        if (
            aligned
            and region is not None
            and fraction <= _MAX_EDIT_FRACTION
            and distance <= _EDIT_DISTANCE
        ):
            edit = Relation("edited", distance, None, region, _edit_note(region))
            if (region[2] - region[0]) * (region[3] - region[1]) <= _LOCAL_BOX_AREA:
                return edit
            deferred_edit = edit
            aligned_correlation = float(
                np.corrcoef(
                    _gray(first, (_SEARCH_SIDE, _SEARCH_SIDE)).ravel(),
                    _gray(second, (_SEARCH_SIDE, _SEARCH_SIDE)).ravel(),
                )[0, 1]
            )
    matches = []
    for larger, smaller, reverse in ((first, second, False), (second, first, True)):
        match = _crop_match(larger, smaller)
        if match is None:
            continue
        score, box = match
        if aligned_correlation is not None and score < aligned_correlation + _CROP_ALIGNMENT_GAIN:
            continue
        region = larger.crop(
            tuple(round(value * larger.size[index % 2]) for index, value in enumerate(box))
        )
        crop_distance = int(imagehash.phash(region) - (first_hash if reverse else second_hash))
        if (
            crop_distance <= _CROP_DISTANCE
            and float(np.median(_blocks(region, smaller))) <= _CROP_MEDIAN
        ):
            matches.append((score, crop_distance, box, reverse))
    if matches:
        _, crop_distance, box, reverse = max(matches)
        note = (
            "The other picture shows the base picture with extra area around it."
            if reverse
            else (
                "The other picture is a crop of the base picture, keeping about "
                f"{round((box[2] - box[0]) * 100)}% of its width and "
                f"{round((box[3] - box[1]) * 100)}% of its height."
            )
        )
        return Relation("cropped", crop_distance, None if reverse else box, None, note)
    return deferred_edit or Relation("different", distance, None, None, "A different picture.")
