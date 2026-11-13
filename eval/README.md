# eval/: how often is the detector right?

Every number the dashboard shows about our own accuracy comes from here, through `eval_runs`
(`GET /api/metrics` serves the latest row per `dataset` + `split`).

```bash
uv run python -m eval copy-slices                                # keep finished recorded hours
uv run python -m eval recall --split dev                          # print the table only
uv run python -m eval recall --split heldout --database-url URL   # ...and write an eval_runs row
uv run pytest tests/test_eval_recall.py                           # offline; no DB, no network
```

`--database-url` is opt-in: without it nothing is written anywhere. Other flags: `--repeats N`
(campaigns per level per window, default 5), `--levels L1 L2`, `--workers 4`, `--json-out FILE`,
`--hours YYYYMMDDHH ...` (ad-hoc hours instead of a split). Ad-hoc or partial runs (`--hours`,
`--max-windows`, a subset of `--levels`) are refused with `--database-url`, so they can't replace
a split's published row.

## Datasets and splits

| `dataset` | What it measures | Columns filled |
|---|---|---|
| `synthetic_campaigns` | **recall**: share of injected synthetic campaigns the detector marks `coordinated` | `recall`, `n`, `details` (`precision`, `f1`, `fpr` stay NULL: no negatives here) |
| `calibration` | **reliability**: coordination score vs coordination ground truth, and decided-verdict confidence vs correctness | `precision`, `recall`, `f1`, `fpr`, `n`, `details`; each reliability metric has its own eligible `n` |

`split` is `dev` (we may look at it while changing the detector or the eval) or `heldout`
(reported only; never tune thresholds, hit rules or synth settings on it). **Split rule** (shared
by every eval here): a window belongs to the UTC hour it starts in; **odd hours are `heldout`,
even hours are `dev`** (`eval/slices.py`, `split_of_hour`). Whole hours, so one burst never
straddles both. `--split` uses every recorded hour of that parity found under `--slice`; the hours
and windows actually used are stored in `details`.

Recorded hours kept so far (2026-11-13, UTC): 19 (`heldout`; recording began 19:41, so only the
19:50 window is complete), 20 (`dev`), 21 (`heldout`). Disclosure: the 19:50 window was used for
a one-window smoke test before the split rule was fixed; nothing was tuned on it.

The recorder keeps raw hours for 24 h only, so these hours are copied (same layout) to
`/srv/junction-espoo/data/eval-slices/jetstream/` on the team server (shared, outside git).

## How the recall eval works (`recall.py`)

