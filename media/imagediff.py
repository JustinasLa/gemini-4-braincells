"""One bounded Super VL comparison, with paired or labelled composite image inputs."""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import math
import re
import struct
import time
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
# Quoted image text ('...' or "...", not an apostrophe) that the reply's own OCR confirms is
# exempt from the wording guard, so a caption swap from 'REAL NEWS' to 'FAKE NEWS' is reported.
_WORD = re.compile(r"[^\W_]+")
# ...and only when the whole sentence is one of the two forms the prompt asks for to report
# image text, so a quote in our own narration of another edit is never exempt:
#   "The [top] caption changed from 'X' to 'Y'."
#   "The/A [top] text/watermark/... 'X' [at the top] was added/removed[ at the bottom]."
# A quoted span cannot run past its closing quote: inside '...' (or a curly pair) a quote mark
# is allowed only as an apostrophe between two letters (DON'T), so one span never swallows the
# words between two quotes. Captured, so only the form's own quote spans are exempt.
_Q = (
    r"((?<!\w)'(?:[^']|(?<=\w)'(?=\w))*'(?!\w)|\"[^\"]*\""
    r"|\u2018(?:[^\u2018\u2019]|(?<=\w)\u2019(?=\w))*\u2019(?!\w)|\u201c[^\u201d]*\u201d)"
)
_ADJ = r"(?:top|bottom|upper|lower|main|first|second|new|old|original|small|large|bold)"
_NOUN = (
    r"(?:captions?|text|words?|lines?|watermarks?|handles?|labels?|titles?|headlines?"
    r"|slogans?|signs?|banners?|logos?|urls?|hashtags?)"
)
_QUOTE = re.compile(_Q)
_TEXT_FORMS = (
    re.compile(
        rf"(?:the\s+)?(?:{_ADJ}\s+)*{_NOUN}(?:\s+text)?\s+(?:(?:was|were|has\s+been)\s+)?"
        rf"(?:changed|edited|replaced|swapped|updated|switched|altered|rewritten|modified)"
        rf"\s+from\s+{_Q}\s+to\s+{_Q}\.?",
        re.I,
    ),
    re.compile(
        rf"(?:the|an?)\s+(?:{_ADJ}\s+)*{_NOUN}\s+{_Q}(?:\s+[^'\"\u2018\u201c.]*?)?\s+"
        rf"(?:was|were)\s+(?:added|removed)(?:\s+[^'\"\u2018\u201c.]*)?\.?",
        re.I,
    ),
)
# Formats that read only the header on open, so the size check runs before any decoding
# (ICO, for one, decodes its largest image inside Image.open). Memes and screenshots use these.
_FORMATS = ("JPEG", "PNG", "GIF", "WEBP")  # JPEG also opens MPO (phone photos)
# A "change" that says nothing changed (seen live: "The bottom caption 'X' is unchanged.").
_NO_CHANGE = re.compile(
    r"\b(?:unchanged|(?:did|does|has|have)\s+not\s+change[d]?|(?:didn't|hasn't|haven't)\s+change"
    r"|(?:remains?|remained|stays?|stayed|is|are)\s+the\s+same|no\s+(?:visible\s+)?changes?)\b",
    re.I,
)
_MAX_SIDE = 1024  # each input is downscaled to this before it is sent
_MAX_COMPOSITE = 2048  # composite width cap
# Larger inputs are refused from the header, before decoding (decompression bombs). This is far
# below Pillow's own warning threshold (~89 MP), so no process-wide warning filter is needed
# (warnings.catch_warnings is not thread-safe, and the lookup decodes in threads).
_MAX_PIXELS = 24_000_000
_RETRY_MIN_S = 3.0  # retry a 5xx only when at least this much of timeout_s is left
_MIN_REQUEST_S = 0.5  # a request is only sent with at least this much budget left
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
- Ignore the "A: earlier" / "B: later" labels.
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


