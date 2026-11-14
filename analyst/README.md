# analyst/ — Gemini explains each cluster, and investigates first

The detector (`signals/`) decides a cluster's status. The analyst explains it: a short verdict
with the evidence it relies on, the strongest counter-evidence and what would change the call.
For candidate clusters Gemini is an **agent**: it can search the web and fetch pages live
(Oxylabs Web API, through `sources/`) and read the cluster's posts and accounts from our
database before it writes the verdict.

```
clusters + evidence ─▶ prompt ─▶ Gemini ──(function call)──▶ tools.py ─▶ Oxylabs /search, /scrape
                                   ▲                              │      DB: posts, accounts
                                   └──── result + evidence_id ◀───┘      (web results stored as
                                   │                                      evidence kind=source)
                                   ▼ verdict JSON
                                guard.py (ids must exist; status = detector's) ─▶ evidence kind=analyst
```

## Files

| File | What it does |
|---|---|
| `agent.py` | The function-calling loop. Every turn offers the tools and the `VerdictDraft` JSON schema; Gemini either calls tools or returns the verdict. On budget or turn limit, one last turn with calling switched off forces the verdict. |
| `tools.py` | `search_web`, `scrape_url` (live Oxylabs), `get_cluster_posts`, `get_account` (read-only DB). `Budget`, `WebCache`, evidence sinks. |
| `guard.py` | Checks every draft: unknown evidence ids dropped, fewer than 2 valid citations → the model's assessment becomes `insufficient`, status is always the detector's. |
| `runner.py` | Routing: `coordinated`/`insufficient` clusters → decision gate, then the verdict model with tools, or (when the web cannot matter) Nemotron cross-checked by decision-1, escalated to the verdict model on disagreement; `likely_organic` → cheap batch model, single shot, no web calls. |
| `decide.py` | Decision gate: one Gemini Decisions API call (`gemini-decision-1`): could a web check change the call, and decision-1's own status. |
| `nemotron.py` | Nemotron 3.5 Super VL single-shot verdict (Verda, reasoning off, JSON schema). |
| `gemini.py`, `prompt.py`, `service.py`, `store.py` | Single-shot model, prompt, verdict pipeline, DB reads/writes. |
| `pricing.py` | Gemini list prices for cost estimates (source and date inside). |
| `compare.py` | Model comparison on real clusters (below). Writes nothing to the DB. |

## Tools Gemini can call

