"""Compare prompt/schema variants of the media reader on the same real media.

    python -m media.compare sample OUT.json [--n 30]     freeze a sample from candidate clusters
    python -m media.compare run SAMPLE.json [--variants v1,v2,v2-noreason] [--out RESULTS.json]

The sample holds (did, cid, embed_kind) of real Bluesky posts, so keep it outside git. `run`
prints one row of metrics per variant (media/README.md records them). Nothing is written to the DB.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from common.config import get_settings
from media.blobs import BlobFetcher, BlobFetchError
from media.nemotron import (
    CONTENT_FLAGS,
    MEDIA_TYPES,
    SYSTEM_PROMPT,
    NemotronClient,
    is_vl_model,
    log_call_attempt,
    parse_content,
    safe_description,
)

# The first prompt (valtteri/nemotron, befeb69), kept verbatim as the baseline.
V1_SYSTEM = (
    "You are a forensic media assistant. Return valid JSON only with keys: "
    "description (string), extracted_text (array of strings), "
    "reuse_cues (array of strings), manipulation_cues (array of strings), "
    "confidence (number from 0 to 1). Do not invent unreadable text or facts."
)
V1_USER = (
    "Analyze this image or video frame for Noise Radar. Describe only visible evidence. "
    "Extract legible text, identify visual reuse or near-duplicate cues, and list "
    "cautious signs of manipulation or misleading context. Do not infer political intent."
)
# media-v2 (0802fc5), kept verbatim. "v3" runs the current media.nemotron.SYSTEM_PROMPT (now
# media-v5, not the media-v3 text measured in media/README.md) plus the adult-content guard.
V2_SYSTEM = f"""You read one image (or one video thumbnail) attached to a Bluesky post.
The post belongs to a cluster of accounts that a behaviour detector found posting in a
suspiciously similar way. Your notes are shown publicly as evidence next to that detector result.

Rules:
- Describe only what is visible. Do not guess who made it, why, or whether it is true.
- Never call a person or account a bot, shill or spammer. Describe the media, not the people.
- Sexual or graphic material: say "adult content" (or "graphic content") with no further detail.
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
V2_USER = "Read this media item and return the JSON object."

VARIANTS: dict[str, dict[str, Any]] = {
    # v1: 700-token budget with reasoning on (the API default), JSON mode, strict json.loads.
    "v1": {"system": V1_SYSTEM, "user": V1_USER, "max_tokens": 700, "json_mode": True},
    "v2": {"system": V2_SYSTEM, "user": V2_USER, "max_tokens": 4096},
    "v2-noreason": {"system": V2_SYSTEM, "user": V2_USER, "max_tokens": 4096, "think": False},
    "v3": {"system": SYSTEM_PROMPT, "user": V2_USER, "max_tokens": 4096, "guard": True},
}
# Words that should never appear in public evidence text: explicit detail, or claims about people.
# Its own list, broader than the guard's in media.nemotron, so the guard can't grade itself.
GRAPHIC = re.compile(
    r"\b(nud\w*|naked|genital\w*|penis|vagina|breast\w*|nipple\w*|sex\w*|erect\w*|anal"
    r"|masturbat\w*|porn\w*|crotch|groin|underwear|lingerie)\b",
    re.I,
)
PEOPLE_CLAIMS = re.compile(
    r"\b(bot|bots|shill\w*|spammer\w*|scammer\w*|troll\w*|fake account)\b", re.I
)


def make_sample(n: int) -> list[dict[str, Any]]:
    from common.db import connect
    from media import store
    from media.worker import group_media, load_cluster_media

    s = get_settings()
    statuses = [x for x in s.media_statuses.split(",") if x]
    out, seen = [], set()
    with connect() as conn:
        for c in store.candidate_clusters(
            conn, statuses=statuses, min_score=s.media_min_score, limit=500
        ):
            for g in group_media(load_cluster_media(conn, c["id"]))[:2]:
                if g.media_hash in seen:
                    continue
                seen.add(g.media_hash)
                p = g.posts[0]
                out.append(
                    {
                        "cluster_id": c["id"],
                        "status": c["status"],
                        "did": p.did,
                        "cid": g.media_hash,
                        "embed_kind": p.embed_kind,
                        "pds_host": p.pds_host,
                        "text": (p.text or "")[:500],
                    }
                )
            if len(out) >= n:
                break
    return out[:n]


