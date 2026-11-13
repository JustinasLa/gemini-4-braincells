"""Compare Gemini models as the analyst on real clusters. Writes nothing to the database.

    python -m analyst.compare --per-status 5 --out analyst/compare_results.json
    python -m analyst.compare --clusters 900,529 --modes agent --out other.json
    python -m analyst.compare --table analyst/compare_results.json   # print the README table

Picks the same clusters for every model (per detector status: clusters with stored posts,
those sharing links first so the web tools get exercised, then by score and id), then runs
each model in two modes:
- agent: function calling with live Oxylabs search/scrape + DB lookups (analyst/agent.py),
  tool evidence kept in memory (MemorySink); one WebCache is shared by all models, so a page or
  query is fetched from Oxylabs once per comparison;
- single: the one-shot verdict on pre-gathered evidence (analyst/gemini.py), no tools.

Per run it records: JSON valid (the reply parsed into VerdictDraft), citations (cited ids that
exist / all cited ids), usable (guard did not have to downgrade for missing citations),
agreement with the detector status, latency, tokens, estimated cost and tool calls. Agreement is
not accuracy: the detector has no ground truth on live clusters.
"""

import argparse
import json
import logging
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime

from analyst import store
from analyst.agent import GeminiAgent
from analyst.gemini import GeminiVerdictModel
from analyst.guard import enforce
from analyst.prompt import SYSTEM, build_prompt
from analyst.tools import Budget, MemorySink, ToolBox, WebCache
from common.config import get_settings
from common.db import connect

log = logging.getLogger("analyst.compare")

MODELS = [
    "gemini-4-hydrogen",
    "gemini-4-argon",
    "gemini-4-carbon",
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
]


def pick_clusters(conn, per_status: int) -> list[int]:
    ids: list[int] = []
    for status in ("coordinated", "insufficient", "likely_organic"):
        rows = conn.execute(
            """
            SELECT c.id FROM clusters c
            WHERE c.status = %s
              AND EXISTS (SELECT 1 FROM cluster_posts cp
                          JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
                          WHERE cp.cluster_id = c.id AND s.text IS NOT NULL)
              AND EXISTS (SELECT 1 FROM evidence e WHERE e.cluster_id = c.id)
            ORDER BY EXISTS (SELECT 1 FROM cluster_posts cp
                             JOIN cluster_posts_snapshot s ON s.uri = cp.post_uri
                             WHERE cp.cluster_id = c.id AND cardinality(s.embed_urls) > 0) DESC,
                     c.score DESC, c.id
            LIMIT %s
            """,
            (status, per_status),
        ).fetchall()
        ids += [r["id"] for r in rows]
    return ids


