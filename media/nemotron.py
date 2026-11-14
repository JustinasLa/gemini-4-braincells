"""Nemotron 3.5 Super VL client for evidence-producing media analysis.

Every call is logged (requested and returned model id, latency, tokens, finish reason). A response
whose model id is not Nemotron Super VL raises `NotVisionModelError`: the shared build endpoint has
answered image calls under a text-model label, and we must not publish findings as "Super VL"
unless the server says they are.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import openai
from openai import OpenAI

if TYPE_CHECKING:
    from media.screenshot import ScreenshotPost

log = logging.getLogger(__name__)

DEFAULT_MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"
DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Bumped whenever the prompt or output schema changes (media/README.md has the iteration log).
# media-v4: the media-v3 prompt, plus the screenshot-v1 read of screenshots (detail.screenshot).
PROMPT_VERSION = "media-v4"

MEDIA_TYPES = ("photo", "screenshot", "meme", "ad_or_promo", "text_image", "video_frame", "other")
CONTENT_FLAGS = ("adult", "promo_or_ad", "scam_cue", "news_or_politics", "handle_or_url_watermark")

SYSTEM_PROMPT = f"""You read one image (or one video thumbnail) attached to a Bluesky post.
The post belongs to a cluster of accounts that a behaviour detector found posting in a
suspiciously similar way. Your notes are shown publicly as evidence next to that detector result.

Rules:
- Describe only what is visible. Do not guess who made it, why, or whether it is true.
- Never call a person or account a bot, shill or spammer. Describe the media, not the people.
- Sexual, nude or graphic material: set content_flags to include "adult" and write the
  description as exactly "Adult content." (or "Graphic content."). Do not describe bodies, acts
  or clothing; list only text, logos, handles and URLs, in extracted_text.
- extracted_text: legible text in the image, verbatim, one entry per line or block, in reading
  order. Empty list if there is none. Never invent unreadable text.
- reuse_cues: visible signs the image is mass-produced or reused: template layouts, watermarks,
  handles, URLs, QR codes, placeholder slots (a city or name that looks swapped in), stock imagery.
- manipulation_cues: visible signs of manipulated or misleading context: edited or mismatched
  text, fake app or news UI, cropped screenshots that hide context, AI-generated look, a caption
  in the image that contradicts what is shown. Empty list if none; be cautious.
- content_flags: any of {list(CONTENT_FLAGS)} that clearly apply.
- media_type: one of {list(MEDIA_TYPES)}.
- confidence: 0.0-1.0, how sure you are that the description and text are right.

