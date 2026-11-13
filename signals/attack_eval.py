"""Red team vs. defence: do Gemini-written paraphrase campaigns get caught, with and without the
Gemini-embedding signal, and do organic look-alikes on the same (fictional) topics stay unflagged?

    python -m signals.attack_eval --fixtures signals/fixtures/gemini_attacks.json \
        [--live-end 2026-11-13T20:00:00Z] [--out results.json]

The background is a real 10-minute window from the database (--live-end), or a synthetic organic
background when no database is given (--offline). Everything injected is synthetic and in memory.
"""

import argparse
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import psycopg

from common.config import get_settings
from signals.attack import load
from signals.config import DEFAULT
from signals.detect import detect
from signals.semantic import GeminiEmbedder
from signals.synth import empty_window, from_texts, inject
from signals.window import load_window

log = logging.getLogger(__name__)
VARIANTS = [("burst", "fresh"), ("burst", "aged"), ("spread", "fresh"), ("spread", "aged")]


def _outcome(res, s) -> dict:
    hits = [c for c in res.clusters if set(c.members) & s.dids]
    best = max(hits, key=lambda c: len(set(c.members) & s.dids), default=None)
    if best is None:
        return {"status": "no_cluster", "content": 0.0, "semantic": None, "fired": []}
    sem = next((x.score for x in best.signals if x.name == "semantic_duplicate"), None)
    return {
        "status": best.status,
        "content": best.family_scores["content"],
        "semantic": sem,
        "fired": best.families_fired,
        "p": best.p_value,
        "guard": best.guard_score,
        "cover": round(len(set(best.members) & s.dids) / len(s.dids), 2),
    }


def run(fixtures: str, base, embedder) -> dict:
    data = load(fixtures)
    rows = []
    for t_i, theme in enumerate(data["sets"]):
        cases = [
            (
                from_texts(
                    theme["campaign"],
                    base,
                    coordinated=True,
                    timing=tm,
                    accounts=ac,
                    seed=t_i,
                    tag="gc",
                ),
                tm,
                ac,
            )
            for tm, ac in VARIANTS
        ]
        cases.append(
            (
                from_texts(
                    theme["organic"],
                    base,
                    coordinated=False,
                    timing="spread",
                    accounts="aged",
                    seed=t_i,
                    tag="go",
                ),
                "spread",
                "aged",
            )
        )
        cases.append(
            (
                from_texts(
                    theme["organic"],
                    base,
                    coordinated=False,
                    timing="burst",
                    accounts="aged",
                    seed=t_i,
                    tag="gb",
                ),
                "burst",
                "aged",
            )
        )
        for s, tm, ac in cases:
            wd = inject(base, s)

            def info(dids, s=s):
                return {d: s.accounts[d] for d in dids if d in s.accounts}

            without = _outcome(detect(wd, info, DEFAULT), s)
            with_emb = _outcome(detect(wd, info, DEFAULT, embed=embedder), s)
            rows.append(
                {
                    "theme": theme["theme"],
                    "truth": "campaign" if s.coordinated else "organic",
                    "timing": tm,
                    "accounts": ac,
                    "without_embeddings": without,
                    "with_embeddings": with_emb,
                }
            )
            log.info(
                "%-8s %-6s %-5s | without: %-14s | with: %-14s sem=%s",
                rows[-1]["truth"],
                tm,
                ac,
                without["status"],
                with_emb["status"],
                with_emb["semantic"],
            )
    return {
        "fixtures_model": data["model"],
        "detector_version": DEFAULT.version,
        "rows": rows,
        "summary": summarize(rows),
    }


def summarize(rows: list[dict]) -> dict:
    out = {}
    for truth in ("campaign", "organic"):
        for mode in ("without_embeddings", "with_embeddings"):
            sel = [r for r in rows if r["truth"] == truth]
            groups: dict[str, list] = {}
            for r in sel:
                groups.setdefault(f"{r['timing']}/{r['accounts']}", []).append(r[mode]["status"])
            out[f"{truth}|{mode}"] = {
                k: f"{sum(st == 'coordinated' for st in v)}/{len(v)} coordinated"
                for k, v in sorted(groups.items())
            }
    return out


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m signals.attack_eval")
    p.add_argument("--fixtures", default="signals/fixtures/gemini_attacks.json")
    p.add_argument("--live-end", default=None, help="ISO end of a 10-minute live window")
    p.add_argument("--offline", action="store_true", help="synthetic background, no database")
    p.add_argument("--out", default=None)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "httpx2", "google_genai", "signals.semantic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.offline:
        base = empty_window(seed=0)
    else:
        end = (
            datetime.fromisoformat(args.live_end.replace("Z", "+00:00"))
            if args.live_end
            else datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=15)
        )
        with psycopg.connect(get_settings().database_url) as conn:
            base = load_window(conn, end - timedelta(minutes=10), end, DEFAULT)
        log.info("background: live window ending %s (%d posts)", end.isoformat(), len(base.posts))
    emb = GeminiEmbedder()
    t = time.monotonic()
    result = run(args.fixtures, base, emb)
    result["embedding_calls"] = emb.calls
    result["posts_embedded"] = emb.embedded
    result["seconds"] = round(time.monotonic() - t, 1)
    print(json.dumps(result["summary"], indent=1))
    print("embedding calls:", emb.calls, "posts embedded:", emb.embedded)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(result, fh, indent=1)


if __name__ == "__main__":
    main()