1. **Windows** (`slices.py`): the recording is cut into clock-aligned 10-minute windows (the live
   worker's main window). Each window is built in memory with the ingest normaliser and the same
   rules as `signals.window.load_window`: posts in the window; likes/reposts/follows whose subject
   got 2..`max_key_accounts` distinct accounts; per-second background counts of every event; events
   duplicated across Jetstream hosts kept once. No database is involved. Windows the recording
   doesn't fully cover (start, end, or a hole over 60 s) are skipped and listed in `details`.
2. **Inject**: for each window, level (L1, L2, L3) and repeat, one campaign from
   `signals.synth.campaign` (12 accounts, `did:plc:synth*`, seeded per window/level/repeat) is
   added to the real window with `signals.synth.inject`. One campaign per detector run, so
   campaigns never help each other. Nothing is posted anywhere and nothing touches a database.
3. **Detect**: `signals.detect.detect`, the live worker's entry point and config. Only synthetic
   accounts get account info (age, followers...); real accounts in an offline window have none.
4. **Score**: a campaign is **caught** when >= 50% of its accounts are in clusters with status
   `coordinated`. **Surfaced** (reported, not counted as caught) = >= 50% in clusters the worker
   would store at any status, i.e. visible on the dashboard as "can't tell" or "chill". Recall =
   caught / injected, with a 95% Wilson interval, overall and per level.

Levels (from `signals/synth.py`): **L1** copy-paste text + link, brand-new templated accounts,
< 1 min burst. **L2** light paraphrase, 3-8 week old accounts, ~5 min jitter. **L3** heavy
paraphrase, aged accounts with followers, slow drip over the whole window.

`details` holds: `per_level` (caught, surfaced, CI, best-cluster status counts),
`detector_version`, `eval_commit`, `hit_rule`, the windows used and skipped, the baseline
cluster counts of each window without injection, and every trial.

## Results

RESULTS_PLACEHOLDER

## Calibration, frozen errors and detector provenance

```bash
uv run python -m eval calibration --split heldout --json-out /tmp/calibration.json
uv run python -m eval calibration --split heldout --write  # configured DATABASE_URL
uv run python -m eval errors                              # latest calibration row, heldout
uv run python -m eval errors --input /tmp/calibration.json --output eval/reports/latest-errors.md
uv run python -m eval audit-versions                      # read-only, exit 1 on missing versions
uv run pytest tests/test_eval_calibration.py
```

This extends the existing subcommand CLI and uses **only `eval.store.write_eval_run`** to write
the existing `eval_runs` table. No schema/API changes: `/api/metrics` already returns `details`.
`--database-url` also opts in to writing; without it or `--write`, calibration does not connect
to the database. `errors` and `audit-versions` only read the database. `errors` defaults to
`eval/reports/latest-errors.md`; it reads the newest **calibration** row by created time then ID
for the requested split. Older recall/FPR-only runs do not contain enough frozen cluster detail
for a trustworthy error report and are not silently reinterpreted.

### Evaluation units and targets

- Uses the existing `eval.slices` held-out rule and window reader, all complete windows of the
  split, and the same `signals.detect.detect` entry point. Each window receives one injection
  at a time: L1/L2/L3 campaigns plus viral/news_link/live_event/fandom organic hard negatives from
  `signals.synth`. No posts are sent anywhere. `--repeats` defaults to one per type/window.
- Each injection is one labelled **case**, not one independent real campaign. Positive hit rules
  reuse recall's union of coordinated accounts >=50% of injected accounts; negative attribution
  requires >=2 case accounts and >=50% of the cluster, as in organic evaluation.
- A representative cluster (highest raw score; deterministic member-list tie-break) supplies the
  score/confidence/guard. Positives consider all clusters overlapping the injected accounts;
  negatives consider only attributed clusters. `predicted_coordinated` stores the case hit rule,
  separately from the representative cluster's `status` (a partial hit can still be a case FN).
- `coordination_score` compares the representative raw score to the coordination label. A noisy-OR
  signal score is **not promised to be a probability**; this diagram tests that interpretation.
- `verdict_confidence` compares confidence to **correctness of the representative verdict**.
  High confidence in `likely_organic` is not a high probability of coordination. `insufficient`
  verdicts are excluded from this target. Missing clusters have NULL scores, not fabricated zero
  probabilities; they remain in case n, recall and FP/FN accounting. Metric-specific n is explicit.
- `by_dataset` keeps synthetic campaigns, organic negatives and optional human-labelled data
  separate. The designed synthetic class balance is not population prevalence; do not pitch
  its pooled precision or Brier score as live-world accuracy. Heuristic organic-live labels are
  not ground truth and are refused until human-reviewed.
- Bins have equal mass (sizes differ by at most one); `--bins` defaults to 10, capped at eligible
  n. Sorting uses prediction only, never ground truth. Ties may span adjacent bins with overlapping
  min/max predictions. ECE is sum(n_bin/n * abs(mean_prediction - observed_rate)); Brier is mean
  squared prediction error. Wilson 95% intervals describe **observed target rate**, not score
  uncertainty. Shared windows/repeated templates are correlated; intervals do not correct for it.
- `--max-windows` produces an explicitly ad-hoc run. It and saved `--input` recomputations cannot
  publish over a split's database row. No detector threshold is changed by these commands.

### `eval_runs.details` JSON contract (schema_version 1)

`dataset="calibration"`; `split` follows the shared rule. Top-level precision/recall/F1/FPR are
case-classification metrics, NULL when their denominator is zero. `n` includes all labelled cases.

```text
details = {
  schema_version: 1, detector_version: string, eval_commit: string|null,
  ran_at: ISO-8601 UTC, method: "equal_mass",
  unit, score_target, confidence_target, interval_limit, mixture_limit: string,
  provenance: {hours_utc, windows, windows_skipped, repeats_per_kind, adhoc, eval_commit_at_start},
  n_cases: int, positives: int, outcomes: {status: int},
  coordination_score: Reliability, verdict_confidence: Reliability,
  by_dataset: {dataset_name: {n_cases, positives, outcomes, coordination_score, verdict_confidence}},
  cases: [Case]
}
Reliability = {
  n: int, requested_bins: int, brier: number|null, ece: number|null,
  bins: [{index: int, n: int, positives: int,
          min_prediction: number, max_prediction: number,
          mean_prediction: number, observed_rate: number, wilson95: [lower, upper]}]
}
Case = {
  case_id: string, dataset: string, case_type: string, label: 0|1,
  label_source: "synthetic"|"human", window_start: Unix seconds,
  detector_version: string, cluster_id: string|int|null, cluster_ids?: [string|int],
  cluster_id_kind?: string, status: "coordinated"|"likely_organic"|"insufficient"|"no_cluster",
  predicted_coordinated?: boolean,
  clusters?: [{cluster_id, status, score, confidence, families_fired, organic_guard_score, overlap}],
  score: number|null, confidence: number|null, families_fired: [string],
  organic_guard_score: number|null
}
```

Hand-label samples are discovered under `--labels-dir` (default `LABELS_DIR`, or
`/srv/junction-espoo/data/labels`). When records exist, the adapter reuses the label owner's
`eval.labels.load_records` / `final_labels` functions for agreed/adjudicated binary labels,
filters split/version, and records exclusions in `provenance.hand_labels`. If that module is
not yet merged, this is recorded as blocked; no parallel adjudication implementation is used.
Frozen label samples currently omit guard/family detail; these remain NULL/unknown, never
reconstructed from a changed live cluster. Hand-label reliability is unweighted descriptive
reliability of the stratified sample, **not** population calibration; stored `sample_weight`
is available for future design-weighted analysis. No human labels existed at implementation time.

`--labels FILE` alternatively appends a JSON array of human-labelled `Case` snapshots. Supply the
**same measured detector version** and the original window start, preserve DB cluster IDs, use a
separate dataset name (e.g. `hand_labels`), and exclude unresolved/disagreeing labels rather than
guessing them. Both class labels are accepted. Split/version mismatches, duplicate case identities,
nonbinary or heuristic labels, invalid scores and invented scores for missing clusters are refused.
The snapshot captures detector output at labelling/evaluation time: error analysis never joins
mutable current cluster rows. No hand labels are manufactured if none exist.

Offline replay IDs have the form `offline:<window>:<kind>:<seed>:<member-digest>` and **are not
database primary keys**. Every snapshot and eval run names its detector version. Signals already
writes the version on both cluster INSERT and UPDATE and in `signals` JSON; regression tests
exercise those actual SQL paths. `audit-versions` checks every current cluster and eval row,
including disagreement between column and JSON version, without backfilling guessed versions.

The committed reports record actual runs, categories, signals and limitations. Reason categories
are descriptive, not causal attribution. Follow-up detector fixes belong on the dev split; never
tune them against these held-out errors. Built with Codex.

## Limits and findings (honest list)

- These are **synthetic** campaigns written by us, so recall here is an upper bound for campaigns
  that look like our generator, not a measured recall on real-world operations.
- One campaign shape per level (12 accounts, one link, four promoted posts). Larger, smaller or
  differently built campaigns aren't measured.
- Real accounts get no account info offline, so organic guards that need it can't fire for real
  accounts that end up in the same cluster as a campaign (live, enrichment would fetch it).
- `deleted` on offline posts/actions only reflects deletes seen up to the window's end (what the
  live worker sees when it loads the window). The detector doesn't read it; `signals/store.py`
  only copies it into snapshots.
- Fixed 10-minute windows: the live worker also slides the window every minute and runs a 1 h
  window. A campaign split across two fixed windows isn't tested here (campaigns are placed inside
  one window).
