"""One bounded Super VL comparison, with paired or labelled composite image inputs."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import math
import re
import time
import warnings
from dataclasses import dataclass, replace
from typing import Any, Literal, cast, get_args

import openai
from PIL import Image, ImageDraw, ImageFont, ImageOps

from media.nemotron import (
    NemotronClient,
    NotVisionModelError,
    _strings,
    is_super_vl_model,
    log_call_attempt,
    parse_content,
    safe_description,
)

log = logging.getLogger(__name__)
PROMPT_VERSION = "imagediff-v1"
# composite beat pair on the live eval (19/20 vs 18/20 clean; pair invented edits on an
# identical pair), at the same latency: media/README.md, "Image diff".
DEFAULT_LAYOUT: Literal["pair", "composite"] = "composite"
ChangeKind = Literal[
    "caption_changed",
    "text_added",
    "text_removed",
    "element_added",
    "element_removed",
    "face_changed",
    "cropped",
    "recolored",
    "watermark",
    "other",
]
CHANGE_KINDS: tuple[ChangeKind, ...] = get_args(ChangeKind)
_ALIASES = {
    "caption": "caption_changed",
    "crop": "cropped",
    "color": "recolored",
    "colour": "recolored",
    "recolor": "recolored",
    "recolour": "recolored",
    "face": "face_changed",
    "face_swap": "face_changed",
    "added_text": "text_added",
    "removed_text": "text_removed",
    "logo": "watermark",
    "handle": "watermark",
}
_FORBIDDEN = re.compile(
    r"\b(deep ?fakes?|fake[sd]?|faked|real|authentic|genuine|bots?|shills?|trolls?|propaganda)\b",
    re.I,
)
# Our own edited_region marker, reported back as an edit (seen live in pair layout).
_MARKER = re.compile(
    r"\bred\s+(?:(?:thin|rectangular|square)\s+)*(?:box|outline|border|frame|rectangle)\b", re.I
)
_MAX_SIDE = 1024  # each input is downscaled to this before it is sent
_MAX_PIXELS = 24_000_000  # larger inputs are refused before decoding (decompression bombs)
_RETRY_MIN_S = 3.0  # retry a 5xx only when at least this much of timeout_s is left
SYSTEM_PROMPT = f"""Compare A (earlier/original) with B (later/variant). Prompt: {PROMPT_VERSION}.
Return only JSON: text_before, text_after, changes, kinds, content_flags, confidence.
- text_before/text_after: legible text in A/B verbatim, at most 20 entries each, at most
  300 characters per entry. Never invent unreadable text.
- changes: at most 5 short plain sentences, one visible edit per sentence, most important
  first, each saying what B shows compared with A. Direction matters: something in A but
  missing from B was removed; something in B but not in A was added. Never mention A, B,
  panels, "the left/right image" or "the earlier/later image". Report each edit once, not
  its side effects: a crop is one change (not one per thing cut off); a replaced face is one
  change (not extra notes on expression, lighting or hair). Most pairs have one or two edits.
  Examples: "The caption changed from 'X' to 'Y'.", "A red hat was added on the right.",
  "The image was cropped; the left third is gone.", "The colours were shifted to a warmer
  tone.", "A watermark '@handle' was added at the bottom."
- kinds: one per change, in the same order, chosen from {list(CHANGE_KINDS)}.
- Report only differences you can clearly see. Re-encoding, compression, resizing, sharpness
  and slight brightness differences are not changes. Identical or visually same images:
  changes [] and kinds []. If unsure whether a difference is there, leave it out.
- Describe the media, not people; never name or identify a person. Never guess intent,
  authorship or truth. Never use deepfake, fake, faked, real, authentic, genuine, bot, shill,
  troll or propaganda. For a swapped face say:
  "The face of the person in the centre was replaced with a different face."
- B may carry a thin red rectangle drawn by our tool to mark where pixels differ. It is not
  part of the image: never report it; look inside it for the change. Ignore the A/B labels.
- content_flags: subset of ["adult", "graphic"]. Sexual/nude material: ["adult"].
  Gore/violence: ["graphic"]. Do not describe bodies or acts in either case.