def _declared_size(data: bytes) -> tuple[int, int] | None:
    """The canvas a GIF or WebP header declares; Pillow allocates it inside Image.open, before
    any size check could run. None for other formats (they read only the header on open).
    Raises ValueError for a GIF/WebP header it cannot read."""
    try:
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return _gif_size(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _webp_size(data)
    except (IndexError, struct.error) as exc:
        raise ValueError("unreadable GIF/WebP header") from exc
    return None


def _gif_size(data: bytes) -> tuple[int, int]:
    """Logical screen, grown to the first frame's extent, found the way Pillow 13's
    GifImagePlugin._seek(0) walks the stream (a stricter walk would disagree with it on
    odd streams and under-report): after an extension's first sub-block it skips sub-blocks
    until an empty one, even past a terminator (an empty first block, a NETSCAPE2.0 block
    with no loop block); comments stop at their terminator; stray bytes are skipped.
    tests/test_imagediff.py checks this against Pillow on mutated streams."""
    width, height, flags = struct.unpack_from("<HHB", data, 6)
    pos = 13 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0)

    def sub_block(at: int) -> tuple[bytes | None, int]:  # Pillow's GifImageFile.data()
        size = data[at]
        return (data[at + 1 : at + 1 + size] if size else None), at + 1 + size

    while True:
        byte = data[pos]  # IndexError at the end: no image (Pillow raises too)
        pos += 1
        if byte == 0x3B:
            raise ValueError("GIF: no image")
        if byte == 0x21:
            label = data[pos]
            block, pos = sub_block(pos + 1)
            if label == 0xFE:  # comment: blocks up to the terminator
                while block:
                    block, pos = sub_block(pos)
                continue
            if label == 0xFF and block is not None and block.startswith(b"NETSCAPE2.0"):
                block, pos = sub_block(pos)  # the loop block (or the terminator)
            while True:
                block, pos = sub_block(pos)
                if not block:
                    break
        elif byte == 0x2C:
            left, top, w, h = struct.unpack_from("<HHHH", data, pos)
            return max(width, left + w), max(height, top + h)


def _webp_size(data: bytes) -> tuple[int, int]:
    chunk = data[12:16]
    if chunk == b"VP8X":  # extended: the canvas (animation allocates two of these)
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":  # lossy key frame
        width, height = struct.unpack_from("<HH", data, 26)
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and data[20] == 0x2F:  # lossless
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    raise ValueError("unreadable WebP header")


def _image(data: bytes) -> Image.Image:
    declared = _declared_size(data)
    if declared and declared[0] * declared[1] > _MAX_PIXELS:
        raise ValueError(f"image too large: {declared[0]}x{declared[1]}")
    with Image.open(io.BytesIO(data), formats=_FORMATS) as source:
        if source.width * source.height > _MAX_PIXELS:
            raise ValueError(f"image too large: {source.width}x{source.height}")
        source.seek(0)
        keyed = "transparency" in source.info  # tRNS: a colour key, not an alpha band
        source.draft("RGB", (_MAX_SIDE, _MAX_SIDE))  # JPEG: decode at a reduced scale
        image = ImageOps.exif_transpose(source)
    # Resizing blends a colour key into its neighbours, and palette/1-bit images resize badly:
    # flatten those to RGBA first.
    if keyed or image.mode not in ("RGB", "RGBA", "L"):
        image = image.convert("RGBA")
    image.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.Resampling.LANCZOS)
    image = image.convert("RGBA")
    background = Image.new("RGB", image.size, "white")
    background.paste(image, mask=image.getchannel("A"))
    return background


