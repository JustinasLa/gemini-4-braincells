# eval/: how often is the detector right?

Every number the dashboard shows about our own accuracy comes from here, through `eval_runs`
(`GET /api/metrics` serves the latest row per `dataset` + `split`).

```bash
uv run python -m eval copy-slices --split heldout                 # keep recorded hours (see below)
uv run python -m eval recall --split dev                          # print the table only
uv run python -m eval recall --split heldout --database-url URL   # ...and write an eval_runs row
uv run pytest tests/test_eval_recall.py                           # offline; no DB, no network
```

`--database-url` is opt-in: without it nothing is written anywhere. Other flags: `--repeats N`
(campaigns per level per window, default 5), `--levels L1 L2`, `--workers 4`, `--json-out FILE`,
`--hours YYYYMMDDHH ...` (ad-hoc hours instead of a split; don't report those as `heldout`).

## Datasets and splits

| `dataset` | What it measures | Columns filled |
|---|---|---|
| `synthetic_campaigns` | **recall**: share of injected synthetic campaigns the detector marks `coordinated` | `recall`, `n`, `details` (`precision`, `f1`, `fpr` stay NULL: no negatives here) |

`split` is `dev` (we may look at it while changing the detector or the eval) or `heldout`
(reported only; never tune thresholds, hit rules or synth settings on it). Splits are whole UTC
hours of recorded Jetstream (`eval/slices.py`, `SPLITS`):

| split | UTC hours | Notes |
|---|---|---|
| `dev` | 2026-11-13 19, 20 | hour 19 starts at 19:41 (recording began), so only 19:50 counts |
| `heldout` | 2026-11-13 21 | |

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

## Limits and findings (honest list)

- These are **synthetic** campaigns written by us, so recall here is an upper bound for campaigns
  that look like our generator, not a measured recall on real-world operations.
- One campaign shape per level (12 accounts, one link, four promoted posts). Larger, smaller or
  differently built campaigns aren't measured.
- Real accounts get no account info offline, so organic guards that need it can't fire for real
  accounts that end up in the same cluster as a campaign (live, enrichment would fetch it).
- Fixed 10-minute windows: the live worker also slides the window every minute and runs a 1 h
  window. A campaign split across two fixed windows isn't tested here (campaigns are placed inside
  one window).
