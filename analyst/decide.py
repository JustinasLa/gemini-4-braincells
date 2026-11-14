"""Decision gate: gemini-decision-1 triages a candidate cluster before the verdict model runs.

The Gemini Decisions API answers calibrated questions about a shared input in one call
(~0.3 s, no output tokens). It runs only on `models/gemini-decision-1` (generate models such as
gemini-4-hydrogen return 404) and only on v1beta: google-genai defaults to /v1/decisions, which
404s, so every call passes `api_version="v1beta"`.

One question per cluster: could a live web check change the assessment? When P(web) is below
the threshold, the runner skips the agent's tool loop and asks the verdict model once with low
thinking (about 2x faster: the verdict turn, not the tool turns, is where the time goes). The
gate never decides a status: the verdict model still makes its own call and guard still checks
it. Any gate failure falls back to the full agent.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

from google import genai

log = logging.getLogger(__name__)

API_VERSION = "v1beta"

NEEDS_WEB = {
    "type": "noul",
    "instructions": (
        "Before writing a verdict, an analyst can make LIVE web checks: search a headline or "
        "claim, or fetch a page linked in the posts, to learn who published it, when, and whether "
        "independent outlets report the same thing. Decide whether such a web check could change "
        "the assessment of this cluster."
    ),
    "criteria": {
        "true_description": (
            "The posts share a news link, headline or distinctive factual claim whose publisher, "
            "date or independent coverage is unknown and would point toward real news (organic) "
            "or a fake-news ring (campaign)."
        ),
        "false_description": (
            "Spam, porn, hashtag or follower farming, link shorteners, giveaways, "
            "self-promotion, opinions without a checkable claim, or the evidence already settles "
            "the call."
        ),
    },
}


@dataclass
class GateResult:
    model: str
    p_web: float  # calibrated P(a web check could change the assessment)
    threshold: float
    latency_ms: int
    input_tokens: int | None

    @property
    def skip_tools(self) -> bool:
        return self.p_web < self.threshold


class DecisionGate:
    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        client: Any | None = None,
        threshold: float = 0.1,
        timeout_s: float = 10.0,
    ):
        if client is None:
            if not api_key:
                raise ValueError("GEMINI_API_KEY is not set")
            client = genai.Client(api_key=api_key)
        self._client = client
        self.name = model.removeprefix("models/")
        self.threshold = threshold
        self._timeout_s = timeout_s

    def check(self, system: str, prompt: str) -> GateResult | None:
        """The gate's answer, or None when the call failed (the caller runs the full agent)."""
        started = time.monotonic()
        try:
            res = self._client.decisions.create(
                api_version=API_VERSION,
                model=f"models/{self.name}",
                input=f"{system}\n\n{prompt}",
                questions={"needs_web": NEEDS_WEB},
                timeout=self._timeout_s,
            )
            p_web = float(res.answers["needs_web"].noul)
        except Exception as exc:  # the gate is an optimisation: never block a verdict on it
            log.warning(
                "decision gate failed (%s: %s); running the full agent", type(exc).__name__, exc
            )
            return None
        usage = getattr(res, "usage", None)
        result = GateResult(
            model=self.name,
            p_web=round(p_web, 4),
            threshold=self.threshold,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=getattr(usage, "total_input_tokens", None),
        )
        log.info(
            "decision gate model=%s p_web=%.3f threshold=%.2f skip_tools=%s latency_ms=%d",
            result.model,
            result.p_web,
            result.threshold,
            result.skip_tools,
            result.latency_ms,
        )
        return result
