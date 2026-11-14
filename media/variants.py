"""CPU-only comparison of image encodings, localized edits, and rectangular crops.

Edited-region boxes use (left, top, right, bottom) fractions of the oriented BASE.
Crop boxes locate the other picture inside the base, in fractions of BASE, and
are None when the other picture is larger; the note identifies that direction.
Crops with a small additional edit can still be classified as cropped. Unknown means
a decoded image has a side below 16 pixels or insufficient grayscale variation.

Very small rendered glyph substitutions (for example, a single digit in text
about 24px tall or smaller on a 600px image) may be reported as resized, by design:
unchanged reposts must not be called edited. Rendered shape and contrast matter,
so font size alone does not guarantee detection.

The 300 ms budget targets 2048-pixel photo/meme pairs, not full-entropy noise
stored as quality-95, 4:4:4 progressive JPEGs: entropy decoding alone can exceed
that budget even with luminance-only draft decoding. Images above Pillow's
decompression-bomb limit (about 179 megapixels) are rejected with ValueError.

Strong global tone changes (brightness or contrast boosts that clip highlights,
gamma curves, filters on coloured text) are not modelled: such a repost of an
unchanged picture may be reported as edited, with scattered regions. Grayscale
and sepia conversions come out as resized.
"""

import hashlib
import io
from dataclasses import dataclass
from typing import Literal

import imagehash
import numpy as np
from PIL import Image, UnidentifiedImageError
from scipy import fft, ndimage

RelationKind = Literal["same", "resized", "cropped", "edited", "different", "unknown"]

# A 512-pixel working image keeps decoding/reduction and hashing inexpensive.
_WORK_SIDE = 512
# A 256-pixel alignment view keeps caption strokes visible at low cost.
_ALIGN_SIDE = 256
# 32 blocks per axis averages 8x8 pixels to suppress individual JPEG artifacts.
_GRID_SIDE = 32
# A 512-pixel, 64-block map resolves small text alongside coarse edits.
_FINE_GRID_SIDE = 64
# EXIF/TIFF orientation tag -> the transpose that shows the picture upright (as
# ImageOps.exif_transpose). Applied explicitly because the converted working copy
# no longer carries TIFF tags, so exif_transpose on it would do nothing.
_ORIENTATION_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
_REGISTRATION_LIMIT = 0.04
_TRIM_LIMIT = 0.015
_RESIDUAL_SIGMA = 0.85
_RESIDUAL_STD_FLOOR = 45.0
_SHARED_EDGE_WEIGHT = 0.04
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
# Shared-edge suppression keeps coarse repost noise below .28; small glyphs
# use the fine map rather than weakening coarse components.
_CHANGE_FLOOR = 0.28
_FINE_CHANGE_FLOOR = 0.12
# A .18 margin above median residuals rejects global brightness/contrast shifts.
_CHANGE_MARGIN = 0.18
_FINE_CHANGE_MARGIN = 0.10
# Three adjacent blocks reject noise unless a component contains a strong change.
_MIN_COMPONENT = 3
# Fine repost noise stays below the .24 anchor; only components with a peak
# twice the fine threshold count. Smaller glyphs can overlap that noise and
# are deliberately missed rather than calling an unchanged repost edited.
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
            source.draft("L", (_WORK_SIDE, _WORK_SIDE))
            source.load()
            orientation = source.getexif().get(274, 1)
            if orientation in (5, 6, 7, 8):
                size = size[::-1]
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
            elif working.mode in ("P", "1"):
                working = working.convert("L")
            if reduction > 1:
                working = working.reduce(reduction)
            working = working.convert("L")
            if orientation in _ORIENTATION_TRANSPOSE:
                working = working.transpose(_ORIENTATION_TRANSPOSE[orientation])
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
    return np.asarray(image.resize(size, Image.Resampling.BILINEAR), dtype=np.float32)


def _warp(values: np.ndarray, scale: np.ndarray, offset: np.ndarray) -> np.ndarray:
    return ndimage.affine_transform(
        values, scale, offset=offset, order=1, mode="nearest", prefilter=False
    )