| Tool | Backend | Result | Citable? |
|---|---|---|---|
| `search_web(query, why)` | Oxylabs Web API `/search` | URLs, titles, snippets, dates | yes: stored as `evidence(kind='source', detail.type='web_search')` |
| `scrape_url(url, why)` | Oxylabs Web API `/scrape` + AI extraction | publisher, author, date, headline, claims, HTTP status | yes: `evidence(kind='source', detail.type='linked_page')` |
| `get_cluster_posts(limit)` | our DB (`cluster_posts_snapshot`) | author, time, text, links | no (context; the detector's signals cover it) |
| `get_account(account)` | our DB (`accounts`, members of this cluster only) | age, followers, posts, avatar/bio, PDS | no |

Rules the code enforces:

- **Stored before seen.** A web result is written as an evidence row (with `source_url`,
  `detail.requested_by = "gemini:<model>"` and the model's `why`) before Gemini gets its
  `evidence_id`. The verdict can cite only ids that exist: the cluster's evidence plus rows the
  tools stored in this run (`guard.enforce`). Every web fact in a verdict has a clickable URL.
- **Budget per cluster** (env): `ANALYST_MAX_TOOL_CALLS=6`, `ANALYST_MAX_WEB_CALLS=4` (live
  Oxylabs requests; cache hits are free), `ANALYST_TIME_BUDGET_S=120`. Calls past a limit get an
  error result telling Gemini to write the verdict; once the tool-call or time budget is used up,
  the next turn has function calling off (past the web limit, DB lookups stay available).
- **Cache.** One process-wide cache by URL and query (6 h; failures 30 min, never retried in a
  run). A page the `sources` worker already scraped for the cluster (`detail.key = page:<url>`) is
  reused from the DB without a new request.
- **Fetch allowlist.** `scrape_url` only accepts URLs already in the cluster's posts or evidence,
  or returned by an earlier search in the same run. Text in posts and pages is data, not
  instructions (said in the prompt; the allowlist limits what an injected instruction could do).
- **Logged.** Every model call (model id, turn, latency, prompt/output/thinking tokens, cost) and
  every tool call (tool, args, endpoint, URL, evidence id, cached, latency) is logged and kept in
  the verdict as `model_calls` / `tool_calls`, plus `cost_usd`.

## Decision gate: fast where the web cannot matter, cross-checked where it is fast

Most of a verdict's time is the verdict turn, not the tools: in production a tool-calling turn
averaged 5.1 s and the verdict-writing turn 18.1 s (about 1,100 thinking tokens). So a candidate
cluster's verdict goes:

```
decision-1 (Gemini Decisions API, ~0.3 s): P(web check could change the call), own status
  ├─ P(web) ≥ 0.1 ─▶ GEMINI_MODEL agent with tools, default thinking (unchanged)
  └─ P(web) < 0.1 ─▶ Nemotron single shot (~2 s)
                       ├─ same assessment as decision-1 ─▶ Nemotron's verdict
                       └─ differs, or Nemotron fails ───▶ GEMINI_MODEL single shot, default thinking
```

- Two independent models must agree before the fast verdict stands; a disagreement is the case
  worth Hydrogen's time. Neither model sets the status (the detector does), and guard checks
  every citation as before. `gate` in the verdict keeps `p_web`, decision-1's `status`,
  `latency_ms` and the `route` (`nemotron`, `escalated`, `single` when there is no Nemotron,
  `agent`).
- Failures take the slower path: a gate error (timeout, Google's input filter) runs the agent; a
  Nemotron error, timeout or wrong model id escalates to GEMINI_MODEL.
- Nemotron runs like the lookup reading (`lookup/assess.py`): the Verda endpoint
  (`VERDA_INFERENCE_KEY`), reasoning off, VerdictDraft as the guided-decoding schema. Without the
  key, or with `ANALYST_FAST_NEMOTRON=false`, the gated path is GEMINI_MODEL single shot.
- The Decisions API runs only on `models/gemini-decision-1` (Hydrogen and the other generate
  models return 404), and only on `v1beta`: google-genai 3.3 defaults to `/v1/decisions`, which
  returns a bare 404, so `decide.py` passes `api_version="v1beta"`.
- Off: `GEMINI_DECISION_MODEL=` (empty).

### Correctness, scored against Bluesky's own takedowns

There are no hand labels yet (`eval/labels.py` has a frozen sample, unlabelled). The external
signal we have is Bluesky moderation: a cluster counts as *taken down* when a member account was
taken down or suspended within 24 h after the cluster was created (accounts actioned before
are left out, as in `eval/takedowns.py`). It is a lower bound (moderation misses many bad
accounts), and "no takedown" is not proof of organic. Sample: all 28 `coordinated`/`insufficient`
clusters with a takedown, plus 32 random ones without. Every option ran single shot on the same
prompt, with no web (where the web can matter, the agent runs in every design). Raw runs:
`gate_takedown_results.json`.

| option | coordinated, taken down (of 28) | coordinated, no takedown (of 32) | latency p50 / mean |
|---|---|---|---|
| detector status | 10 | 3 | – |
| Hydrogen, default thinking | 17 | 4 | 11.8 / 12.1 s |
| Hydrogen, LOW thinking | 15 | 3 | 6.2 / 6.4 s |
| Nemotron alone | 12 | 3 | 1.8 / 1.8 s |
| decision-1's status alone | 18 | 3 | 0.3 s (no explanation) |
| **cascade (this code), live run** | **18** | **3 of 31** | **2.1 / 5.2 s** |

- Nemotron alone is fast but mostly repeats the detector (16 of the 28 taken-down clusters
  `insufficient`). LOW thinking costs Hydrogen accuracy. The cascade escalated 12 of 59 clusters
  (20%) and matched the best option in both columns, at $0.0055 of Gemini per cluster.
- On the clusters the gate actually sends to the fast path (`p_web` < 0.1, 12 here), the
  cascade called all 7 taken-down clusters coordinated and none of the other 5.
- The live run forced the fast path on all 60 (Google's input filter blocked one gate call, so
  59 were scored). In the worker, only gated clusters take it.

### Speed

On the 15 tool-routed clusters of the model comparison (`--modes agent` vs `--modes gated` in
two processes, so no shared web cache; raw runs `compare_results_gate.json`, measured with an
earlier version whose fast path was LOW-thinking Hydrogen), the gate sent 6 clusters to the fast
path (`p_web` 0.03-0.08) and 9 to the agent. The agent runs were unchanged (337.0 s vs 326.5 s in
total, within run-to-run noise). The 7 of those 15 clusters where Hydrogen cited web evidence in
the earlier runs all had `p_web` ≥ 0.148. In agent mode the 6 gated clusters took about 21 s
each; the fast path now takes about 2 s (Nemotron), or about 14 s when it escalates. On the 120
most recent candidate clusters with a verdict, the gate would send 33% to the fast path (28/42
`coordinated`, 11/76 `insufficient`).

## Verdict: the detector decides, Gemini explains

`status` is always the detector's status. Gemini's own call is `llm_assessment`, with
`agrees_with_detector` and `disagreement_reason`. A disagreement (for example Gemini finds the
linked "news" exists only on a ring of fresh blogspot sites) is shown and counted, never applied
silently. The API's cluster summaries carry `verdict.llm_assessment`, `agrees_with_detector`,
`disagreement_reason` and `tool_calls` (count). Older verdicts (before this change) may hold a
downgraded `status` and no `llm_assessment`.

## Run

```bash
uv run python -m analyst cluster 900 --dry-run     # agent verdict, prints JSON, writes nothing
uv run python -m analyst cluster 900 --no-tools    # single-shot verdict
uv run python -m analyst worker                    # keep verdicts fresh (tools on by default)
uv run pytest tests/test_analyst.py tests/test_analyst_agent.py tests/test_analyst_gate.py
```

`--dry-run` keeps tool evidence in memory (ids from 900000000 up), so nothing is written.

## Model comparison

Defaults: **`GEMINI_MODEL=gemini-4-hydrogen`** (verdicts, runs the agent) and
**`GEMINI_BATCH_MODEL=gemini-3.5-flash-lite`** (single-shot verdicts for `likely_organic`
clusters). Measured with `python -m analyst.compare` on real clusters from the production DB
(read-only connection; tool evidence kept in memory), budget 6 tool calls / 4 live web calls /
120 s per cluster. Raw runs: `compare_results.json`, `compare_results_links.json`.

**Run 1: 15 clusters**, 5 per detector status (coordinated, insufficient, likely_organic;
clusters with shared links first, then by score). Ids
149, 275, 394, 405, 846, 22, 37, 45, 116, 182, 21, 72, 103, 114, 121. Most are spam (porn
hashtags, link shorteners), so the models rightly call the web only a few times.

| mode | model | JSON valid | citations valid | usable | agrees w/ detector | latency p50 / max (s) | cost / cluster | tool calls (mean) | tool evidence cited |
|---|---|---|---|---|---|---|---|---|---|
| agent | gemini-4-hydrogen | 15/15 | 100% | 15/15 | 13/15 | 18.0 / 34.4 | $0.0386 | 1.3 | 4 |
| agent | gemini-4-argon | 15/15 | 100% | 15/15 | 13/15 | 14.6 / 76.6 | $0.0147 | 0.3 | 0 |
| agent | gemini-4-carbon | 15/15 | 100% | 15/15 | 12/15 | 43.3 / 133.3 | $0.0560 | 1.8 | 8 |
| agent | gemini-3.8-flash | 15/15 | 100% | 15/15 | 12/15 | 13.5 / 27.1 | $0.0099 | 0.6 | 2 |
| agent | gemini-3.5-flash-lite | 15/15 | 100% | 15/15 | 15/15 | 1.4 / 1.8 | $0.0014 | 0.0 | 0 |
| single | gemini-4-hydrogen | 15/15 | 100% | 15/15 | 12/15 | 13.3 / 25.1 | $0.0234 | - | - |
| single | gemini-4-argon | 15/15 | 100% | 15/15 | 12/15 | 11.3 / 23.9 | $0.0099 | - | - |
| single | gemini-4-carbon | 15/15 | 100% | 15/15 | 12/15 | 24.2 / 87.1 | $0.0281 | - | - |
| single | gemini-3.8-flash | 15/15 | 100% | 15/15 | 13/15 | 10.5 / 20.4 | $0.0060 | - | - |
| single | gemini-3.5-flash-lite | 15/15 | 100% | 15/15 | 14/15 | 1.3 / 1.9 | $0.0012 | - | - |

**Run 2: 5 news-link clusters** (agent mode only), where a source check can matter: a
blogspot "celebrity news" ring (900), NBC/CNN/AP links about the FAA ground stop (529, 675,
831), a TMZ link (669). All five are `insufficient` for the detector.

| mode | model | JSON valid | citations valid | usable | agrees w/ detector | latency p50 / max (s) | cost / cluster | tool calls (mean) | tool evidence cited |
|---|---|---|---|---|---|---|---|---|---|
| agent | gemini-4-hydrogen | 5/5 | 100% | 5/5 | 1/5 | 29.5 / 47.1 | $0.0657 | 3.0 | 6 |
| agent | gemini-4-argon | 5/5 | 100% | 5/5 | 4/5 | 25.2 / 37.7 | $0.0210 | 0.6 | 0 |
| agent | gemini-4-carbon | 5/5 | 100% | 5/5 | 2/5 | 65.1 / 104.1 | $0.0701 | 3.2 | 5 |
| agent | gemini-3.8-flash | 5/5 | 100% | 5/5 | 3/5 | 17.7 / 40.5 | $0.0182 | 1.8 | 2 |
| agent | gemini-3.5-flash-lite | 5/5 | 100% | 5/5 | 4/5 | 1.4 / 1.6 | $0.0013 | 0.0 | 0 |

Columns: *JSON valid* = the reply parsed into the verdict schema; *citations valid* = cited ids
that exist / all cited ids (before guard); *usable* = guard did not have to downgrade for
missing citations; *agrees* = Gemini's `llm_assessment` equals the detector's status (agreement,
not accuracy: there are no ground-truth labels for live clusters yet); *cost* from
`pricing.py` list prices (thinking tokens billed as output); *tool evidence cited* = citations of
evidence the tools stored in that run. Both runs together cost $3.72 in Gemini calls and 27 live
Oxylabs requests (one shared cache).

Why these two:

- **All five models are reliable on format**: 175/175 valid JSON, 100% valid citations, no
  guard downgrades. The choice is about investigation, latency and cost.
- **gemini-4-hydrogen investigates when it matters and stays fast.** On the news clusters it made
  3.0 tool calls per cluster and cited 6 web results. FAA ground stop: it searched the headline
  (675, CNBC among the hits) or scraped the AP article (831) and called both `likely_organic`
  (real news, independent coverage). Blogspot ring (900): it scraped two of the ring's pages and
  searched the story (a February TMZ article came up) and called it `coordinated`.
  On spam it mostly skips the web (1.3 calls, $0.039 per cluster, p50 18 s). Its price is not an
  introductory one.
- **gemini-4-carbon** investigates about as much (3.2 calls, 5 web citations) but is 2-4x slower
  (p50 65 s on news clusters, max 133 s, over the time budget) and costs more, and its price
  doubles on 2026-12-01. **gemini-4-argon** is cheaper but barely uses the tools (0 web
  citations in either run), so it wastes the agent. **gemini-3.8-flash** is a good cheaper
  fallback for the agent (1.8 calls, 2 web citations on news clusters, $0.018).
- **gemini-3.5-flash-lite never calls a tool** (0 calls in 20 agent runs), so it is not an
  agent. Single-shot it is the cheapest and fastest by far ($0.0012 per cluster, 1.3 s p50,
  14/15 agreement), which is what the bulk of `likely_organic` clusters needs.
- Disagreements are almost all on clusters the detector marks `insufficient` (the detector
  abstained; Gemini, after looking, sometimes commits). They are stored as
  `agrees_with_detector=false` with a reason and do not change the status.

The numbers above were measured at commit 1821ed3. The review fixes after it (post links seeded
into the fetch allowlist, a broken tool returns an error instead of aborting, clearer search
summaries) change what the agent may fetch, not the models, so they were not re-measured.

Example after those fixes (cluster 675, `--dry-run`, gemini-4-hydrogen, $0.062, 4 model turns):
4 established accounts shared a CNN link about the FAA nationwide ground stop. Gemini read the
posts, scraped the CNN page (`/scrape`, 5.5 s), searched the story (`/search`, 1.6 s; CNBC among
the hits) and cited both. Its assessment was `likely_organic` against the detector's
`insufficient`, with the reason that the organic guard (0.525) was just under its 0.6 threshold
while live coverage by several major outlets confirms real breaking news. The status stays
`insufficient`; the disagreement is stored and shown.

Re-run: `uv run python -m analyst.compare --per-status 5` (needs `DATABASE_URL`,
`GEMINI_API_KEY`, `OXYLABS_WEB_API_KEY`; about 25 min and $3 for 5 models x 2 modes x 15
clusters).
