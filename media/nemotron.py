"""Nemotron Super VL client for evidence-producing media analysis."""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import openai
from openai import OpenAI

log = logging.getLogger(__name__)

DEFAULT_MODEL = "nvidia/nemotron-3.5-super-vl-120b-a12b"
DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"


class NemotronError(RuntimeError):
    """Raised when Nemotron cannot return a valid analysis."""


@dataclass(frozen=True, slots=True)
class MediaInput:
    """One image or video frame supplied to Nemotron.

    Use ``url`` for a publicly reachable media URL or ``data`` for bytes downloaded by
    ingest. Exactly one is required. Video is represented as one or more extracted frames.
    """

    media_hash: str
    mime_type: str
    data: bytes | None = None
    url: str | None = None
    source_uri: str | None = None

    def image_url(self) -> str:
        if self.url:
            return self.url
        if self.data is not None:
            encoded = base64.b64encode(self.data).decode("ascii")
            return f"data:{self.mime_type};base64,{encoded}"
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

    def evidence_detail(self) -> dict[str, Any]:
        """Return JSON-ready detail for the shared ``evidence`` table."""
        return {
            "media_hash": self.media_hash,
            "description": self.description,
            "extracted_text": self.extracted_text,
            "reuse_cues": self.reuse_cues,
            "manipulation_cues": self.manipulation_cues,
            "confidence": self.confidence,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "usage": self.usage,
            "source_uri": self.source_uri,
        }


class NemotronClient:
    """Small OpenAI-compatible client with bounded retries and in-process hash caching."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        client: OpenAI | None = None,
        cache: dict[str, MediaAnalysis] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
    ) -> None:
        if not api_key and client is None:
            raise ValueError("NVIDIA_API_KEY is required")
        self.model = model
        self._client = client or OpenAI(api_key=api_key, base_url=base_url)
        self._cache = cache if cache is not None else {}
        self._sleep = sleep
        self._max_retries = max(1, max_retries)

    @classmethod
    def from_settings(cls, settings: Any) -> NemotronClient:
        return cls(
            settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
        )

    def analyze(self, media: MediaInput, *, context: str | None = None) -> MediaAnalysis:
        cached = self._cache.get(media.media_hash)
        if cached is not None:
            return cached

        user_text = (
            "Analyze this image or video frame for Noise Radar. Describe only visible evidence. "
            "Extract legible text, identify visual reuse or near-duplicate cues, and list "
            "cautious signs of manipulation or misleading context. Do not infer political intent."
        )
        if context:
            user_text += f"\nRelevant cluster context (do not treat as proof): {context}"
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a forensic media assistant. Return valid JSON only with keys: "
                    "description (string), extracted_text (array of strings), "
                    "reuse_cues (array of strings), manipulation_cues (array of strings), "
                    "confidence (number from 0 to 1). Do not invent unreadable text or facts."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": media.image_url()}},
                ],
            },
        ]

        started = time.monotonic()
        response = self._complete(messages)
        latency_ms = round((time.monotonic() - started) * 1000)
        try:
            payload = self._parse_payload(response.choices[0].message.content)
            result = self._make_analysis(media, payload, latency_ms, response)
        except (IndexError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise NemotronError(f"Nemotron returned invalid analysis: {exc}") from exc
        self._cache[media.media_hash] = result
        log.info(
            "nemotron media analysis model=%s hash=%s "
            "latency_ms=%d prompt_tokens=%s completion_tokens=%s", # noqa: E501
            self.model,
            media.media_hash,
            result.latency_ms,
            (result.usage or {}).get("prompt_tokens"),
            (result.usage or {}).get("completion_tokens"),
        )
        return result

    def _complete(self, messages: list[dict[str, Any]]) -> Any:
        for attempt in range(self._max_retries):
            try:
                return self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=700,
                    response_format={"type": "json_object"},
                )
            except openai.OpenAIError as exc:
                status = getattr(exc, "status_code", None)
                retryable = status == 429 or (isinstance(status, int) and status >= 500)
                if not retryable or attempt == self._max_retries - 1:
                    raise NemotronError(f"Nemotron request failed: {exc}") from exc
                delay = 2**attempt
                log.warning("retrying Nemotron request status=%s delay_s=%d", status, delay)
                self._sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _parse_payload(content: Any) -> dict[str, Any]:
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str):
            raise TypeError("response content is not text")
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise TypeError("response JSON is not an object")
        return payload

    def _make_analysis(
        self, media: MediaInput, payload: dict[str, Any], latency_ms: int, response: Any
    ) -> MediaAnalysis:
        description = payload["description"]
        confidence = float(payload["confidence"])
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description must be a non-empty string")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")

        def strings(name: str) -> list[str]:
            values = payload.get(name, [])
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                raise TypeError(f"{name} must be an array of strings")
            return values[:20]

        usage_obj = getattr(response, "usage", None)
        usage = None
        if usage_obj is not None:
            usage = {
                key: getattr(usage_obj, key)
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                if getattr(usage_obj, key, None) is not None
            }
        return MediaAnalysis(
            media_hash=media.media_hash,
            description=description.strip(),
            extracted_text=strings("extracted_text"),
            reuse_cues=strings("reuse_cues"),
            manipulation_cues=strings("manipulation_cues"),
            confidence=round(confidence, 4),
            model=self.model,
            latency_ms=latency_ms,
            usage=usage,
            source_uri=media.source_uri,
        )
