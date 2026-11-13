# Graph arm: answers to the 3 code questions

Method: `entire brain brief`, `entire graph query`, `entire graph neighbors --direction in`, then direct reads of the spans Graph returned. Line numbers are from the committed tree at b22fdf4.

## 1. How a cluster status reaches `GET /api/clusters/{id}`

Statuses are `coordinated`, `likely_organic` and `insufficient`. The user-facing names are Shill, Chill and Can't tell (yet).

Producer (signals worker, `python -m signals run|once`):
1. `signals/__main__.py:main` (69). The `run` loop calls `run_once` for each window kind (10m every 60 s, 1h every 300 s), and `once` calls it once.
2. `signals/__main__.py:run_once` (31) loads the window with `load_window`, then calls `detect(wd, account_info, cfg, mh_cache)` at line 55.
3. `signals/detect.py:detect` (29) runs `build_keys`, `build_graph` and `communities` (these only group accounts into candidate clusters). It then calls `score_cluster(g, keys, wd, {}, cfg)` for each group at line 43, without account info.
4. `detect` rescores the "suspicious" clusters at line 53, after `account_info(ordered)` (line 49). `account_info` is a closure in `run_once`: `enrich()` followed by `load_account_info()`.
5. `signals/score.py:score_cluster` (701) is where `status` is set.
   - Signals come from `content_signals`, `timing_signals` (which also returns `p_value`), `network_signals` and `identity_signals`. `organic_guards` supplies the guard score.
   - **coordinated**: at least `min_families_coordinated` families fired, `p_value <= max_p_coordinated`, `score >= min_score_coordinated`, and `guard < organic_guard_strong` (about lines 727-733).
   - **likely_organic**: `guard >= organic_guard_strong` and at most 1 family fired.
   - **insufficient**: everything else, with confidence 0.
   - The thresholds are in `signals/config.py` (status block at line 41). The result is a `ClusterScore` with `.status`, built at about line 750.
6. `signals/__main__.py:run_once` line 56 filters the clusters with `signals/store.py:worth_storing` (33). It keeps a cluster if it is `coordinated` or its best family score is at least `MIN_STORE_SCORE`.
7. `signals/store.py:store_clusters` (79) opens one transaction per cluster and calls `_store_one` (95).
8. `_store_one` writes `c.status` in four places:
   - the `campaigns` INSERT (line 146);
   - the `clusters` INSERT (line 166) or UPDATE (line 176) through the shared `row` tuple (line 155);
   - the `UPDATE clusters SET signals` call at about line 255, whose JSON comes from `_signals_json` (44). That JSON includes the human-readable `status_rule` (line 70);
   - the final `UPDATE campaigns` (line 272).
   - The DB column is `clusters.status`, with a CHECK constraint on the three values at `db/schema.sql:54`.

Reader (API):
9. `api/main.py:16` includes `api/routes/clusters.py:router` (prefix `/api/clusters`).
10. `api/routes/clusters.py:get_cluster` (27) takes the `Store` from `api/deps.py:get_store`. It returns 404 if `store.get_cluster` gives `None`.
11. `api/store.py:Store.get_cluster` (96) runs `SELECT {_SUMMARY_COLS} FROM clusters c {_LATEST_VERDICT} WHERE c.id = %s`. `_SUMMARY_COLS` includes `c.status` and `c.signals`. `_LATEST_VERDICT` is a lateral join on the newest `evidence` row with `kind='analyst'`.
12. `api/store.py:_summary` (38) builds a `ClusterSummary`, with `status=row["status"]` copied through `ClusterSummary.model_fields`.
13. `get_cluster` then builds `ClusterDetail(**summary.model_dump(), signals=row["signals"], members, posts, evidence, analyst=row["verdict_detail"])`.
14. `api/schemas.py:ClusterSummary.status: Status` (line 34, with `Status` at line 8) validates the value, and FastAPI serializes it through `response_model=ClusterDetail`.

The analyst does not change `clusters.status`. `analyst/guard.py:enforce` copies `ctx.status` (loaded by `analyst/store.py:load_context`) into `Verdict.status`. The model's own call goes in `llm_assessment`. The JSON therefore has two separate fields: the top-level `status` (detector) and `verdict.status` / `analyst` (analyst).

## 2. Paths that can make a live Oxylabs `/scrape` or `/search` call

The single HTTP sink is `sources/oxylabs.py:OxylabsClient._post` (143), which calls `self._http.post(path, json=body)` at line 150.
- `OxylabsClient.search` (102) posts to `/search`.
- `OxylabsClient.scrape_facts` (120) posts to `/scrape` (the body is built at line 122).

