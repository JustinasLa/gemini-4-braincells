"""Gemini as the verdict model: structured JSON output against the VerdictDraft schema."""

import logging
import time
from typing import Protocol

from google import genai
from google.genai import types

from analyst.verdict import VerdictDraft
from common.config import get_settings

log = logging.getLogger(__name__)


class VerdictModel(Protocol):
    name: str

    def draft(self, system: str, prompt: str) -> VerdictDraft: ...


class GeminiVerdictModel:
    def __init__(self, api_key: str, model: str, *, temperature: float = 0.2):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set")
        self._client = genai.Client(api_key=api_key)
        self.name = model
        self._temperature = temperature

    @classmethod
    def from_settings(cls) -> "GeminiVerdictModel":
        s = get_settings()
        return cls(s.gemini_api_key, s.gemini_model)

    def draft(self, system: str, prompt: str) -> VerdictDraft:
        started = time.monotonic()
        res = self._client.models.generate_content(
            model=self.name,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=VerdictDraft,
                temperature=self._temperature,
            ),
        )
        usage = res.usage_metadata
        log.info(
            "gemini call model=%s latency_ms=%d prompt_tokens=%s output_tokens=%s",
            self.name,
            (time.monotonic() - started) * 1000,
            getattr(usage, "prompt_token_count", None),
            getattr(usage, "candidates_token_count", None),
        )
        if isinstance(res.parsed, VerdictDraft):
            return res.parsed
        return VerdictDraft.model_validate_json(res.text or "")