def _mark(
    image: Image.Image,
    region: tuple[float, float, float, float] | None,
    panel: tuple[int, int, int, int],
) -> bool:
    """Draw the edited_region box inside ``panel`` (x, y, w, h: where B is in ``image``).

    Drawn on the final image, so composite scaling can't thin it. False: nothing drawn.
    """
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
    x, y, w, h = panel
    box = (
        x + left * (w - 1),
        y + top * (h - 1),
        x + right * (w - 1),
        y + bottom * (h - 1),
    )
    ImageDraw.Draw(image).rectangle(box, outline="red", width=max(3, h // 200))
    return True


def _composite(
    original: Image.Image, variant: Image.Image
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Side-by-side A | B with label bars; also returns B's panel as (x, y, w, h)."""
    gap, bar = 16, 40
    height = min(original.height, variant.height)
    ratio = original.width / original.height + variant.width / variant.height
    scaled_height = min(height, (_MAX_COMPOSITE - gap) / ratio)
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
    panel = (widths[0] + gap, bar, panels[1].width, height)
    if composite.width > _MAX_COMPOSITE:  # a very narrow panel was padded for its label
        scale = _MAX_COMPOSITE / composite.width
        size = (_MAX_COMPOSITE, max(1, round(composite.height * scale)))
        composite = composite.resize(size, Image.Resampling.LANCZOS)
        x, y, w, h = (round(value * scale) for value in panel)
        panel = (x, y, max(1, w), max(1, h))
    return composite, panel


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
    ocr = _OcrText(before + after)
    for index, sentence in enumerate(sentences):
        unquoted = _exempt_image_text(sentence, ocr)
        if (
            not sentence
            or _FORBIDDEN.search(unquoted)
            or _NO_CHANGE.search(_QUOTE.sub("''", sentence))  # a caption may say "no change"
            or (marked and _MARKER.search(unquoted))
        ):
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


class _OcrText:
    """The reply's own transcription of A and B, as word sequences."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = {tuple(_words(line)) for line in lines}
        self.joined = " " + " ".join(" ".join(_words(line)) for line in lines) + " "

    def confirms(self, words: list[str]) -> bool:
        """A whole transcribed line, or a run of 2+ words of the transcription."""
        if not words:
            return False
        if tuple(words) in self.lines:
            return True
        return len(words) >= 2 and f" {' '.join(words)} " in self.joined


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def _exempt_image_text(sentence: str, ocr: _OcrText) -> str:
    """The sentence with the quote spans of a text-report form blanked where the
    transcription confirms them; any other sentence unchanged (all of it is guarded)."""
    for form in _TEXT_FORMS:
        match = form.fullmatch(sentence)
        if match is None:
            continue
        out, last = [], 0
        for group in range(1, (match.lastindex or 0) + 1):
            start, end = match.span(group)
            out += [sentence[last:start], _image_text(sentence[start:end], ocr)]
            last = end
        return "".join(out) + sentence[last:]
    return sentence


def _image_text(quoted: str, ocr: _OcrText) -> str:
    """Blank a quoted span the transcription confirms; keep anything else for the guards
    (scare quotes, quoted narration, a quoted 'red box')."""
    return "''" if ocr.confirms(_words(quoted)) else quoted


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
    pending = False  # a request was sent and its attempt is not logged yet
    try:
        model = str(client.model)
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive and finite")
        deadline = entered + timeout_s
        if layout not in ("pair", "composite"):
            raise ValueError("unknown image layout")
        digests = hashlib.sha256(original).digest() + hashlib.sha256(variant).digest()
        media_hash = hashlib.sha256(digests).hexdigest()[:16]
        earlier, later = _image(original), _image(variant)
        if layout == "pair":
            images = [earlier, later]
            marked = _mark(later, edited_region, (0, 0, later.width, later.height))
        else:
            composite, panel = _composite(earlier, later)
            images = [composite]
            marked = _mark(composite, edited_region, panel)
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
        # Checked before reserving a limiter slot, so a lookup out of budget costs none.
        limiter = client._limiter
        if not _fits(limiter, deadline):
            raise TimeoutError("no time budget left for the request")
        limiter.wait()
        for attempt in (1, 2):
            if deadline - time.monotonic() <= 0:
                raise TimeoutError("time budget spent before the request")
            if deadline - time.monotonic() < _MIN_REQUEST_S:  # after the limiter's real wait
                raise TimeoutError("too little time budget left for the request")
            started, pending = time.monotonic(), True
            client.calls += 1
            try:
                response = client._client.chat.completions.create(
                    model=client.model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=max_tokens,
                    # httpx applies this per read, not to the whole reply: the deadline
                    # check below drops a reply that trickled in after it.
                    timeout=deadline - started,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "image_diff", "schema": _SCHEMA},
                    },
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                break
            except openai.InternalServerError as exc:
                # Verda answers some calls with a bare 500 (any request shape, about 5-10%
                # under load); a reply takes ~0.5 s, so one retry fits when budget is left.
                # Not on a dropped connection: the server may have run that inference already.
                if (
                    attempt == 2
                    or deadline - time.monotonic() < _RETRY_MIN_S
                    or not _fits(limiter, deadline)
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
                pending = False
                limiter.wait()  # the retry takes its own rate-limiter slot, like _complete
        if time.monotonic() > deadline:
            raise TimeoutError("reply arrived after the time budget")
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
        if pending and started is not None:
            _log_failure(model, media_hash, started, response, attempt, type(exc).__name__)
        # The worker stops on NotVisionModelError; the lookup must not, but say it loudly.
        level = logging.ERROR if isinstance(exc, NotVisionModelError) else logging.WARNING
        log.log(level, "image comparison unavailable: %s: %.300s", type(exc).__name__, exc)
        return None


def _fits(limiter: Any, deadline: float) -> bool:
    """True when a request can still start after the limiter's wait with budget to spare."""
    now = time.monotonic()
    starts = max(now, getattr(limiter, "_next", 0.0))
    return deadline - starts >= _MIN_REQUEST_S


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
