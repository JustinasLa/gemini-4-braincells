"""Gemini as the verdict model: structured JSON output against the VerdictDraft schema."""

import logging
import time
from typing import Protocol

from google import genai
from google.genai import types

from analyst.pricing import cost_usd
from analyst.verdict import VerdictDraft
from common.config import get_settings

log = logging.getLogger(__name__)


class VerdictModel(Protocol):
    name: str

    def draft(self, system: str, prompt: str) -> VerdictDraft: ...


class GeminiVerdictModel:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        temperature: float = 0.2,
        client=None,
    ):
        if client is None:
            if not api_key:
                raise ValueError("GEMINI_API_KEY is not set")
            client = genai.Client(api_key=api_key)
        self._client = client
        self.name = model
        self._temperature = temperature
        self.last_call: dict = {}

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
        prompt_tokens = getattr(usage, "prompt_token_count", None)
        thoughts = getattr(usage, "thoughts_token_count", None)
        output = (getattr(usage, "candidates_token_count", None) or 0) + (thoughts or 0)
        self.last_call = {  # read by analyst/compare.py
            "latency_ms": int((time.monotonic() - started) * 1000),
            "prompt_tokens": prompt_tokens,
            "output_tokens": output,  # candidates + thinking, both billed as output
            "cost_usd": cost_usd(self.name, prompt_tokens, output),
            "thoughts_tokens": thoughts,
        }
        log.info(
            "gemini call model=%s latency_ms=%d prompt_tokens=%s output_tokens=%s "
            "thoughts_tokens=%s cost_usd=%s",
            self.name,
            self.last_call["latency_ms"],
            prompt_tokens,
            output,
            thoughts,
            self.last_call["cost_usd"],
        )
        if isinstance(res.parsed, VerdictDraft):
            return res.parsed
        return VerdictDraft.model_validate_json(res.text or "")
