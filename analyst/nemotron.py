"""Nemotron 3.5 Super VL as a single-shot verdict model (the fast path in analyst/runner.py).

Same setup as the lookup reading (lookup/assess.py): the dedicated Verda vLLM endpoint,
reasoning off and the VerdictDraft JSON schema as guided decoding (reasoning on is 5-10x
slower and can spend every token thinking). About 1.8 s per verdict on real clusters.
"""

import logging
import time
from typing import Any

from analyst.verdict import VerdictDraft
from common.config import get_settings
from media.nemotron import is_super_vl_model

log = logging.getLogger(__name__)


class NemotronVerdictModel:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        timeout_s: float = 20.0,
        max_tokens: int = 2048,
        client: Any = None,
    ):
        if client is None:
            if not api_key:
                raise ValueError("VERDA_INFERENCE_KEY is not set")
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0)
        self._client = client
        self.name = model
        self._max_tokens = max_tokens
        self.last_call: dict = {}

    @classmethod
    def from_settings(cls) -> "NemotronVerdictModel":
        s = get_settings()
        return cls(s.verda_inference_key, base_url=s.verda_base_url, model=s.verda_model)

    def draft(self, system: str, prompt: str) -> VerdictDraft:
        started = time.monotonic()
        res = self._client.chat.completions.create(
            model=self.name,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=self._max_tokens,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "verdict", "schema": VerdictDraft.model_json_schema()},
            },
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        returned = getattr(res, "model", None)
        if not is_super_vl_model(returned):  # a shared endpoint once answered with another model
            raise ValueError(f"expected Nemotron 3.5 Super VL, the response came from {returned!r}")
        self.name = returned
        usage = getattr(res, "usage", None)
        self.last_call = {
            "latency_ms": int((time.monotonic() - started) * 1000),
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "completion_tokens", None),
            "thoughts_tokens": None,
            "cost_usd": None,  # self-hosted: no per-token price
        }
        log.info(
            "nemotron call model=%s latency_ms=%d prompt_tokens=%s output_tokens=%s",
            returned,
            self.last_call["latency_ms"],
            self.last_call["prompt_tokens"],
            self.last_call["output_tokens"],
        )
        choice = res.choices[0] if res.choices else None
        if choice is None or not choice.message.content:
            raise ValueError("Nemotron returned no content")
        return VerdictDraft.model_validate_json(choice.message.content)
