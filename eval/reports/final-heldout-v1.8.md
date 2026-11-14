# Final held-out evaluation - detector v1.8

Run on 2026-11-14 from exact deployed commit `417b57141144` (detector `v1.8`). Dry run: metrics were computed and JSON reports written, but production `eval_runs` rows were not updated.

## Command

```bash
PATH=/home/valtteri/.local/bin:/usr/bin:/bin DRY_RUN=1 HOSTINGER_HOST=srv2159172.hstgr.cloud HOSTINGER_SSH_KEY=/home/valtteri/.ssh/hostinger_ed25519 bash deploy/publish-evals.sh
```

This is the documented `deploy/publish-evals.sh` procedure with `DRY_RUN=1`. It runs campaign recall on both splits plus held-out organic FPR and calibration. `--base-rates` used `signals/fixtures/attack_results_live_v15_20261113T2000Z.json`.

## Held-out results

The split rule assigns whole UTC hours: odd hours are held out. The run used Jetstream hours `2026111319` and `2026111321`, seven complete 10-minute windows (`19:50Z`, `21:00Z`-`21:50Z`); incomplete `19:40Z` was skipped. The recorder warned that the tail of the historical 19-hour gzip was truncated; readable events were retained and incomplete windows excluded.

| Dataset | Result | n | Notes |
|---|---:|---:|---|
| `synthetic_campaigns` | Recall 69/105 = **65.71%** (95% Wilson CI 56.22-74.09%) | 105 | L1 35/35 (100%); L2 34/35 (97.14%); L3 0/35 (0%). L3 was surfaced as `insufficient`, not counted as caught. |
| `organic_synth` | FPR **0/84 = 0%** | 84 | Synthetic hard negatives; 95% Wilson CI 0-4.36%. |
| `organic_live` | FPR **0/211 = 0%** | 211 | Fixed-rule organic bursts; 95% Wilson CI 0-1.79%; among 39 clustered cases, 0 false positives. |
| `calibration` | precision 1.0, recall 0.667, F1 0.8, FPR 0 | 49 | Score reliability: n=35, Brier 0.2519, ECE 0.3005. Verdict confidence: n=24, Brier 0.0838, ECE 0.1834. |

The pipeline also ran dev: synthetic campaign recall was 56/90 = 62.22% on six windows from even UTC hour `2026111320`. This is context only.

JSON artifacts are in `/srv/junction-espoo/data/eval-slices/results/`: `synthetic_campaigns-heldout-v1.8.json`, `organic-heldout-v1.8.json`, `calibration-heldout-v1.8.json`, plus the dev recall file.

## Production and dashboard comparison

At run time `/version.txt` identified deployed commit `417b57141144`, detector `v1.8`. `GET /api/metrics` and the dashboard still showed v1.7 rows (`eval_commit` `0ccc611`): synthetic held-out recall 69/105 = 65.71%; synthetic organic FPR 0/84; live organic FPR 0/211; calibration n=49, precision 1.0, recall 0.667, F1 0.8, FPR 0.0. These values happen to match this run on the shared slices, but their provenance remains v1.7. The API returns the latest `eval_runs` row per dataset/split; `eval/` writes the rows. The metrics are not hard-coded, and methodology documents the datasets, split and limits.

The write-enabled attempt could not open the production DB tunnel: the configured Hostinger SSH key `/home/valtteri/.ssh/hostinger_ed25519` is absent for this seat and SSH host-key verification failed. The dry run wrote no rows. Dashboard/API provenance remains v1.7 until a seat with the authorized key republishes v1.8.

## Method and limits

Campaigns are injected one at a time into recorded real windows and scored with the live detector entry point. A campaign counts as caught when at least half its accounts appear in `coordinated` clusters. L1 is copy-paste text/link from new templated accounts in a short burst; L2 is lightly paraphrased with older accounts and jitter; L3 is heavily paraphrased, aged accounts posting slowly. Organic-live cases are selected by a fixed rule that does not read detector output.

These small designed samples do not estimate real campaign recall or population precision. The same held-out hours were previously evaluated for v1.7, and the 19:50 window had a disclosed earlier smoke test; the split is held out from dev tuning, but not untouched by prior evaluation. Campaigns sharing windows are correlated, while Wilson intervals treat trials as independent. Offline real accounts lack live profile enrichment, identity base rates come from one snapshot, and semantic embeddings are off. Fixed 10-minute replay does not test campaigns split across windows or live sliding/one-hour windows. Zero observed organic FPR does not establish zero real-world false positives. No human labels were available, so live precision and human-label agreement are not measured.
