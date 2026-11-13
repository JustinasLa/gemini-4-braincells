# Token A/B: nograph arm (grep/Glob/Read only)

## 1. Cluster status: producer -> `GET /api/clusters/{id}`

Write path (signals worker), in order:

1. `signals/__main__.py:main` runs `python -m signals run`. The loop calls `run_once` for each window kind, 10m every 60s and 1h every 300s. `once` calls it directly.
2. `signals/__main__.py:run_once` (l.30-63):
   - `load_window` (`signals/window.py`) loads the window data.
   - `detect(wd, account_info, cfg, mh_cache)` is called at l.54.
   - `worth_storing` filters at l.56, then `store_clusters` writes at l.57.
3. `signals/detect.py:detect` (l.29-74):
   - `build_keys` (`signals/keys.py`) builds the shared keys.
   - `build_graph` and `communities` (`signals/graph.py`) produce the groups.
   - Pass 1 is `score_cluster(g, keys, wd, {}, cfg)` at l.43, with no account info.
   - `account_info(ordered)` (the closure in `run_once`, which calls `enrich` and then `load_account_info`) is called at l.49 for the suspicious clusters only.
   - Pass 2 is `score_cluster(groups[i], keys, wd, info, cfg)` at l.53, which overwrites the pass-1 result.
4. `signals/score.py:score_cluster` (l.701-763) is where the status is decided. It calls these helpers in order:
   - `shared_keys` (l.80)
   - `content_signals` (l.94)
   - `timing_signals` (l.197), which returns the permutation-test `p_value`
   - `network_signals` (l.293)
   - `identity_signals` (l.441)
   - `organic_guards` (l.539)

   Then it computes `fam`, `fired`, a noisy-OR `score` over the top two families, and `guard`. The status rule (thresholds from `signals/config.py:42-44` and `organic_guard_strong`):
   - `coordinated` (l.726-732): `len(fired) >= min_families_coordinated` (2), `p_value <= max_p_coordinated` (0.01), `score >= min_score_coordinated` (0.6) and `guard < organic_guard_strong`.
   - `likely_organic` (l.735): `guard >= organic_guard_strong` and `len(fired) <= 1`.
   - `insufficient` (l.738): everything else.

   It returns a `ClusterScore` with `status`, `confidence`, `timeline()` (l.661) and `member_graph()` (l.679).
5. `signals/store.py:worth_storing` (l.33-37) keeps a cluster if it is `coordinated` or its max family score is >= `MIN_STORE_SCORE` (0.5).
6. `signals/store.py:store_clusters` (l.79) opens one transaction per cluster and calls `_store_one` (l.95).
   - `_store_one` matches an existing episode by Jaccard overlap and by campaign.
   - It writes `c.status` into `clusters.status` with an INSERT at l.166 or an UPDATE at l.175. It also writes `campaigns.status` (an INSERT and an UPDATE, both near the end of `_store_one`).
   - `_signals_json` (l.44-76) builds the `clusters.signals` JSON. That JSON includes the `status_rule` text, and `_store_one` writes it with `UPDATE clusters SET signals` at l.256.
   - The DB constraint is in `db/schema.sql:54`: `CHECK (status IN ('coordinated','likely_organic','insufficient'))`.

Read path (API):

7. `api/main.py:14` mounts `clusters.router`.
8. `api/routes/clusters.py:get_cluster` (l.26-31) handles `GET /api/clusters/{cluster_id}`, with `response_model=ClusterDetail`. It gets its store from `api/deps.py:get_store`, which opens a `common.db.connect()` connection and returns 503 on `OperationalError`.
9. `api/store.py:Store.get_cluster` (l.96-145):
   - `SELECT {_SUMMARY_COLS} FROM clusters c {_LATEST_VERDICT}` (l.99-102) reads `c.status` straight from the column.
   - It also queries `cluster_members`, `cluster_posts_snapshot` and `evidence` (`kind <> 'analyst'`).
   - `_summary(row)` (l.38-59) builds a `ClusterSummary`. `status` is copied through `ClusterSummary.model_fields`, and `verdict` is a `VerdictBrief` from the latest analyst evidence row.
   - `ClusterDetail(**summary.model_dump(), signals=..., members=..., posts=..., evidence=..., analyst=row["verdict_detail"])` is built at l.136.
10. `api/schemas.py`: `Status = Literal[...]` (l.8), `ClusterSummary.status` (l.34, "the detector's call") and `ClusterDetail` (l.81). FastAPI serialises these to JSON.

So the JSON `status` is the detector's value, copied from the DB column. The analyst never changes it. The analyst's own call is in `verdict.llm_assessment`. `Verdict.status` is forced to the detector's status by `analyst/guard.py:enforce`.

