"""Compare the screenshot read (media/screenshot.py) with reasoning on and off, on real screenshots.

    python -m media.compare_screenshot sample OUT.json [--n 40] [--likely-posts]
    python -m media.compare_screenshot run SAMPLE.json [--variants on,off] [--out RESULTS.json]
        [--save-media DIR]

The sample holds (did, cid) of real Bluesky posts and the media-v3 OCR of each, so keep it and
the results outside git. `run` prints one row of metrics per variant and, when both "on" and
"off" ran, their agreement. A variant may repeat ("on,off,off") to see run-to-run variance; the
repeat is reported as "off#2". Nothing is written to the DB.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from common.config import get_settings
from media.blobs import BlobFetcher, BlobFetchError
from media.nemotron import NemotronClient, is_super_vl_model, log_call_attempt, parse_content
from media.screenshot import (
    SCREENSHOT_SYSTEM_PROMPT,
    SCREENSHOT_USER_PROMPT,
    screenshot_from_payload,
)

VARIANTS: dict[str, dict[str, Any]] = {
    "on": {"max_tokens": 4096, "think": True},
    # what NemotronClient.read_screenshot sends by default (screenshot_reasoning=False)
    "off": {"max_tokens": 1536, "think": False},
}
_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: Any) -> list[str]:
    if isinstance(text, list):
        text = " ".join(str(t) for t in text)
    return _TOKEN.findall(str(text or "").lower())


# --likely-posts: most media-v3 "screenshots" are charts, maps, documents and news pages; keep the
# ones whose v3 OCR has an @handle or whose description names a social post, to test the fields.
_SOCIAL = "(post|tweet|social media|bluesky|twitter|threads|reddit|tiktok|instagram|facebook)"
_LIKELY_POST = f"""
              AND ((e.detail->>'extracted_text') ~ '@[A-Za-z0-9_]{{3,}}'
                   OR e.detail->>'description' ~* '{_SOCIAL}')"""


def make_sample(n: int, likely_posts: bool = False) -> list[dict[str, Any]]:
    """The newest distinct media-v3 screenshots, with the source post's embed kind and PDS."""
    from common.db import connect

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (e.detail->>'media_hash')
                   e.cluster_id, e.created_at, e.detail->>'media_hash' AS cid,
                   e.detail->'dids'->>0 AS did, e.detail->>'media_kind' AS media_kind,
                   e.detail->'extracted_text' AS extracted_text,
                   e.detail->'content_flags' AS content_flags,
                   s.embed_kind, a.pds_host
            FROM evidence e
            LEFT JOIN cluster_posts_snapshot s ON s.uri = e.detail->'post_uris'->>0
            LEFT JOIN accounts a ON a.did = e.detail->'dids'->>0
            WHERE e.kind = 'media' AND e.detail->>'prompt_version' = 'media-v3'
              AND e.detail->>'media_type' = 'screenshot'
              AND NOT coalesce(e.detail->'content_flags', '[]'::jsonb) ? 'adult'"""
            + (_LIKELY_POST if likely_posts else "")
            + """
            ORDER BY e.detail->>'media_hash', e.created_at DESC"""
        ).fetchall()
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    out = []
    for r in rows[:n]:
        embed_kind = r["embed_kind"]
        if r["media_kind"] == "video_thumbnail":  # the v3 read saw the video's thumbnail
            embed_kind = "video"
        out.append(
            {
                "cluster_id": r["cluster_id"],
                "cid": r["cid"],
                "did": r["did"],
                "media_kind": r["media_kind"],
                "embed_kind": embed_kind,
                "pds_host": r["pds_host"],
                "extracted_text": r["extracted_text"] or [],
                "content_flags": r["content_flags"] or [],
            }
        )
    return out


def _one(client: NemotronClient, v: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    if "adult" in (item.get("content_flags") or []):  # the worker never sends these either
        return {"cid": item["cid"], "skipped": "adult"}
    messages = [
        {"role": "system", "content": SCREENSHOT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": SCREENSHOT_USER_PROMPT},
                {"type": "image_url", "image_url": {"url": item["data_url"]}},
            ],
        },
    ]
    kw: dict[str, Any] = {"max_tokens": v["max_tokens"], "temperature": 0.1}
    if not v["think"]:
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
            task="screenshot",
        )
        return {"cid": item["cid"], "error": type(exc).__name__, "latency_ms": latency_ms}
    latency_ms = log_call_attempt(
        model=client.model, media_hash=item["cid"], started=t, response=r, task="screenshot"
    )
    ch = r.choices[0]
    out: dict[str, Any] = {
        "cid": item["cid"],
        "latency_ms": latency_ms,
        "model": r.model,
        "finish": ch.finish_reason,
        "null_content": not ch.message.content,
        "prompt_tokens": r.usage.prompt_tokens if r.usage else None,
        "completion_tokens": r.usage.completion_tokens if r.usage else None,
        "reasoning_tokens": getattr(
            getattr(r.usage, "completion_tokens_details", None), "reasoning_tokens", None
        ),
        "raw": ch.message.content,
    }
    try:
        payload = parse_content(ch.message.content)
        post = screenshot_from_payload(
            payload, media_hash=item["cid"], model=r.model, latency_ms=latency_ms
        )
    except (TypeError, ValueError, KeyError) as exc:
        return out | {"ok": False, "parse_error": f"{type(exc).__name__}: {exc}"}
    return out | {
        "ok": True,
        "is_post": payload.get("is_post"),
        "post": post.to_detail() if post else None,
    }


def _grounding(post: dict[str, Any], ocr: list[str]) -> float | None:
    """Share of the read text's word tokens that the media-v3 OCR of the same image also has."""
    words = _tokens(post.get("text"))
    if not words:
        return None
    seen = set(_tokens(ocr))
    return sum(w in seen for w in words) / len(words)


