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
| `runner.py` | Routing: `coordinated`/`insufficient` clusters → decision gate, then the verdict model with tools (or one low-thinking call when the gate says the web cannot matter); `likely_organic` → cheap batch model, single shot, no web calls. |
| `decide.py` | Decision gate: one Gemini Decisions API call (`gemini-decision-1`) estimates whether a web check could change the call. |
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
  error result telling Gemini to write the verdict; the next turn has function calling off.
- **Cache.** One process-wide cache by URL and query (6 h; failures 30 min, never retried in a
  run). A page the `sources` worker already scraped for the cluster (`detail.key = page:<url>`) is
  reused from the DB without a new request.
- **Fetch allowlist.** `scrape_url` only accepts URLs already in the cluster's posts or evidence,
  or returned by an earlier search in the same run. Text in posts and pages is data, not
  instructions (said in the prompt; the allowlist limits what an injected instruction could do).
- **Logged.** Every model call (model id, turn, latency, prompt/output/thinking tokens, cost) and
  every tool call (tool, args, endpoint, URL, evidence id, cached, latency) is logged and kept in
  the verdict as `model_calls` / `tool_calls`, plus `cost_usd`.

## Decision gate: skip the tool loop where the web cannot matter

Most of a verdict's time is the verdict turn, not the tools: in production a tool-calling turn
averaged 5.1 s and the verdict-writing turn 18.1 s (about 1,100 thinking tokens). So before a
candidate cluster's agent run, `decide.py` asks the **Gemini Decisions API** one calibrated
yes/no question over the same system prompt and evidence: *could a live web check change the
assessment?* (~0.27 s p50, no output tokens). When `P(web) < ANALYST_GATE_MAX_P_WEB` (0.1), the
verdict model (`GEMINI_MODEL`) answers once, without tools, at `ANALYST_FAST_THINKING=LOW`.
Otherwise the full agent runs at default thinking, unchanged.

- The gate never sets a status. The verdict model still makes its own call and guard still
  checks every citation. The verdict keeps the trace in `gate` (`p_web`, `threshold`,
  `latency_ms`, `route`: `single` or `agent`).
- It fails open: any gate error (timeout, 404, Google's input filter) runs the full agent and
  stores `gate.error`.
- The Decisions API runs only on `models/gemini-decision-1` (Hydrogen and the other generate
  models return 404), and only on `v1beta`: google-genai 3.3 defaults to `/v1/decisions`, which
  returns a bare 404, so `decide.py` passes `api_version="v1beta"`.
- Off: `GEMINI_DECISION_MODEL=` (empty).

Measured with `python -m analyst.compare --modes agent` and `--modes gated` (two processes, so
no shared web cache) on the 15 tool-routed clusters from both runs below. Raw runs:
`compare_results_gate.json`.

| mode | model | JSON valid | citations valid | usable | agrees w/ detector | latency p50 / max (s) | cost / cluster | tool calls (mean) | tool evidence cited |
|---|---|---|---|---|---|---|---|---|---|
| agent | gemini-4-hydrogen | 15/15 | 100% | 15/15 | 6/15 | 29.2 / 49.5 | $0.0540 | 2.5 | 8 |
| gated | gemini-4-hydrogen | 15/15 | 100% | 15/15 | 11/15 | 26.0 / 64.0 | $0.0436 | 1.9 | 10 |

- **Gated clusters get 2.9x faster.** The gate sent 6 of 15 to the single call (149, 394, 405,
  846, 37, 182; `p_web` 0.03-0.08): 125.6 s in total in agent mode, 43.5 s gated (about 21 s →
  7 s each). The other 9 ran the same agent (337.0 s vs 326.5 s, within run-to-run noise). This
  sample picks link-sharing clusters first, so the p50 moves little.
- **None of the gated clusters needed the web.** In the earlier comparison runs Hydrogen cited
  web evidence on 7 of these clusters. All 7 had `p_web` ≥ 0.148, and the gate sent them all to
  the agent.
- **Production mix**: the gate alone, run on the 120 most recent candidate clusters with a
  verdict, would skip the tools on 33% (28/42 `coordinated`, 11/76 `insufficient`). Two
  prompts were blocked by Google's input filter, and both went to the agent.
- **Low thinking is less decisive.** On the gated clusters Hydrogen at LOW said `insufficient`
  more often (846, 37, 182 went from `coordinated` to `insufficient`, which agrees with the
  detector on all three). In a separate single-shot test, LOW changed the assessment on 3 of
  15 clusters compared with default thinking.


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
uv run pytest tests/test_analyst.py tests/test_analyst_agent.py
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