def _register(
    first: np.ndarray, second: np.ndarray, aspect: float
) -> tuple[np.ndarray, np.ndarray]:
    """Phase initialization followed by robust subpixel translation/scale fitting.

    Offsets sample OTHER in BASE's frame. Robust least squares prevents a small
    caption from pulling the geometry; all reductions and FFTs are single-threaded.
    Undo the known aspect normalization so a similarity in original coordinates
    remains a similarity when fitted in the square working frame. The 4% range
    also refines near-full crops below the resolution of the crop search grid.
    """
    side = first.shape[0]
    window = np.outer(np.hanning(side), np.hanning(side))
    first = ndimage.gaussian_filter(first, 0.8)
    second = ndimage.gaussian_filter(second, 0.8)
    first = (first - first.mean()) / max(float(first.std()), _MIN_STD)
    second = (second - second.mean()) / max(float(second.std()), _MIN_STD)
    cross = np.fft.rfft2(first * window) * np.conj(np.fft.rfft2(second * window))
    correlation = np.fft.irfft2(cross / np.maximum(np.abs(cross), 1e-8), s=first.shape)
    peak = np.array(np.unravel_index(np.argmax(correlation), correlation.shape))
    shift = peak.astype(float)
    for axis in range(2):
        before, after = peak.copy(), peak.copy()
        before[axis] = (before[axis] - 1) % side
        after[axis] = (after[axis] + 1) % side
        low, middle, high = (
            correlation[tuple(before)],
            correlation[tuple(peak)],
            correlation[tuple(after)],
        )
        denominator = low - 2 * middle + high
        if abs(denominator) > 1e-8:
            shift[axis] += float(np.clip(0.5 * (low - high) / denominator, -0.5, 0.5))
    shift[shift > side / 2] -= side
    offset = -np.clip(shift, -side * 0.04, side * 0.04)
    scale = 1.0
    rows, columns = np.indices(first.shape, dtype=np.float32)
    center = (side - 1) / 2
    axes = np.array((1 / np.sqrt(aspect), np.sqrt(aspect)))
    offset += center * (1 - axes)
    gradient_row, gradient_column = np.gradient(first)
    jacobian = np.stack(
        (
            gradient_row,
            gradient_column,
            (gradient_row * (rows - center) + gradient_column * (columns - center)) / side,
        )
    )
    interior = (rows >= 4) & (rows < side - 4) & (columns >= 4) & (columns < side - 4)
    for _ in range(16):
        dimensions = scale * axes
        residual = _warp(second, dimensions, offset) - first
        valid = interior & (rows * dimensions[0] + offset[0] >= 2)
        valid &= rows * dimensions[0] + offset[0] < side - 2
        valid &= columns * dimensions[1] + offset[1] >= 2
        valid &= columns * dimensions[1] + offset[1] < side - 2
        weights = valid * np.minimum(1, 0.08 / np.maximum(np.abs(residual), 1e-6))
        normal = np.einsum("ihw,jhw,hw->ij", jacobian, jacobian, weights)
        target = np.einsum("ihw,hw,hw->i", jacobian, residual, weights)
        try:
            update = np.linalg.solve(normal + np.eye(3) * 1e-6, target)
        except np.linalg.LinAlgError:
            break
        scale_update = float(np.clip(update[2] / side, -0.005, 0.005))
        scale -= scale_update
        offset -= np.clip(update[:2], -1, 1) - center * scale_update * axes
        if abs(scale - 1) > _REGISTRATION_LIMIT or np.max(np.abs(offset)) > side * 0.04:
            break
        if np.max(np.abs(update)) < 0.002:
            break
    return scale * axes, offset


def _residual(
    first: np.ndarray, second: np.ndarray, scale: np.ndarray, offset: np.ndarray
) -> np.ndarray:
    """Discount shared blurred edges without hiding new structure on either side.

    A gray-level normalization floor prevents sparse captions on flat templates
    from amplifying resampling noise. Gradients retain absolute luminance units
    so the edge weighting does not itself depend on the background's contrast.
    """
    side = first.shape[0]
    rows, columns = np.indices(first.shape, dtype=np.float32)
    valid = (rows * scale[0] + offset[0] >= 0) & (rows * scale[0] + offset[0] <= side - 1)
    valid &= (columns * scale[1] + offset[1] >= 0) & (columns * scale[1] + offset[1] <= side - 1)
    valid = ndimage.binary_erosion(valid, iterations=3)
    first = ndimage.gaussian_filter(first, _RESIDUAL_SIGMA)
    second = ndimage.gaussian_filter(_warp(second, scale, offset), _RESIDUAL_SIGMA)
    shared_edges = np.minimum(np.hypot(*np.gradient(first)), np.hypot(*np.gradient(second)))
    first = (first - first[valid].mean()) / max(float(first[valid].std()), _RESIDUAL_STD_FLOOR)
    second = (second - second[valid].mean()) / max(float(second[valid].std()), _RESIDUAL_STD_FLOOR)
    return np.where(valid, np.abs(first - second) / (1 + _SHARED_EDGE_WEIGHT * shared_edges), 0)