def _one(client: NemotronClient, v: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": v["system"]},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": v["user"]
                    + (
                        f"\nPost text (context only, not proof): {item['text']}"
                        if v is not VARIANTS["v1"] and item.get("text")
                        else ""
                    ),
                },
                {"type": "image_url", "image_url": {"url": item["data_url"]}},
            ],
        },
    ]
    kw: dict[str, Any] = {"max_tokens": v["max_tokens"], "temperature": 0.1}
    if v.get("json_mode"):
        kw["response_format"] = {"type": "json_object"}
    if v.get("think") is False:
        kw["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
    t = time.monotonic()
    try:
        r = client._client.chat.completions.create(model=client.model, messages=messages, **kw)
    except Exception as exc:  # noqa: BLE001 - measured, not hidden
        latency_ms = log_call_attempt(
            model=client.model,
            media_hash=item["cid"],
            started=t,
            outcome=type(exc).__name__,
            error_type=type(exc).__name__,
        )
        return {"cid": item["cid"], "error": type(exc).__name__, "latency_ms": latency_ms}
    latency_ms = log_call_attempt(model=client.model, media_hash=item["cid"], started=t, response=r)
    ch = r.choices[0] if r.choices else None
    content = ch.message.content if ch else None
    out = {
        "cid": item["cid"],
        "latency_ms": latency_ms,
        "model": r.model,
        "finish": ch.finish_reason if ch else None,
        "null_content": not content,
        "prompt_tokens": r.usage.prompt_tokens if r.usage else None,
        "completion_tokens": r.usage.completion_tokens if r.usage else None,
        "reasoning_tokens": getattr(
            getattr(r.usage, "completion_tokens_details", None), "reasoning_tokens", None
        ),
    }
    if ch is None:
        return out | {"ok": False, "error": "NoChoices"}
    try:
        strict = json.loads(content or "")
        out["strict_ok"] = isinstance(strict, dict) and bool(strict.get("description"))
    except json.JSONDecodeError:
        out["strict_ok"] = False
    try:
        payload = parse_content(content)
        desc = str(payload.get("description") or "")
        text = payload.get("extracted_text") or []
        if v.get("guard"):  # what the pipeline stores (MediaAnalysis applies the same guard)
            flags = [str(f).lower() for f in payload.get("content_flags") or []]
            desc, payload["content_flags"] = safe_description(desc, flags)
        out |= {
            "ok": bool(desc.strip()),
            "description": desc,
            "ocr": text if isinstance(text, list) else [str(text)],
            "flags": payload.get("content_flags") or [],
        }
    except ValueError:
        out["ok"] = False
    return out


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    ok = [r for r in rows if r.get("ok")]
    lat = [r["latency_ms"] for r in rows if "latency_ms" in r]

    def mean(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(statistics.mean(vals)) if vals else None

    return {
        "n": n,
        "valid_json": f"{len(ok)}/{n}",
        "strict_json": f"{sum(bool(r.get('strict_ok')) for r in rows)}/{n}",
        "null_content": sum(bool(r.get("null_content")) for r in rows),
        "cut_off": sum(r.get("finish") == "length" for r in rows),
        "errors": sum("error" in r for r in rows),
        "vl_model": f"{sum(is_vl_model(r.get('model')) for r in rows)}/{n}",
        "with_ocr_text": sum(bool(r.get("ocr")) for r in ok),
        "flagged_adult": sum("adult" in (r.get("flags") or []) for r in ok),
        "graphic_words": sum(bool(GRAPHIC.search(r.get("description", ""))) for r in ok),
        "claims_about_people": sum(
            bool(PEOPLE_CLAIMS.search(r.get("description", ""))) for r in ok
        ),
        "latency_ms_p50": round(statistics.median(lat)) if lat else None,
        "prompt_tokens_mean": mean("prompt_tokens"),
        "completion_tokens_mean": mean("completion_tokens"),
        "reasoning_tokens_mean": mean("reasoning_tokens"),
    }


def run(sample: list[dict[str, Any]], variants: list[str], workers: int = 4) -> dict[str, Any]:
    import base64

    client = NemotronClient.from_settings(get_settings())
    client.describe_model()
    items = []
    with BlobFetcher() as f:
        for it in sample:
            try:
                b = f.fetch(
                    did=it["did"],
                    cid=it["cid"],
                    pds_host=it.get("pds_host"),
                    embed_kind=it.get("embed_kind"),
                )
            except BlobFetchError:
                continue
            items.append(
                it | {"data_url": f"data:{b.mime_type};base64," + base64.b64encode(b.data).decode()}
            )
    results = {}
    for name in variants:
        with ThreadPoolExecutor(workers) as pool:
            rows = list(pool.map(lambda it, v=VARIANTS[name]: _one(client, v, it), items))
        results[name] = {"metrics": _metrics(rows), "rows": rows}
        print(json.dumps({"variant": name, **results[name]["metrics"]}))
    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(prog="python -m media.compare")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("out")
    s.add_argument("--n", type=int, default=30)
    s = sub.add_parser("run")
    s.add_argument("sample")
    s.add_argument("--variants", default="v1,v2,v2-noreason")
    s.add_argument("--out")
    args = p.parse_args()
    if args.cmd == "sample":
        sample = make_sample(args.n)
        with open(args.out, "w") as fh:
            json.dump(sample, fh, indent=1)
        print(f"{len(sample)} media items -> {args.out}")
        return
    with open(args.sample) as fh:
        sample = json.load(fh)
    results = run(sample, args.variants.split(","))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=1)


if __name__ == "__main__":
    main()