## 2. Paths that can trigger live Oxylabs `/scrape` or `/search`

All HTTP goes through one place: `sources/oxylabs.py:OxylabsClient._post` (l.143, `self._http.post(path, json=body)` at l.150). It retries up to 2 times on 429/5xx and transport errors. The two public wrappers are `OxylabsClient.search` (l.102, `POST /search`) and `OxylabsClient.scrape_facts` (l.120, `POST /scrape`). Both need the `OXYLABS_WEB_API_KEY` setting (`common/config.py:24`); `OxylabsClient.__init__` (l.78) raises `ValueError` if it is empty.

### A. API entry point, `POST /api/analyze` (the only API route that reaches Oxylabs)

- `api/routes/analyze.py:analyze` (l.31-62) calls `SourceChecker.check` (`sources/checker.py:46`) at l.58. It does this only if the request has a `url` or a 5+-word phrase and the checker is configured.
- For each top link, `check` calls `_cached("scrape:...")` (checker l.59; `_cached` is at l.88), which calls `OxylabsClient.scrape_facts` and then `_post("/scrape")`.
- `_search_evidence` (l.76) calls `_cached("search:...")` (l.83), which calls `OxylabsClient.search` and then `_post("/search")`. This covers the headline of each scraped page (l.65) and the post-text phrase (l.69-71).
- The checker comes from `api/deps.py:get_checker` (l.99-104), which builds `SourceChecker(OxylabsClient.from_settings())` lazily. It returns None, and `analyze` adds a "not configured" note, when no key is set.
- `SourceChecker` has a 6h success / 30min failure in-process cache. Nothing is stored.
- `GET /api/clusters*` and `/api/metrics` never call Oxylabs.

### B. Sources worker (`python -m sources worker`, `deploy/docker-compose.yml:105`)

`sources/__main__.py:main` (l.50) -> `OxylabsClient.from_settings()` (l.68) -> `run_worker` (l.33) -> `store.clusters_to_check` (`sources/store.py:63`) -> `check_cluster` (l.23) -> `SourceChecker.check` (`checker.py:46`) -> `scrape_facts` / `search` as in A. Evidence is saved by `store.save_evidence` (l.27). Evidence already stored under the same key is skipped (`stored` mapping, checker l.56 and l.80).

### C. Sources CLI one-offs (`sources/__main__.py:main`)

- `search <query>` -> `client.search` (l.79)
- `scrape <url>` -> `client.scrape_facts` (l.82)
- `cluster <id> [--dry-run]` -> `check_cluster` -> `SourceChecker.check` (l.85)

### D. Analyst worker (`python -m analyst worker`, compose l.116)

- `analyst/__main__.py:main` -> `_make_model` (the `OxylabsClient.from_settings()` call is inside it, only if `analyst_tools` is on and `--no-tools` was not passed; on `ValueError` it runs without web tools).
- Then `run_worker` (l.55) -> `run_cluster` (l.27) -> `Analyst.verdict` (`analyst/runner.py:148`).
- Routing: clusters whose status is not in `TOOL_STATUSES` (`likely_organic`) take the `analyze(ctx, self.batch)` single-shot path (l.151-152). That path has no web calls.
- `coordinated` and `insufficient` clusters build a `ToolBox(... web=self.web if self.tools else None ...)` (l.160) and call `investigate(ctx, self.agent, box)` (l.169).
- `investigate` (`analyst/service.py:195`) -> `GeminiAgent.run` (`analyst/agent.py:130`), which calls `tools.call(fc.name, fc.args)` at l.152 for each model function call.
- `ToolBox.call` (`analyst/tools.py:305`) -> `_search_web` (l.387) or `_scrape_url` (l.411) -> `ToolBox._web_evidence` (l.359) -> `fetch()`.
  - `search_web`: `self._web.search(query, MAX_SEARCH_HITS)` (l.396) -> `_post("/search")`.
  - `scrape_url`: `self._web.scrape_facts(url)` (l.426) -> `_post("/scrape")`.
- Gates before the live call:
  - `declarations()` only offers the web tools if `self._web` is set.
  - A stored DB row or a `WebCache` hit short-circuits.
  - `Budget.web_refusal` (`max_web_calls`) can raise `BudgetRefused`.
  - `_scrape_url` only accepts URLs from the cluster or from earlier search results.
  - `OxylabsError` is caught, and the failure is cached.
- With `--dry-run` the evidence sink is `MemorySink(read_through=DbSink.existing)`, so stored rows are reused but live calls can still happen for new keys.

### E. Analyst CLI

`python -m analyst cluster <id>` -> `main` -> `_make_model` -> `run_cluster` -> the same chain as D.

### F. Offline benchmark (`python -m analyst.compare`)