def _pct(vals: list[float], q: float) -> int | None:
    if not vals:
        return None
    s = sorted(vals)
    return round(s[min(len(s) - 1, round(q * (len(s) - 1)))])


def _metrics(rows: list[dict[str, Any]], ocr: dict[str, list[str]]) -> dict[str, Any]:
    n = len(rows)
    ok = [r for r in rows if r.get("ok")]
    posts = [r["post"] for r in ok if r.get("post")]
    lat = [r["latency_ms"] for r in rows if "latency_ms" in r and "error" not in r]
    ground = [g for p in posts if (g := _grounding(p, ocr.get(p["media_hash"], []))) is not None]

    def mean(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(statistics.mean(vals)) if vals else None

    return {
        "n": n,
        "errors": sum("error" in r for r in rows),
        "valid_json": f"{len(ok)}/{n}",
        "null_content": sum(bool(r.get("null_content")) for r in rows),
        "cut_off": sum(r.get("finish") == "length" for r in rows),
        "super_vl_model": f"{sum(is_super_vl_model(r.get('model')) for r in rows)}/{n}",
        "raw_is_post": sum(r.get("is_post") is True for r in ok),
        "posts": len(posts),
        "handle": sum(bool(p["handle"]) for p in posts),
        "display_name": sum(bool(p["display_name"]) for p in posts),
        "text": sum(bool(p["text"]) for p in posts),
        "shown_time": sum(bool(p["shown_time"]) for p in posts),
        "any_count": sum(any(v is not None for v in p["counts"].values()) for p in posts),
        "ui_oddities": sum(bool(p["ui_oddities"]) for p in posts),
        "platforms": dict(Counter(p["platform"] for p in posts).most_common()),
        "grounding_median": round(statistics.median(ground), 3) if ground else None,
        "grounding_under_0_5": sum(g < 0.5 for g in ground),
        "latency_ms_p50": _pct(lat, 0.5),
        "latency_ms_p90": _pct(lat, 0.9),
        "completion_tokens_mean": mean("completion_tokens"),
        "reasoning_tokens_mean": mean("reasoning_tokens"),
    }


def _agreement(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> dict[str, Any]:
    """How often two variants read the same items the same way (both must have parsed)."""
    by_cid = {r["cid"]: r for r in b if r.get("ok")}
    pairs = [(x, by_cid[x["cid"]]) for x in a if x.get("ok") and x["cid"] in by_cid]
    both = [(x["post"], y["post"]) for x, y in pairs if x["post"] and y["post"]]

    def handle(p):
        return (p["handle"] or "").lower() or None

    jacc = []
    for p, q in both:
        s, t = set(_tokens(p["text"])), set(_tokens(q["text"]))
        if s or t:
            jacc.append(len(s & t) / len(s | t))
    return {
        "pairs": len(pairs),
        "same_is_post": sum(bool(x["post"]) == bool(y["post"]) for x, y in pairs),
        "both_posts": len(both),
        "same_handle": sum(handle(p) == handle(q) for p, q in both),
        "same_platform": sum(p["platform"] == q["platform"] for p, q in both),
        "text_jaccard_median": round(statistics.median(jacc), 3) if jacc else None,
        "text_jaccard_under_0_5": sum(j < 0.5 for j in jacc),
    }


def run(
    sample: list[dict[str, Any]],
    variants: list[str],
    workers: int = 4,
    save_media: str | None = None,
) -> dict[str, Any]:
    client = NemotronClient.from_settings(get_settings())
    print(json.dumps({"endpoint": client.describe_model()}))
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
            if save_media:
                os.makedirs(save_media, exist_ok=True)
                path = os.path.join(save_media, f"{len(items):02d}-{it['cid']}.jpg")
                with open(path, "wb") as fh:
                    fh.write(b.data)
            items.append(
                it | {"data_url": f"data:{b.mime_type};base64," + base64.b64encode(b.data).decode()}
            )
    print(json.dumps({"sample": len(sample), "fetched": len(items)}))
    ocr = {it["cid"]: it.get("extracted_text") or [] for it in items}
    results: dict[str, Any] = {}
    for name in variants:
        key, i = name, 1
        while key in results:
            i += 1
            key = f"{name}#{i}"
        with ThreadPoolExecutor(workers) as pool:
            rows = list(pool.map(lambda it, v=VARIANTS[name]: _one(client, v, it), items))
        results[key] = {"metrics": _metrics(rows, ocr), "rows": rows}
        print(json.dumps({"variant": key, **results[key]["metrics"]}))
    keys, agreement = list(results), {}
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            agreement[f"{a} vs {b}"] = _agreement(results[a]["rows"], results[b]["rows"])
            print(json.dumps({"agreement": f"{a} vs {b}", **agreement[f"{a} vs {b}"]}))
    return results | {"agreement": agreement}


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser(prog="python -m media.compare_screenshot")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("out")
    s.add_argument("--n", type=int, default=40)
    s.add_argument(
        "--likely-posts", action="store_true", help="only screenshots that look like posts"
    )
    s = sub.add_parser("run")
    s.add_argument("sample")
    s.add_argument("--variants", default="on,off")
    s.add_argument("--out")
    s.add_argument("--save-media", help="also write the fetched images here (keep outside git)")
    args = p.parse_args()
    if args.cmd == "sample":
        sample = make_sample(args.n, args.likely_posts)
        with open(args.out, "w") as fh:
            json.dump(sample, fh, indent=1)
        print(f"{len(sample)} screenshots -> {args.out}")
        return
    with open(args.sample) as fh:
        sample = json.load(fh)
    results = run(sample, args.variants.split(","), save_media=args.save_media)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=1)


if __name__ == "__main__":
    main()
