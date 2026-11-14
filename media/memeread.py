"""One bounded Super VL read of a meme: its added text, its frame, the template's common name,
who the text makes fun of, and visible edits to the picture. lookup/family.py reads every copy
of a picture with it to sort copies into a family tree (caption, poster, label, face swap).

Reasoning off, a JSON schema, ~0.3-0.6 s per image on Verda (2026-11-14). Text on a small
thumbnail is the weak spot: the model fills in a poster's tiny subtitle it cannot read. The
reply marks each text item large or small, and small text from a small image is dropped.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import math
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any

import openai
from PIL import Image, ImageOps

from media.imagediff import _reserve
from media.nemotron import (
    NemotronClient,
    NotVisionModelError,
    is_super_vl_model,
    log_call_attempt,
    parse_content,
)

log = logging.getLogger(__name__)
PROMPT_VERSION = "memeread-v2"
WHERE = ("top", "bottom", "on_picture", "below_frame", "watermark")
FRAMES = ("none", "poster", "panel", "screenshot")
# below this long side, small text is not read reliably (a 120 px TinEye thumbnail)
SMALL_IMAGE_PX = 300
_UPSCALE_TO = 448
_MAX_SIDE = 1024
_MIN_REQUEST_S = 0.5

SYSTEM_PROMPT = """You read one meme image. Report only what is visible. Reply with JSON only.
- text: each piece of legible text that an editor ADDED on or around the photograph, verbatim,
  in reading order. Never include text that is part of the photographed scene itself (signs,
  numbers on vehicles or buildings, writing on clothes or products). Each item:
  {"text": "...", "where": "top"|"bottom"|"on_picture"|"below_frame"|"watermark",
  "size": "large"|"small"}. "on_picture" is a label placed over part of the scene; a site
  watermark, handle or URL is "watermark"; text under a poster's border is "below_frame".
  "small" is text under about a twentieth of the image height. [] if there is none. Never
  invent or complete text you cannot read with certainty.
- frame: "none"; "poster" (the picture inside a black or coloured border with a title under it,
  a demotivator); "panel" (one panel of a multi-panel image); "screenshot" (the picture inside
  an app or web page screenshot).
- template: the meme template's common name if you recognise this picture as a widely known
  meme (e.g. "Distracted Boyfriend"), else "".
- aimed_at: the person, group, company or thing the added text makes fun of, mocks or
  criticises, in at most 5 words, in the text's own words where possible (e.g. "a senator",
  "Google", "the USA"), not the whole joke; "" if it targets no one or there is no text.
- edits: visible edits to the picture itself besides added text, each at most 12 words (e.g.
  "a different face pasted over the girl's face"); [] if you see none."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "where": {"type": "string", "enum": list(WHERE)},
                    "size": {"type": "string", "enum": ["large", "small"]},
                },
                "required": ["text", "where", "size"],
            },
        },
        "frame": {"type": "string", "enum": list(FRAMES)},
        "template": {"type": "string"},
        "aimed_at": {"type": "string"},
        "edits": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["text", "frame", "template", "aimed_at", "edits"],
}


@dataclass(frozen=True, slots=True)
class MemeText:
    text: str
    where: str


@dataclass(frozen=True, slots=True)
class MemeRead:
    text: list[MemeText] = field(default_factory=list)
    frame: str = "none"
    template: str = ""
    aimed_at: str = ""
    edits: list[str] = field(default_factory=list)
    model: str = ""
    latency_ms: int | None = None

    @property
    def caption(self) -> str:
        """The added text without watermarks, one line per item."""
        return "\n".join(t.text for t in self.text if t.where != "watermark")


def _clean(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit] if isinstance(value, str) else ""


def _prepare(data: bytes) -> tuple[str, int]:
    """JPEG data URL (small thumbnails upscaled so the model can see them) and the long side."""
    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        side = max(image.size)
        scale = _UPSCALE_TO / side if side < _UPSCALE_TO else min(1.0, _MAX_SIDE / side)
        if scale != 1.0:
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
        out = io.BytesIO()
        image.save(out, "JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii"), side


def parse_read(payload: dict[str, Any], *, side: int, model: str = "") -> MemeRead:
    small_image = side < SMALL_IMAGE_PX
    items = []
    for item in payload.get("text") or []:
        if not isinstance(item, dict):
            continue
        text = _clean(item.get("text"), 200)
        where = item.get("where") if item.get("where") in WHERE else "on_picture"
        if not text or (small_image and item.get("size") == "small"):
            continue
        items.append(MemeText(text=text, where=where))
    frame = payload.get("frame") if payload.get("frame") in FRAMES else "none"
    edits = [_clean(e, 120) for e in payload.get("edits") or [] if _clean(e, 120)]
    return MemeRead(
        text=items[:12],
        frame=frame,
        template=_clean(payload.get("template"), 60),
        aimed_at=_clean(payload.get("aimed_at"), 60) if items else "",
        edits=edits[:4],
        model=model,
    )


def read_meme(client: NemotronClient, data: bytes, *, timeout_s: float) -> MemeRead | None:
    """One bounded call (``timeout_s`` covers everything); None when unavailable. Never raises."""
    entered = time.monotonic()
    started, attempt = None, 1
    media_hash = hashlib.sha256(data).hexdigest()[:16]
    try:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be positive and finite")
        deadline = entered + timeout_s
        url, side = _prepare(data)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read this meme. Return the JSON."},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            },
        ]
        # Same-package private access, as media.imagediff: the lookup's budget bounds the call.
        if not _reserve(client._limiter, deadline):
            raise TimeoutError("no time budget left for the request")
        for attempt in (1, 2):
            if deadline - time.monotonic() < _MIN_REQUEST_S:
                raise TimeoutError("too little time budget left for the request")
            started = time.monotonic()
            client.calls += 1
            try:
                response = client._client.chat.completions.create(
                    model=client.model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=700,
                    timeout=deadline - started,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "meme_read", "schema": _SCHEMA},
                    },
                    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                )
                break
            except openai.InternalServerError:
                # Verda answers some calls with a bare 500; one retry when budget is left
                if attempt == 2 or deadline - time.monotonic() < 2 * _MIN_REQUEST_S:
                    raise
                if not _reserve(client._limiter, deadline):
                    raise
        if time.monotonic() > deadline:
            raise TimeoutError("reply arrived after the time budget")
        if not is_super_vl_model(response.model):
            raise NotVisionModelError("meme read response is not Super VL")
        if not response.choices:
            raise ValueError("meme read returned no choices")
        choice = response.choices[0]
        if getattr(choice, "finish_reason", None) == "length":
            raise ValueError("meme read cut off at max_tokens")
        latency = log_call_attempt(
            model=str(client.model),
            media_hash=media_hash,
            started=started,
            response=response,
            attempt=attempt,
            task="memeread",
        )
        read = parse_read(parse_content(choice.message.content), side=side, model=response.model)
        return replace(read, latency_ms=latency)
    except Exception as exc:
        level = logging.ERROR if isinstance(exc, NotVisionModelError) else logging.WARNING
        log.log(level, "meme read unavailable: %s: %.300s", type(exc).__name__, exc)
        return None
