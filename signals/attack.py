"""Gemini as a red team: it writes synthetic test campaigns that try to evade our detector.

Everything is FICTIONAL and OFFLINE: the topics are invented (made-up towns, laws, products), the
texts are only used in memory to test the detector, nothing is ever posted, and every account
built from them is a `did:plc:synth...` placeholder (see signals/synth.py).

Two kinds of output, both saved as JSON fixtures so eval is reproducible (LLM output isn't):
  campaign  one operator, many personas, ONE message, each post worded differently
  organic   many independent people reacting to the SAME fictional event, each with their own
            opinion: same topic, different messages (the look-alike a semantic signal must not flag)

    python -m signals.attack generate --out signals/fixtures/gemini_attacks.json --n 6
"""

import argparse
import json
import logging
import os
import time
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("GEMINI_ATTACK_MODEL", "gemini-4-hydrogen")

THEMES = [
    "a new parking fee in the fictional town of Velmora",
    "a fictional energy drink brand called ZapNova",
    "a made-up bill called the Riverside Data Act in the fictional country of Ostland",
    "the fictional crypto token GLINT",
    "a fictional supermarket chain, FreshTrove, closing stores",
    "a made-up vaccine-free summer camp program in the fictional region of Haldor",
    "a fictional streaming service, Lumen+, raising prices",
    "a fictional mayor, Dana Krell, of the fictional city of Port Ashby",
]

SYSTEM = """You generate SYNTHETIC TEST DATA for a research tool that detects coordinated
inauthentic behaviour on social media. The data is never posted anywhere; it is only used offline
to measure whether the detector can tell coordinated campaigns from organic conversation.
All topics are fictional. Never mention real people, real organisations, real places, or real
products. Do not include links, @mentions or hashtags. Write like real short social media posts
(10-40 words), varied in tone, length, punctuation and persona."""

CAMPAIGN_PROMPT = """Topic (fictional): {theme}

Write {n} posts for a COORDINATED campaign: one operator controls {n} different personas and wants
them all to push this single message: "{stance}". The operator knows that detectors catch
copy-paste, so every post must say the same thing in clearly DIFFERENT words, with a different
persona voice (parent, student, retiree, small business owner, gamer, ...). No two posts may share
a sentence or a distinctive phrase."""

ORGANIC_PROMPT = """Topic (fictional): {theme}

Write {n} posts by {n} UNRELATED people who independently heard about this today. They have
DIFFERENT opinions (some for, some against, some joking, some asking questions, some unsure) and
mention different details. This is ordinary organic conversation, not a campaign."""

STANCE_PROMPT = """Topic (fictional): {theme}
Give one short campaign message (max 15 words) that an astroturfing operator might push about it."""


class Posts(BaseModel):
    posts: list[str] = Field(description="One string per post")


class Stance(BaseModel):
    message: str


def _client():
    from google import genai

    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def _ask(client, model: str, prompt: str, schema, temperature: float = 1.0):
    from google.genai import types

    t = time.monotonic()
    res = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=temperature,
        ),
    )
    u = res.usage_metadata
    log.info(
        "gemini model=%s latency_ms=%d prompt_tokens=%s output_tokens=%s",
        model,
        (time.monotonic() - t) * 1000,
        getattr(u, "prompt_token_count", None),
        getattr(u, "candidates_token_count", None),
    )
    return schema.model_validate_json(res.text)


def generate(n_themes: int, n_posts: int = 12, model: str = DEFAULT_MODEL) -> dict:
    client = _client()
    out = {
        "model": model,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "Synthetic, fictional, offline test data. Never posted.",
        "sets": [],
    }
    for theme in THEMES[:n_themes]:
        stance = _ask(client, model, STANCE_PROMPT.format(theme=theme), Stance, 0.7).message
        camp = _ask(
            client, model, CAMPAIGN_PROMPT.format(theme=theme, n=n_posts, stance=stance), Posts
        ).posts
        org = _ask(client, model, ORGANIC_PROMPT.format(theme=theme, n=n_posts), Posts).posts
        out["sets"].append(
            {"theme": theme, "stance": stance, "campaign": camp[:n_posts], "organic": org[:n_posts]}
        )
    return out


def load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m signals.attack")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--out", required=True)
    g.add_argument("--n", type=int, default=6, help="number of fictional themes")
    g.add_argument("--posts", type=int, default=12)
    g.add_argument("--model", default=DEFAULT_MODEL)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    data = generate(args.n, args.posts, args.model)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"wrote {len(data['sets'])} theme sets to {args.out}")


if __name__ == "__main__":
    main()