Inside `sources/oxylabs.py`, these are the only two callers of `_post`. Every other path goes through `search()` or `scrape_facts()`.

**A. API entry point, `POST /api/analyze`** (`api/routes/analyze.py:analyze`, line 32). It is the only API route that reaches Oxylabs. `GET /api/clusters*` is DB-only.
- `api/deps.py:get_checker` (26) lazily builds `SourceChecker(OxylabsClient.from_settings())`. It returns `None` if `oxylabs_web_api_key` is unset, in which case no live call is made (line 53 adds a note instead).
- `analyze` line 58 calls `SourceChecker.check([post])` (`sources/checker.py:46`), which has two branches:
  - `check` → `_cached("scrape:<url>")` (88) → `self._client.scrape_facts(u)` (line 59) → `_post("/scrape")`;
  - `check` → `_search_evidence` (76) → `_cached("search:...")` → `self._client.search(...)` (line 83) → `_post("/search")`. This runs for the headline of a scraped page and for the claim phrase from the posted text.
- The call happens only if the request has a `url` or a text phrase of 5 or more words.
- `SourceChecker._cached` keeps an in-process TTL cache (including a failure TTL), so repeated requests may not hit Oxylabs.

**B. Sources worker, `python -m sources worker|cluster|search|scrape`** (`sources/__main__.py:main`, 59).
- `worker`: `main` → `run_worker` (33) → `check_cluster` (23) → `SourceChecker.check(posts, stored=...)` → the same `scrape_facts` / `search` calls as in A. The `stored` argument skips keys already saved as evidence. Clusters come from `store.clusters_to_check`.
- `cluster <id>`: `main` → `check_cluster` (line 85) → `SourceChecker.check`, with the same calls. `--dry-run` still calls Oxylabs but does not save.
- `search <query>`: `main` → `client.search` (line 79) → `_post("/search")`. This is a direct CLI call.
- `scrape <url>`: `main` → `client.scrape_facts` (line 82) → `_post("/scrape")`. Also a direct CLI call.

**C. Analyst worker and CLI** (`python -m analyst worker|cluster`). Gemini function calling drives these.
- `analyst/__main__.py:main` → `_make_model` (141). It builds an `OxylabsClient` only if `analyst_tools` is on and `--no-tools` is not given. If the key is missing it logs a warning and uses DB tools only. It then calls `Analyst.from_settings(web, tools=...)`.
- `worker`: `main` → `run_worker` (55) → `run_cluster` (27, called at line 80). `cluster <id>`: `main` → `run_cluster` (line 127).
- `run_cluster` → `Analyst.verdict` (`analyst/runner.py:69`). A likely_organic cluster takes the early-return path when `self.batch` is set and the status is not in `TOOL_STATUSES` (line 72). That path is a single-shot `analyze` with no tools and no Oxylabs call. Other statuses build a `ToolBox(web=self.web if self.tools else None, ...)` (line 81).
- `Analyst.verdict` → `analyst/service.py:investigate` (26). It returns `no_evidence_verdict` without any call when `ctx.evidence` is empty. Otherwise it calls `GeminiAgent.run` (`analyst/agent.py:130`).
- `GeminiAgent.run` → `ToolBox.call` (`analyst/tools.py:305`, invoked from `agent.py:152`). `call` first checks `Budget.refusal()`, then dispatches by tool name:
  - `search_web` → `ToolBox._search_web` (387) → `_web_evidence` (359) → `self._web.search(query, MAX_SEARCH_HITS)` (line 396) → `_post("/search")`;
  - `scrape_url` → `ToolBox._scrape_url` (411) → `_web_evidence` → `self._web.scrape_facts(url)` (line 426) → `_post("/scrape")`.
- `_web_evidence` (359) is the gate. It tries `sink.existing(key)` (a stored evidence row), then `WebCache`. Only if both miss does it check `Budget.web_refusal()` (`max_web_calls`), increment `web_calls`, and run the live fetch.
- `_scrape_url` only fetches URLs that came from the cluster's posts or evidence, or from an earlier `search_web` result (`_allowed_urls`), and only if `checkable_url()` accepts them.
- `get_cluster_posts` and `get_account` are DB tools. They make no Oxylabs call.

**D. Offline comparison script, `python -m analyst.compare`** (`analyst/compare.py:main`, 196). It builds `OxylabsClient.from_settings()` at line 220 and runs `run_one` (73) in a thread pool. In `agent` mode `run_one` builds a `ToolBox(web=web, ...)`, so it reaches the same `ToolBox.call` → `_search_web` / `_scrape_url` → Oxylabs chain as in C. In `single` mode there are no tools and no live calls.