Return only one JSON object with keys: description (string, at most 2 sentences), media_type,
extracted_text, reuse_cues, manipulation_cues, content_flags (arrays of strings), confidence."""


# Explicit words that must never reach public evidence text (the prompt asks; this enforces).
_GRAPHIC = re.compile(
    r"\b(nud(e|ity)|naked|genital\w*|penis|vagina|breasts?|nipples?|sex(ual)? act\w*|erect\w*"
    r"|anal|masturbat\w*|porn\w*)\b",
    re.I,
)
ADULT_DESCRIPTION = "Adult content (not described)."


def safe_description(description: str, flags: list[str]) -> tuple[str, list[str]]:
    """Replace the description of adult media, flagged by the model or caught by word list."""
    if "adult" in flags or _GRAPHIC.search(description):
        return ADULT_DESCRIPTION, flags if "adult" in flags else [*flags, "adult"]
    return description, flags


class NemotronError(RuntimeError):
    """Nemotron could not return a valid analysis for one media item."""


class NotVisionModelError(NemotronError):
    """Not Nemotron Super VL. Fatal: stop instead of storing mislabelled rows."""


@dataclass(frozen=True, slots=True)
class MediaInput:
    """One image or video frame supplied to Nemotron.

    Use ``url`` for a publicly reachable media URL or ``data`` for downloaded bytes. Video is
    represented by its thumbnail frame.
    """

    media_hash: str
    mime_type: str
    data: bytes | None = None
    url: str | None = None
    source_uri: str | None = None

    def image_url(self) -> str:
        if self.data is not None:
            encoded = base64.b64encode(self.data).decode("ascii")
            return f"data:{self.mime_type};base64,{encoded}"
        if self.url:
            return self.url
        raise ValueError("MediaInput requires either url or data")


@dataclass(frozen=True, slots=True)
class MediaAnalysis:
    media_hash: str
    description: str
    extracted_text: list[str]
    reuse_cues: list[str]
    manipulation_cues: list[str]
    confidence: float
    model: str
    latency_ms: int
    usage: dict[str, Any] | None = None
    source_uri: str | None = None
    media_type: str = "other"
    content_flags: list[str] = field(default_factory=list)
    model_requested: str | None = None
    finish_reason: str | None = None
    prompt_version: str = PROMPT_VERSION

    def evidence_detail(self) -> dict[str, Any]:
        """JSON-ready detail for the shared ``evidence`` table."""
        return asdict(self)

    @classmethod
    def from_detail(cls, detail: dict[str, Any]) -> MediaAnalysis:
        """Rebuild from a stored ``evidence.detail`` (the media-hash cache)."""
        names = cls.__dataclass_fields__.keys()
        return cls(**{k: detail[k] for k in names if k in detail})


def is_vl_model(model_id: str | None) -> bool:
    """True when the id names a vision-language model ("...-super-vl-..." / "...-VL-...")."""
    return bool(model_id) and "vl" in re.split(r"[^a-z0-9]+", model_id.lower())


def is_super_vl_model(model_id: str | None) -> bool:
    """Nemotron 3.5 Super VL specifically: the nemotron, super and vl tokens and version 3.5
    (e.g. 'nemotron-3.5-super-vl', 'nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4')."""
    if not model_id:
        return False
    tokens = re.split(r"[^a-z0-9]+", model_id.lower())
    version = any(a == "3" and b == "5" for a, b in zip(tokens, tokens[1:], strict=False))
    return version and {"nemotron", "super", "vl"}.issubset(tokens)


class RateLimiter:
    """Spaces call starts at least 60/rpm seconds apart (one process, one key)."""

    def __init__(
        self,
        rpm: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self._clock, self._sleep = clock, sleep
        self._next = 0.0

    def wait(self) -> None:
        now = self._clock()
        if now < self._next:
            self._sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


def parse_content(content: Any) -> dict[str, Any]:
    """The first JSON object in a reply, tolerating fences, <think> blocks and prose around it."""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str) or not content.strip():
        raise ValueError("response content is empty")
    # Skip braces inside <think> blocks, but decode the original text: a "<think>" written
    # inside a JSON string value (verbatim post text) must survive.
    thinking = [m.span() for m in re.finditer(r"<think>.*?</think>", content, flags=re.S)]
    start = next(
        (i for i, c in enumerate(content) if c == "{" and not any(a <= i < b for a, b in thinking)),
        None,
    )
    if start is None:
        raise ValueError("no JSON object in response")
    # Only the first object: never promote an object nested in a broken envelope.
    payload, _ = json.JSONDecoder().raw_decode(content, start)
    if not isinstance(payload, dict):
        raise ValueError("response JSON is not an object")
    return payload


def _strings(value: Any, limit: int = 20) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"expected a list of strings, got {type(value).__name__}")
    out = [str(v).strip() for v in value if isinstance(v, str | int | float) and str(v).strip()]
    return out[:limit]


def _usage(response: Any) -> dict[str, Any] | None:
    u = getattr(response, "usage", None)
    if u is None:
        return None
    out = {
        k: getattr(u, k)
        for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        if getattr(u, k, None) is not None
    }
    details = getattr(u, "completion_tokens_details", None)
    if getattr(details, "reasoning_tokens", None) is not None:
        out["reasoning_tokens"] = details.reasoning_tokens
    return out


def log_call_attempt(
    *,
    model: str,
    media_hash: str,
    started: float,
    response: Any = None,
    attempt: int = 1,
    outcome: str = "ok",
    error_type: str | None = None,
    task: str = "analyze",
) -> int:
    """One consistent record per API attempt, including failures and comparison calls."""
    latency_ms = round((time.monotonic() - started) * 1000)
    usage = _usage(response) or {}
    choices = getattr(response, "choices", None)
    record = {
        "model": model,
        "returned_model": getattr(response, "model", None),
        "hash": media_hash,
        "task": task,
        "latency_ms": latency_ms,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "finish_reason": getattr(choices[0], "finish_reason", None) if choices else None,
        "attempt": attempt,
        "outcome": outcome,
        "error_type": error_type,
    }
    log.log(
        logging.INFO if outcome == "ok" else logging.WARNING,
        "nemotron call %s",
        " ".join(f"{key}={value}" for key, value in record.items()),
        extra={"nemotron_call": record},
    )
    return latency_ms


class NemotronClient:
    """OpenAI-compatible client: rate limit, exponential backoff, VL check, hash cache."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        client: OpenAI | None = None,
        cache: dict[str, MediaAnalysis] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 4,
        rpm: float = 30,
        max_tokens: int = 4096,
        reasoning: bool = True,
        timeout_s: float = 120,
        screenshot_reasoning: bool = False,
        screenshot_max_tokens: int = 1536,
    ) -> None:
        if not api_key and client is None:
            raise ValueError("Nemotron needs VERDA_INFERENCE_KEY or NVIDIA_API_KEY")
        self.model = model
        self.base_url = base_url
        self._client = client or OpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0
        )
        self._cache = cache if cache is not None else {}
        self._sleep = sleep
        self._max_retries = max(1, max_retries)
        self._limiter = RateLimiter(rpm, sleep=sleep)
        self.max_tokens = max_tokens
        self.reasoning = reasoning
        # The screenshot read is a narrow extraction; reasoning off is about 3x faster
        # (media/README.md, screenshot-v1). A small budget caps a looping reply.
        self.screenshot_reasoning = screenshot_reasoning
        self.screenshot_max_tokens = screenshot_max_tokens
        self._screenshots: dict[str, ScreenshotPost | None] = {}
        self.calls = 0  # model requests made (cache hits excluded)

    @classmethod
    def from_settings(cls, settings: Any) -> NemotronClient:
        """Dedicated Verda Super VL endpoint when its key is set, else NVIDIA's build API."""
        common = dict(
            rpm=settings.media_rpm,
            max_tokens=settings.media_max_tokens,
            reasoning=settings.media_reasoning,
            screenshot_reasoning=settings.media_screenshot_reasoning,
            screenshot_max_tokens=settings.media_screenshot_max_tokens,
        )
        if settings.verda_inference_key:
            return cls(
                settings.verda_inference_key,
                base_url=settings.verda_base_url,
                model=settings.verda_model,
                **common,
            )
        return cls(
            settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            model=settings.nvidia_media_model,
            **common,
        )

    def describe_model(self) -> dict[str, Any]:
        """The served model's id and root from /models; raises if the model isn't served."""
        for m in self._client.models.list().data:
            if m.id == self.model:
                info = {"id": m.id, "root": getattr(m, "root", None), "base_url": self.base_url}
                log.info("nemotron model %s", info)
                return info
        raise NotVisionModelError(f"model {self.model!r} is not served at {self.base_url}")

    def analyze(self, media: MediaInput, *, context: str | None = None) -> MediaAnalysis:
        cached = self._cache.get(media.media_hash)
        if cached is not None:
            return cached
        user_text = "Read this media item and return the JSON object."
        if context:
            user_text += f"\nPost text (context only, not proof): {context[:500]}"
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": media.image_url()}},
                ],
            },
        ]
        response, latency_ms = self._complete(messages, media.media_hash)
        choice = response.choices[0] if response.choices else None
        finish = getattr(choice, "finish_reason", None)
        try:
            if choice is None:
                raise ValueError("no choices in response")
            payload = parse_content(choice.message.content)
            result = self._make_analysis(media, payload, latency_ms, response, finish)
        except (TypeError, ValueError, KeyError) as exc:
            raise NemotronError(
                f"Nemotron returned invalid analysis (finish_reason={finish}): {exc}"
            ) from exc
        self._cache[media.media_hash] = result
        return result

    def read_screenshot(self, media: MediaInput) -> ScreenshotPost | None:
        """Read a screenshot of a social-media post as fields (media/screenshot.py).

        None when the image isn't a screenshot of a post, or nothing findable is legible.
        Raises NemotronError (invalid reply, request failed) or NotVisionModelError.
        """
        from media.screenshot import (
            SCREENSHOT_SYSTEM_PROMPT,
            SCREENSHOT_USER_PROMPT,
            screenshot_from_payload,
        )

        if media.media_hash in self._screenshots:
            return self._screenshots[media.media_hash]
        messages = [
            {"role": "system", "content": SCREENSHOT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": SCREENSHOT_USER_PROMPT},
                    {"type": "image_url", "image_url": {"url": media.image_url()}},
                ],
            },
        ]
        response, latency_ms = self._complete(
            messages,
            media.media_hash,
            reasoning=self.screenshot_reasoning,
            max_tokens=self.screenshot_max_tokens,
            task="screenshot",
        )
        choice = response.choices[0] if response.choices else None
        finish = getattr(choice, "finish_reason", None)
        try:
            if choice is None:
                raise ValueError("no choices in response")
            payload = parse_content(choice.message.content)
            post = screenshot_from_payload(
                payload, media_hash=media.media_hash, model=response.model, latency_ms=latency_ms
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise NemotronError(
                f"Nemotron returned an invalid screenshot read (finish_reason={finish}): {exc}"
            ) from exc
        self._screenshots[media.media_hash] = post
        return post

    def _complete(
        self,
        messages: list[dict[str, Any]],
        media_hash: str,
        *,
        reasoning: bool | None = None,
        max_tokens: int | None = None,
        task: str = "analyze",
    ) -> tuple[Any, int]:
        extra: dict[str, Any] = {}
        if not (self.reasoning if reasoning is None else reasoning):
            extra["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        for attempt in range(self._max_retries):
            self._limiter.wait()
            started = time.monotonic()
            try:
                self.calls += 1
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=max_tokens or self.max_tokens,
                    **extra,
                )
            except openai.OpenAIError as exc:
                status = getattr(exc, "status_code", None)
                retryable = isinstance(exc, openai.APIConnectionError | openai.RateLimitError) or (
                    isinstance(status, int) and status >= 500
                )
                retry = retryable and attempt < self._max_retries - 1
                log_call_attempt(
                    model=self.model,
                    media_hash=media_hash,
                    started=started,
                    attempt=attempt + 1,
                    outcome="retry" if retry else type(exc).__name__,
                    error_type=type(exc).__name__,
                    task=task,
                )
                if not retry:
                    raise NemotronError(f"Nemotron request failed: {exc}") from exc
                delay = min(60.0, 2**attempt) * (1 + random.random() / 2)
                self._sleep(delay)
                continue
            except Exception as exc:  # a programming error, not an API failure: log, re-raise
                log_call_attempt(
                    model=self.model,
                    media_hash=media_hash,
                    started=started,
                    attempt=attempt + 1,
                    outcome=type(exc).__name__,
                    error_type=type(exc).__name__,
                    task=task,
                )
                raise
            returned_model = getattr(response, "model", None)
            valid_model = is_super_vl_model(returned_model)
            latency_ms = log_call_attempt(
                model=self.model,
                media_hash=media_hash,
                started=started,
                response=response,
                attempt=attempt + 1,
                outcome="ok" if valid_model else "NotVisionModelError",
                task=task,
            )
            if not valid_model:
                raise NotVisionModelError(
                    f"asked for {self.model!r} at {self.base_url} but the response came from "
                    f"{returned_model!r}, which is not a Nemotron Super VL model"
                )
            return response, latency_ms
        raise AssertionError("unreachable")

    def _make_analysis(
        self,
        media: MediaInput,
        payload: dict[str, Any],
        latency_ms: int,
        response: Any,
        finish: str | None,
    ) -> MediaAnalysis:
        description = payload.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description must be a non-empty string")
        try:
            confidence = min(1.0, max(0.0, float(payload.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        media_type = str(payload.get("media_type") or "other").strip().lower()
        flags = [f.lower() for f in _strings(payload.get("content_flags"))]
        flags = [f for f in dict.fromkeys(flags) if f in CONTENT_FLAGS]
        description, flags = safe_description(description.strip(), flags)
        return MediaAnalysis(
            media_hash=media.media_hash,
            description=description,
            extracted_text=_strings(payload.get("extracted_text")),
            reuse_cues=_strings(payload.get("reuse_cues")),
            manipulation_cues=_strings(payload.get("manipulation_cues")),
            confidence=round(confidence, 4),
            model=response.model,
            latency_ms=latency_ms,
            usage=_usage(response),
            source_uri=media.source_uri,
            media_type=media_type if media_type in MEDIA_TYPES else "other",
            content_flags=flags,
            model_requested=self.model,
            finish_reason=finish,
        )