`compare.main` (l.196) builds `OxylabsClient.from_settings()` -> `run_one` (l.73) -> `ToolBox(web=web, ...)` (l.83) -> `GeminiAgent.run` -> `ToolBox.call` -> the same `_search_web` / `_scrape_url` chain as D. It runs only in `agent` mode.

Not Oxylabs callers: `signals/`, `ingest/`, `eval/`, and the `api/routes/clusters.py` and `metrics.py` routes.

## 3. What depends on `analyst/guard.py`

Contents: `enforce(draft, ctx, *, model) -> Verdict` (l.15-63), `no_evidence_verdict(ctx) -> Verdict` (l.66-79) and `MIN_CITATIONS = 2`. Its imports are `analyst.context.ClusterContext` and `analyst.verdict.{Verdict, VerdictDraft}` only.

Callers (a repo-wide grep of imports and calls):

- `analyst/service.py:13` imports both functions.
  - `analyze` (l.188) calls `no_evidence_verdict` at l.190 and `enforce(draft, ctx, model=model.name)` at l.192.
  - `investigate` (l.195) calls `no_evidence_verdict` at l.197 and `enforce(result.draft, full, model=agent.name)` at l.200, where `full` adds the tool evidence.
- `analyst/compare.py:33` imports `enforce`, and `run_one` calls it at l.122 (agent and single modes).
- `tests/test_backend_db.py:153` imports `no_evidence_verdict`.
- `tests/test_analyst.py` and `tests/test_analyst_agent.py` reach `enforce` indirectly through `analyze` and `investigate`.

Upstream of `analyze` and `investigate` (the transitive callers):

- `analyst/runner.py:Analyst.verdict` (l.152 and l.169).
- `analyst/__main__.py:run_cluster` (l.33 or l.35).
- `analyst/__main__.py:run_worker` (l.80) and the `cluster` CLI (`main`).
- The analyst service in compose (`python -m analyst worker`).

The API never imports `guard`. It only reads the stored verdict, via `api/store.py:_summary` (l.44-56) and `ClusterDetail.analyst`.

What `enforce` does to the output (what downstream code relies on):

- It drops citations to unknown evidence ids.
- It downgrades the model's assessment to `insufficient` when there are fewer than 2 valid citations.
- It sets `Verdict.status` and `detector_status` to `ctx.status`, so the detector's status is always preserved.
- It sets `llm_assessment`, `agrees_with_detector`, `disagreement_reason` and `adjustments`.
- It clamps `confidence` to [0, 1].

Downstream consumers of those fields:

- `analyst/store.py:save_verdict` (l.70) stores the verdict as `evidence(kind='analyst')`.
- `api/store.py:_summary` reads `llm_assessment`, `agrees_with_detector`, `disagreement_reason` and `tool_calls`.
- `analyst/compare.py` reads `adjustments` for its "usable" metric.

If `enforce` raised instead of returning a verdict:

- Nothing in `service.py` catches it, so `analyze` and `investigate` would propagate it.
- Worker path:
  - `run_cluster` (`__main__.py:27`) would not reach `store.save_verdict` (l.37), so no verdict row is written.
  - `run_worker` catches it at l.81-84. It calls `conn.rollback()`, sets a 600s backoff for that cluster in `failed`, and logs "verdict for cluster %s failed; retry later".
  - The cluster would be picked up again by `clusters_needing_verdict` after the backoff, and would fail again if the cause is deterministic.
  - The worker itself would stay up (the outer `except` at l.85 is a second net).
- The cluster would have no analyst verdict. `api/store.py` returns `verdict=None` and `analyst=None` for it. The detector status in the API is unaffected.
- Agent path cost:
  - The Oxylabs and Gemini calls have already happened by the time `enforce` runs.
  - Tool evidence is kept, because `DbSink.save` (`tools.py:98`) commits each row.
  - The verdict, the model cost and the tool-call records (`tool_calls`, `model_calls`, `cost_usd`) are lost.
  - A retry would reuse the stored `source` evidence and avoid repeat web calls, but would pay for Gemini again.
- `python -m analyst cluster <id>` would exit with a traceback and write nothing.
- `analyst/compare.py:run_one`: the exception is caught by its `except Exception`, which records `json_valid=False` and an `error`. That run is counted as a failure instead of being "usable" or "downgraded".
- Tests: `tests/test_analyst.py` (citation drop, downgrade, disagreement, clamp), `tests/test_analyst_agent.py` (`investigate`) and `tests/test_backend_db.py` (`no_evidence_verdict` round trip) would fail.
- `no_evidence_verdict` is the no-model fallback, so a raise there would remove the "no evidence yet" verdict. Clusters with no evidence would stay verdictless and be retried every backoff cycle.
