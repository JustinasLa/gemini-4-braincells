"""Whether an image shows the same template as a reference: the same web page, app screen or
meme template, with other names, numbers or text in it.

People answer a screenshot with their own screenshot of the same page ("Stand with Jensen"
signer search showing "Xi Jingping" instead of "Dario Amodei"). Those are not copies of the
picture, so perceptual hashes and reverse image search miss them; a vision model looking at the
reference beside the candidate sees the shared layout and says what differs.

One Nemotron Super VL call per candidate: one image with panel R (the reference) beside panel 1.
One at a time, not a grid of many: in a grid the model judged each panel against the others
(a near-identical panel made it reject real matches), measured live 2026-11-14. Two questions,
and only the second decides: same site or app (a shared header alone), and the same kind of
screen content (a cropped result list counts; the site's confirmation page does not). Guided
JSON, reasoning off. Never raises into the lookup: a failed call returns None.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import time
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from media.imagediff import _reserve
from media.nemotron import is_super_vl_model, log_call_attempt, parse_content

log = logging.getLogger(__name__)

_TILE = 640  # a panel's longest side (px)
_BAR = 34  # label bar height
_MIN_REQUEST_S = 1.0

SYSTEM_PROMPT = """You compare two pictures. Panel R is a reference screenshot or meme; panel
1 is a picture someone posted. Answer two questions about panel 1:
same_site: is it from the same website, app or meme template as R (same name, logo, look)?
same_screen: does it show the same kind of screen content as R: the same kind of search
result or list entry, form, post or meme panel, even when the names, numbers, dates, captions
or other text differ, or it is cropped to that part, resized, darker or photographed from a
screen? Another page of the same site (a confirmation message, a home page, an article, a
menu) is NOT the same screen, and neither is a different app, meme, photo or chart.
differs: when same_screen is true, say in under 15 words what content differs from R, quoting
the new text exactly as shown (e.g. shows "Xi Jingping" instead of "Dario Amodei"); if nothing
differs, "no visible difference". Report only what you can read; never guess names.
Return JSON only."""

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "same_site": {"type": "boolean"},
        "same_screen": {"type": "boolean"},
        "differs": {"type": "string"},
    },
    "required": ["same_site", "same_screen", "differs"],
}


@dataclass(frozen=True)
class TemplateVerdict:
    same_template: bool  # the same kind of screen content (same_screen)
    differs: str = ""
    same_site: bool = False


def _image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((_TILE, _TILE), Image.Resampling.LANCZOS)
    return img


def pair(reference: Image.Image, candidate: Image.Image) -> Image.Image:
    """R | 1, side by side, each under a label bar."""
    panels = [("R", reference), ("1", candidate)]
    gap = 16
    out = Image.new("RGB", (2 * _TILE + gap, _TILE + _BAR), (255, 255, 255))
    draw = ImageDraw.Draw(out)
    font = ImageFont.load_default(size=26)
    for k, (label, img) in enumerate(panels):
        x = k * (_TILE + gap)
        draw.rectangle((x, 0, x + _TILE - 1, _BAR - 1), fill=(20, 20, 20))
        draw.text((x + 10, 3), f"Panel {label}", fill=(255, 255, 255), font=font)
        out.paste(img, (x + (_TILE - img.width) // 2, _BAR + (_TILE - img.height) // 2))
    return out


def _part(img: Image.Image) -> dict[str, Any]:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


def parse_verdict(payload: Any) -> TemplateVerdict | None:
    """The model's answer; None when it isn't one (not a 'no')."""
    if not isinstance(payload, dict):
        return None
    same, site = payload.get("same_screen"), payload.get("same_site")
    if not isinstance(same, bool):
        return None
    differs = payload.get("differs") if isinstance(payload.get("differs"), str) else ""
    differs = " ".join(differs.split())[:200]
    if not same or differs.lower().rstrip(".") in ("no visible difference", "none", "n/a"):
        differs = ""
    return TemplateVerdict(same, differs, site is True)


def same_template(
    client: Any,
    reference: bytes,
    candidate: bytes,
    *,
    timeout_s: float,
    max_tokens: int = 256,
) -> tuple[TemplateVerdict, int, str] | None:
    """One bounded call: (verdict, latency ms, model), or None when it failed."""
    started = time.monotonic()
    deadline = started + timeout_s
    model, media_hash = str(getattr(client, "model", "unknown")), ""
    try:
        media_hash = hashlib.sha256(
            hashlib.sha256(reference).digest() + hashlib.sha256(candidate).digest()
        ).hexdigest()[:16]
        image = pair(_image(reference), _image(candidate))
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Panel R is the reference; panel 1 is the picture to check. "
                        "Return the JSON.",
                    },
                    _part(image),
                ],
            },
        ]
        if not _reserve(client._limiter, deadline):
            raise TimeoutError("no time budget left for the request")
        if deadline - time.monotonic() < _MIN_REQUEST_S:
            raise TimeoutError("too little time budget left for the request")
        sent = time.monotonic()
        client.calls += 1
        response = client._client.chat.completions.create(
            model=client.model,
            messages=messages,
            temperature=0.0,
            max_tokens=max_tokens,
            timeout=deadline - sent,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "template_match", "schema": _SCHEMA},
            },
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        if time.monotonic() > deadline:
            raise TimeoutError("reply arrived after the time budget")
        if not is_super_vl_model(response.model):
            raise ValueError("template comparison response is not Super VL")
        choice = response.choices[0] if response.choices else None
        if choice is None or getattr(choice, "finish_reason", None) == "length":
            raise ValueError("no complete reply")
        verdict = parse_verdict(parse_content(choice.message.content))
        if verdict is None:
            raise ValueError("the reply has no same_screen answer")
        latency = log_call_attempt(
            model=model, media_hash=media_hash, started=sent, response=response, attempt=1,
            task="templatematch",
        )  # fmt: skip
        return verdict, latency, str(response.model)
    except Exception as exc:
        log.warning("template comparison unavailable: %s: %.300s", type(exc).__name__, exc)
        return None