def _pool(difference: np.ndarray, grid: int) -> np.ndarray:
    cell = difference.shape[0] // grid
    return difference.reshape(grid, cell, grid, cell).mean(axis=(1, 3))


def _blocks(base: Image.Image, other: Image.Image) -> np.ndarray:
    first, second = (_gray(image, (_ALIGN_SIDE, _ALIGN_SIDE)) for image in (base, other))
    return _pool(_residual(first, second, np.ones(2), np.zeros(2)), _GRID_SIDE)


def _changed_mask(blocks: np.ndarray, *, strong_only: bool = False) -> np.ndarray:
    floor = _FINE_CHANGE_FLOOR if strong_only else _CHANGE_FLOOR
    margin = _FINE_CHANGE_MARGIN if strong_only else _CHANGE_MARGIN
    threshold = max(floor, float(np.median(blocks)) + margin)
    labels, _ = ndimage.label(blocks > threshold, structure=np.ones((3, 3)))
    counts = np.bincount(labels.ravel())
    keep = (counts >= _MIN_COMPONENT) & (not strong_only)
    keep[np.unique(labels[blocks >= threshold * _STRONG_CHANGE_FACTOR])] = True
    keep[0] = False
    return keep[labels]


def _mask_box(changed: np.ndarray) -> tuple[float, float, float, float] | None:
    rows, columns = np.nonzero(changed)
    if not len(rows):
        return None
    return (
        float(columns.min() / changed.shape[1]),
        float(rows.min() / changed.shape[0]),
        float((columns.max() + 1) / changed.shape[1]),
        float((rows.max() + 1) / changed.shape[0]),
    )


def _aligned(gray: np.ndarray, blocks: np.ndarray) -> bool:
    if float(np.median(blocks)) > _ALIGN_MEDIAN:
        return False
    cell = _ALIGN_SIDE // _GRID_SIDE
    texture = gray.reshape(_GRID_SIDE, cell, _GRID_SIDE, cell).std(axis=(1, 3))
    informative = texture >= max(_TEXTURE_MIN_STD, float(np.quantile(texture, _TEXTURE_QUANTILE)))
    return not informative.any() or float(np.median(blocks[informative])) <= _ALIGN_MEDIAN


def _edit_location(box: tuple[float, float, float, float]) -> str:
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
    return location


def _band_share(location: str) -> int:
    return int(location.split()[1].rstrip("%")) if location.startswith(("top ", "bottom ")) else 0


def _edit_note(changed: np.ndarray) -> str:
    """Group nearby glyphs into areas without enlarging the reported edit box."""
    structure = np.ones((3, 3))
    labels, count = ndimage.label(
        ndimage.binary_dilation(changed, structure=structure, iterations=2),
        structure=structure,
    )
    locations = []
    for label in range(1, count + 1):
        box = _mask_box(changed & (labels == label))
        if box is not None:
            locations.append(_edit_location(box))
    # One phrase per band: "bottom 16%" and "bottom 14%" read as one place.
    merged: dict[str, str] = {}
    for location in locations:
        band = location.split()[0] if location.startswith(("top ", "bottom ")) else location
        if band not in merged or _band_share(location) > _band_share(merged[band]):
            merged[band] = location
    locations = list(merged.values())
    if len(locations) == 1:
        return f"Same picture with changes in the {locations[0]}."
    return f"Same picture with changes in {len(locations)} areas ({' and '.join(locations)})."


def _window_sums(integral: np.ndarray, height: int, width: int) -> np.ndarray:
    return (
        integral[height:, width:]
        - integral[:-height, width:]
        - integral[height:, :-width]
        + integral[:-height, :-width]
    )