Not on the path: `signals/` (the detector and `signals/enrich.py` call Bluesky only), `ingest/`, `eval/` and the `GET /api/*` routes.

## 3. The analyst guard (`analyst/guard.py`)

`guard.py` defines `enforce(draft, ctx, *, model) -> Verdict` (line 15) and `no_evidence_verdict(ctx) -> Verdict` (line 66). It has no state and does no I/O.

Callers of `enforce` (from `graph neighbors --direction in`):
- `analyst/service.py:analyze` (19), at line 23. This is the single-shot path, used by `run_cluster` for plain `VerdictModel`s and by `Analyst.verdict` for the batch model.
- `analyst/service.py:investigate` (26), at line 31. This is the Gemini tool-calling path, and it is the main path.
- `analyst/compare.py:run_one` (73), at line 122, in both agent and single modes.

Callers of `no_evidence_verdict`:
- `analyze`, line 21, and `investigate`, line 28, both when `not ctx.evidence`.
- `tests/test_backend_db.py:test_analyst_store_roundtrip`, line 177.

Indirect dependents (everything above `analyze` and `investigate`):
- `Analyst.verdict` (`analyst/runner.py:69`).
- `analyst/__main__.py:run_cluster` (27), which then calls `store.save_verdict` (`analyst/store.py:70`).
- `analyst/__main__.py:run_worker` (55) and the `cluster` CLI.
- Stored `evidence` rows with `kind='analyst'`. The API reads these in `api/store.py`: `_LATEST_VERDICT`, `_summary` (builds `VerdictBrief`) and `get_cluster` (the `analyst` field).
- Tests `tests/test_analyst.py` and `tests/test_analyst_agent.py` (`test_citation_guard_drops_ids_no_tool_returned`).

What the guard guarantees today (and the code relies on it):
- `Verdict.status` is always `ctx.status`, the detector's call. The model never overrides it.
- Citation ids not in `ctx.evidence_ids` are dropped. The model's call is downgraded to `insufficient` if fewer than `MIN_CITATIONS` (2) valid citations remain.
- Confidence is clamped to [0, 1].
- Disagreements are recorded (`agrees_with_detector`, `disagreement_reason`).
- `adjustments` is filled in.
- By design the guard never throws on bad model output. It turns it into a downgraded verdict.

If `enforce` raised instead of returning a verdict:
- `analyze` and `investigate` would propagate the exception, because neither catches it. `investigate` would also lose its post-processing (`tool_calls`, `model_calls`, `cost_usd` in `model_copy`).
- `run_cluster` would raise before `store.save_verdict` and `conn.commit()` (`analyst/__main__.py:27-36`). No `evidence(kind='analyst')` row would be written, so the API's `verdict` stays `None` or stale, and the `analyst` field would be empty or old.
- In `run_worker` (line 79-84) the per-cluster `except Exception` would call `conn.rollback()` and set `failed[cluster_id] = now + 600 s`. The cluster would be retried after 10 minutes. `clusters_needing_verdict` would keep selecting it, because it has no verdict. In the worst case this means a loop of repeated Gemini and Oxylabs spend with no verdict stored. The rollback could also discard tool evidence in the same transaction. `DbSink.save` writes inside `conn`, and `run_cluster` only commits after `save_verdict`, so the `rollback()` at line 82 would drop the live-fetched evidence. That would force repeat Oxylabs calls on retry. (I read this from the code. I did not run it.)
- In `python -m analyst cluster <id>` there is no handler. The CLI would crash with a traceback and save nothing.
- In `analyst/compare.py:run_one` the `except Exception` at line 130 would record `json_valid=False` for the run instead of crashing. The comparison would be skewed: failed runs would count as invalid JSON instead of as a downgraded verdict.
- `no_evidence_verdict` raising would fail the same paths for clusters without evidence. Those clusters are excluded by `HAVING count(e.id) ... > 0` in `analyst/store.py:clusters_needing_verdict`, but `cluster <id>` can still reach it.
- Tests that call `enforce` or `no_evidence_verdict` and assert on the returned `Verdict` would fail.

Brain invariant (recalled in the brief): every Gemini tool result is stored as `kind=source` evidence, so the guard's id check covers anything the model cites. That is why `investigate` passes `full = ctx + tools.new_evidence` into `enforce` (service.py:30-31). Raising or skipping the guard would also remove this citation validation.