- confidence: a number from 0 to 1."""
_TEXT_SCHEMA = {
    "type": "array",
    "items": {"type": "string", "maxLength": 300},
    "maxItems": 20,
}
_SCHEMA = {
    "type": "object",
    "properties": {
        "text_before": _TEXT_SCHEMA,
        "text_after": _TEXT_SCHEMA,
        "changes": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "kinds": {
            "type": "array",
            "items": {"type": "string", "enum": list(CHANGE_KINDS)},
            "maxItems": 5,
        },
        "content_flags": {
            "type": "array",
            # no "uniqueItems": Verda's vLLM grammar rejects it (400 "Unimplemented keys")
            "items": {"type": "string", "enum": ["adult", "graphic"]},
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["text_before", "text_after", "changes", "kinds", "content_flags", "confidence"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class ImageDiff:
    changes: list[str]
    kinds: list[ChangeKind]
    text_before: list[str]
    text_after: list[str]
    confidence: float
    model: str
    latency_ms: int


def _image(data: bytes) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > _MAX_PIXELS:
                raise ValueError(f"image too large: {source.width}x{source.height}")
            source.seek(0)
            source.draft("RGB", (_MAX_SIDE, _MAX_SIDE))  # JPEG: decode at a reduced scale
            image = ImageOps.exif_transpose(source)
    if image.mode not in ("RGB", "RGBA", "L"):  # palette/1-bit resize badly; flatten first
        image = image.convert("RGBA")
    image.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.Resampling.LANCZOS)
    image = image.convert("RGBA")
    background = Image.new("RGB", image.size, "white")
    background.paste(image, mask=image.getchannel("A"))
    return background


def _mark(image: Image.Image, region: tuple[float, float, float, float] | None) -> bool:
    """Draw the edited_region box on ``image``; False (nothing drawn) for a bad region."""
    if region is None:
        return False
    try:
        if len(region) != 4 or any(isinstance(value, bool) for value in region):
            return False
        values = [float(value) for value in region]
        if not all(math.isfinite(value) for value in values):
            return False
        left, top, right, bottom = [min(1.0, max(0.0, value)) for value in values]
    except (TypeError, ValueError, OverflowError):
        return False
    if left >= right or top >= bottom:
        return False
    box = (
        left * (image.width - 1),
        top * (image.height - 1),
        right * (image.width - 1),
        bottom * (image.height - 1),
    )
    ImageDraw.Draw(image).rectangle(box, outline="red", width=max(2, image.height // 200))
    return True


def _composite(original: Image.Image, variant: Image.Image) -> Image.Image:
    gap, bar = 16, 40
    height = min(original.height, variant.height)
    ratio = original.width / original.height + variant.width / variant.height
    scaled_height = min(height, (2048 - gap) / ratio)
    height = max(1, int(scaled_height))
    panels = [
        image.resize(
            (max(1, int(image.width * scaled_height / image.height)), height),
            Image.Resampling.LANCZOS,
        )
        for image in (original, variant)
    ]
    widths = [max(128, image.width) for image in panels]
    composite = Image.new("RGB", (sum(widths) + gap, height + bar), "white")
    draw = ImageDraw.Draw(composite)
    font = ImageFont.load_default(size=20)
    for image, label, offset in zip(
        panels, ("A: earlier", "B: later"), (0, widths[0] + gap), strict=True
    ):
        composite.paste(image, (offset, bar))
        draw.text((offset + 8, 8), label, fill="black", font=font)
    return composite


def _part(image: Image.Image) -> dict[str, Any]:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}


def _normalise(payload: dict[str, Any], model: str, *, marked: bool = False) -> ImageDiff:
    """Keep kinds aligned 1:1: padding, drops and deduplication follow original change indices.

    ``marked``: we drew a red edited_region box on B, so a change about a red box is ours.
    """
    raw = payload.get("changes")
    if not isinstance(raw, str | list):
        raise ValueError("changes must be a string or list")
    raw = [raw] if isinstance(raw, str) else raw
    sentences = [" ".join(" ".join(_strings([value])).split()) for value in raw]
    before = [text[:300] for text in _strings(payload.get("text_before"), limit=20)]
    after = [text[:300] for text in _strings(payload.get("text_after"), limit=20)]
    flags = [flag.lower() for flag in _strings(payload.get("content_flags"))]
    _, flags = safe_description(" ".join(sentences + before + after), flags)
    kinds = payload.get("kinds", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    kinds = kinds if isinstance(kinds, list) else []
    changes: list[str] = []
    aligned: list[ChangeKind] = []
    for index, sentence in enumerate(sentences):
        if not sentence or _FORBIDDEN.search(sentence) or (marked and _MARKER.search(sentence)):
            continue
        sentence = sentence if sentence.endswith(".") else sentence + "."
        if sentence in changes or len(changes) == 5:
            continue
        kind = str(kinds[index]).strip().lower() if index < len(kinds) else "other"
        kind = _ALIASES.get(kind, kind)
        changes.append(sentence)
        aligned.append(cast(ChangeKind, kind if kind in CHANGE_KINDS else "other"))
    if "adult" in flags or "graphic" in flags:
        changes = ["Adult content." if "adult" in flags else "Graphic content."]
        aligned, before, after = ["other"], [], []
    try:
        value = payload.get("confidence", 0.5)
        confidence = float(value) if not isinstance(value, bool) else 0.5
        confidence = min(1.0, max(0.0, confidence)) if math.isfinite(confidence) else 0.5
    except (TypeError, ValueError, OverflowError):
        confidence = 0.5
    return ImageDiff(changes, aligned, before, after, round(confidence, 4), model, 0)


def describe_changes(
    client: NemotronClient,
    original: bytes,
    variant: bytes,
    *,
    timeout_s: float,
    edited_region: tuple[float, float, float, float] | None = None,
    layout: Literal["pair", "composite"] = DEFAULT_LAYOUT,
    max_tokens: int = 2048,
) -> ImageDiff | None:
    """One bounded call (``timeout_s`` covers everything); never raises into the lookup."""
    entered = time.monotonic()
    started, response, media_hash, attempt, model = None, None, "", 1, "unknown"
    try:
        model = str(client.model)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive and finite")
        deadline = entered + timeout_s
        if layout not in ("pair", "composite"):
            raise ValueError("unknown image layout")
        media_hash = hashlib.sha256(original + variant).hexdigest()[:16]
        earlier, later = _image(original), _image(variant)
        marked = _mark(later, edited_region)
        images = [earlier, later] if layout == "pair" else [_composite(earlier, later)]
        text = (
            "The first image is A (earlier/original); the second is B (later/variant)."
            if layout == "pair"
            else "The left panel is A (earlier/original); the right panel is B (later/variant)."
        )
        if marked:
            text += (
                " The red box on B only marks where a pixel comparison saw a difference; it is"
                " not part of the image, so never report it."
            )
        text += " Return the comparison JSON."
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [{"type": "text", "text": text}, *[_part(image) for image in images]],
            },
        ]
        # Same-package private access: _complete's retry loop cannot honour a lookup budget.
        limiter = client._limiter
        if getattr(limiter, "_next", 0.0) - time.monotonic() >= deadline - time.monotonic():
            raise TimeoutError("rate limiter wait exceeds the time budget")
        limiter.wait()
        for attempt in (1, 2):
            started = time.monotonic()
            if deadline - started <= 0:
                raise TimeoutError("time budget spent before the request")
            client.calls += 1
            try:
                response = client._client.chat.completions.create(
                    model=client.model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=max_tokens,
                    timeout=deadline - started,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "image_diff", "schema": _SCHEMA},
                    },
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                break
            except (openai.InternalServerError, openai.APIConnectionError) as exc:
                # Verda answers some calls with a bare 500 (any request shape, about 5-10%
                # under load); a reply takes ~0.5 s, so one retry fits when budget is left.
                if (
                    attempt == 2
                    or isinstance(exc, openai.APITimeoutError)
                    or deadline - time.monotonic() < _RETRY_MIN_S
                ):
                    raise
                log_call_attempt(
                    model=model,
                    media_hash=media_hash,
                    started=started,
                    attempt=attempt,
                    outcome="retry",
                    error_type=type(exc).__name__,
                    task="imagediff",
                )
        if not is_super_vl_model(response.model):
            raise NotVisionModelError("image comparison response is not Super VL")
        if not response.choices:
            raise ValueError("image comparison returned no choices")
        choice = response.choices[0]
        # A reply is ~100 tokens; one cut at max_tokens is a loop, and a repaired cut could
        # lose an unfinished content_flags (the adult/graphic guard), so it is not used.
        if getattr(choice, "finish_reason", None) == "length":
            raise ValueError("comparison cut off at max_tokens")
        payload = parse_content(choice.message.content)
        result = _normalise(payload, response.model, marked=marked)
        latency = log_call_attempt(
            model=model,
            media_hash=media_hash,
            started=started,
            response=response,
            attempt=attempt,
            task="imagediff",
        )
        return replace(result, latency_ms=latency)
    except Exception as exc:
        if started is not None:
            _log_failure(model, media_hash, started, response, attempt, type(exc).__name__)
        log.warning("image comparison unavailable: %s: %.300s", type(exc).__name__, exc)
        return None


def _log_failure(
    model: str, media_hash: str, started: float, response: Any, attempt: int, error: str
) -> None:
    """Best effort: a malformed response must not make the failure log raise."""
    for resp in (response, None) if response is not None else (None,):
        try:
            log_call_attempt(
                model=model,
                media_hash=media_hash,
                started=started,
                response=resp,
                attempt=attempt,
                outcome=error,
                error_type=error,
                task="imagediff",
            )
            return
        except Exception:
            continue