def _crop_match(
    larger: Image.Image, smaller: Image.Image
) -> tuple[float, tuple[float, float, float, float]] | None:
    """Reuse field statistics and its FFT across scales.

    A field-sized circular convolution agrees with the linear convolution in
    the valid interior; wrapped samples lie only in the discarded top/left rim.
    """
    ratio = _SEARCH_SIDE / max(larger.size)
    width, height = (max(1, round(dimension * ratio)) for dimension in larger.size)
    field = _gray(larger, (width, height)).astype(np.float64)
    integral = np.pad(field, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    square_integral = np.pad(field * field, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    fft_shape = tuple(fft.next_fast_len(dimension, real=True) for dimension in field.shape)
    field_fft = fft.rfft2(field, s=fft_shape, workers=1)
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
            sums = _window_sums(integral, template_height, template_width)
            squares = _window_sums(square_integral, template_height, template_width)
            variance = np.maximum(squares - sums * sums / template.size, 0)
            template_fft = fft.rfft2(template[::-1, ::-1], s=fft_shape, workers=1)
            correlation = fft.irfft2(field_fft * template_fft, s=fft_shape, workers=1)[
                template_height - 1 : height, template_width - 1 : width
            ]
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
    identical = base == other
    first, first_size, _ = _decode(base)
    if identical:
        return Relation("same", 0, None, None, "Identical image bytes.")
    second, second_size, _ = _decode(other)
    first_hash, second_hash = imagehash.phash(first), imagehash.phash(second)
    distance = int(first_hash - second_hash)
    if (
        min(*first_size, *second_size) < _MIN_SIDE
        or np.asarray(first).std() < _MIN_STD
        or np.asarray(second).std() < _MIN_STD
    ):
        return Relation(
            "unknown", distance, None, None, "Too little image detail to compare reliably."
        )
    aspect = (first_size[0] / first_size[1]) / (second_size[0] / second_size[1])
    deferred_edit = None
    aligned_correlation = None
    if abs(aspect - 1) <= _ASPECT_TOLERANCE and distance <= _EDIT_DISTANCE:
        shared_size = (min(first.width, second.width), min(first.height, second.height))
        first_fine, second_fine = (
            _gray(image.resize(shared_size, Image.Resampling.LANCZOS), (_WORK_SIDE, _WORK_SIDE))
            for image in (first, second)
        )
        first_coarse, second_coarse = (
            values.reshape(_ALIGN_SIDE, 2, _ALIGN_SIDE, 2).mean(axis=(1, 3))
            for values in (first_fine, second_fine)
        )
        dimensions, offset = _register(first_coarse, second_coarse, aspect)
        scale = float(np.sqrt(dimensions.prod()))
        difference = _residual(
            first_fine, second_fine, dimensions, offset * 2 + (dimensions - 1) / 2
        )
        blocks = _pool(difference, _GRID_SIDE)
        aligned = abs(scale - 1) <= _REGISTRATION_LIMIT and _aligned(first_coarse, blocks)
        region, fraction = None, 0.0
        if aligned:
            factor = _FINE_GRID_SIDE // _GRID_SIDE
            changed = _changed_mask(blocks).repeat(factor, axis=0).repeat(factor, axis=1)
            changed |= _changed_mask(
                _pool(difference, _FINE_GRID_SIDE),
                strong_only=True,
            )
            region, fraction = _mask_box(changed), float(changed.mean())
        if aligned and region is None and distance <= _RESIZE_DISTANCE:
            if abs(scale - 1) > _TRIM_LIMIT:
                origin = (offset + (1 - dimensions) / 2) / _ALIGN_SIDE
                lower = np.clip(-origin / dimensions, 0, 1)
                upper = np.clip((1 - origin) / dimensions, 0, 1)
                box = (float(lower[1]), float(lower[0]), float(upper[1]), float(upper[0]))
                note = (
                    "The other picture shows the base picture with extra area around it."
                    if scale < 1
                    else (
                        "The other picture is a crop of the base picture, keeping about "
                        f"{round((box[2] - box[0]) * 100)}% of its width and "
                        f"{round((box[3] - box[1]) * 100)}% of its height."
                    )
                )
                return Relation("cropped", distance, None if scale < 1 else box, None, note)
            if first_size == second_size:
                note = f"Same picture, re-encoded at {second_size[0]}x{second_size[1]}."
            else:
                note = (
                    f"Same picture, rescaled from {first_size[0]}x{first_size[1]} "
                    f"to {second_size[0]}x{second_size[1]}."
                )
            shift = (
                (offset + (dimensions - 1) * (_ALIGN_SIDE - 1) / 2)
                * np.array(first_size[::-1])
                / _ALIGN_SIDE
            )
            if abs(scale - 1) > 0.003:
                geometry = "a thin border trimmed" if scale > 1 else "a thin border added"
                note = f"{note[:-1]}, with {geometry}."
            elif np.max(np.abs(shift)) > 0.5:
                pixels = max(1, round(float(np.linalg.norm(shift))))
                note = f"{note[:-1]}, shifted by {pixels} pixel{'s' if pixels != 1 else ''}."
            return Relation("resized", distance, None, None, note)
        if (
            aligned
            and abs(scale - 1) <= _TRIM_LIMIT
            and region is not None
            and fraction <= _MAX_EDIT_FRACTION
            and distance <= _EDIT_DISTANCE
        ):
            edit = Relation("edited", distance, None, region, _edit_note(changed))
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