def run_one(model: str, mode: str, cluster_ids: list[int], cache: WebCache, web) -> list[dict]:
    s = get_settings()
    out = []
    with connect(timeout_s=10) as conn:
        for cid in cluster_ids:
            ctx = store.load_context(conn, cid)
            rec = {"model": model, "mode": mode, "cluster_id": cid, "detector": ctx.status}
            started = time.monotonic()
            try:
                if mode == "agent":
                    box = ToolBox(
                        ctx,
                        web=web,
                        sink=MemorySink(),
                        cache=cache,
                        budget=Budget(
                            max_tool_calls=s.analyst_max_tool_calls,
                            max_web_calls=s.analyst_max_web_calls,
                            max_seconds=s.analyst_time_budget_s,
                        ),
                        conn=conn,
                        requested_by=f"gemini:{model}",
                    )
                    res = GeminiAgent(model, api_key=s.gemini_api_key).run(
                        SYSTEM, build_prompt(ctx), box
                    )
                    draft = res.draft
                    known = ctx.evidence_ids | set(box.new_evidence)
                    full = replace(ctx, evidence=[*ctx.evidence, *box.new_evidence.values()])
                    rec.update(
                        cost_usd=res.cost_usd,
                        prompt_tokens=sum(c.prompt_tokens or 0 for c in res.model_calls),
                        output_tokens=sum(c.output_tokens or 0 for c in res.model_calls),
                        model_turns=len(res.model_calls),
                        tool_calls=len(box.records),
                        web_calls=box.budget.web_calls,
                        cited_tool_evidence=sum(
                            1 for c in draft.evidence if c.evidence_id in box.new_evidence
                        ),
                        tools=[
                            {k: r[k] for k in ("name", "endpoint", "source_url", "cached", "ok")}
                            for r in map(asdict, box.records)
                        ],
                    )
                else:
                    m = GeminiVerdictModel(s.gemini_api_key, model)
                    draft = m.draft(SYSTEM, build_prompt(ctx))
                    known, full = ctx.evidence_ids, ctx
                    rec.update(m.last_call, model_turns=1, tool_calls=0, web_calls=0)
                v = enforce(draft, full, model=model)
                cited = [c.evidence_id for c in draft.evidence]
                rec.update(
                    json_valid=True,
                    cited=len(cited),
                    cited_valid=sum(1 for i in cited if i in known),
                    usable=not any("valid citations" in a for a in v.adjustments),
                    llm_assessment=v.llm_assessment,
                    agrees=v.agrees_with_detector,
                    confidence=v.confidence,
                )
            except Exception as exc:  # a failed run is a data point, not a crash
                log.warning("%s %s cluster %s failed: %s", model, mode, cid, exc)
                rec.update(json_valid=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")
            rec["latency_s"] = round(time.monotonic() - started, 2)
            log.info("%s", {k: rec.get(k) for k in ("model", "mode", "cluster_id", "agrees")})
            out.append(rec)
    return out


def summarize(runs: list[dict]) -> list[dict]:
    rows = []
    for mode in ("agent", "single"):
        for model in dict.fromkeys(r["model"] for r in runs):
            rs = [r for r in runs if r["model"] == model and r["mode"] == mode]
            if not rs:
                continue
            ok = [r for r in rs if r.get("json_valid")]
            lat = sorted(r["latency_s"] for r in rs)
            cited = sum(r.get("cited", 0) for r in ok)
            costs = [r.get("cost_usd") for r in ok if r.get("cost_usd") is not None]
            rows.append(
                {
                    "mode": mode,
                    "model": model,
                    "n": len(rs),
                    "json_valid": len(ok),
                    "citations_valid": (
                        sum(r.get("cited_valid", 0) for r in ok) / cited if cited else None
                    ),
                    "usable": sum(1 for r in ok if r.get("usable")),
                    "agrees": sum(1 for r in ok if r.get("agrees")),
                    "latency_p50_s": statistics.median(lat),
                    "latency_max_s": lat[-1],
                    "cost_per_cluster_usd": statistics.mean(costs) if costs else None,
                    "tool_calls_mean": statistics.mean(r.get("tool_calls", 0) for r in ok)
                    if ok
                    else None,
                    "cited_tool_evidence": sum(r.get("cited_tool_evidence", 0) for r in ok),
                }
            )
    return rows


def table(data: dict) -> str:
    lines = [
        "| mode | model | JSON valid | citations valid | usable | agrees w/ detector "
        "| latency p50 / max (s) | cost / cluster | tool calls (mean) | tool evidence cited |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in data["summary"]:
        n = r["n"]
        cv = "n/a" if r["citations_valid"] is None else f"{r['citations_valid']:.0%}"
        cost = "n/a" if r["cost_per_cluster_usd"] is None else f"${r['cost_per_cluster_usd']:.4f}"
        tc = "-" if r["mode"] == "single" else f"{r['tool_calls_mean']:.1f}"
        te = "-" if r["mode"] == "single" else str(r["cited_tool_evidence"])
        lines.append(
            f"| {r['mode']} | {r['model']} | {r['json_valid']}/{n} | {cv} | {r['usable']}/{n} "
            f"| {r['agrees']}/{n} | {r['latency_p50_s']:.1f} / {r['latency_max_s']:.1f} "
            f"| {cost} | {tc} | {te} |"
        )
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="python -m analyst.compare")
    p.add_argument("--per-status", type=int, default=5)
    p.add_argument("--clusters", help="comma-separated cluster ids instead of --per-status")
    p.add_argument("--models", default=",".join(MODELS))
    p.add_argument("--modes", default="agent,single")
    p.add_argument("--out", default="analyst/compare_results.json")
    p.add_argument("--table", help="print the markdown table of a results file and exit")
    args = p.parse_args()
    if args.table:
        with open(args.table) as f:
            print(table(json.load(f)))
        return

    from sources.oxylabs import OxylabsClient

    if args.clusters:
        cluster_ids = [int(x) for x in args.clusters.split(",")]
    else:
        with connect(timeout_s=10) as conn:
            cluster_ids = pick_clusters(conn, args.per_status)
    log.info("clusters: %s", cluster_ids)
    cache = WebCache()
    web = OxylabsClient.from_settings()
    jobs = [(m, mode) for mode in args.modes.split(",") for m in args.models.split(",")]
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        results = list(pool.map(lambda j: run_one(j[0], j[1], cluster_ids, cache, web), jobs))
    runs = [r for rs in results for r in rs]
    data = {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "clusters": cluster_ids,
        "budget": {
            "max_tool_calls": get_settings().analyst_max_tool_calls,
            "max_web_calls": get_settings().analyst_max_web_calls,
            "max_seconds": get_settings().analyst_time_budget_s,
        },
        "summary": summarize(runs),
        "runs": runs,
    }
    with open(args.out, "w") as f:
        json.dump(data, f, indent=1, default=str)
    print(table(data))


if __name__ == "__main__":
    main()
