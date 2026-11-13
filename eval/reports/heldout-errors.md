# Held-out error analysis

Dataset: `calibration`; split: `heldout`; run: `local`.
Detector: `v1.3`; eval commit: `4401d0f`.
Measured at: 2026-11-13T22:01:04+00:00; cases: 7; false positives: 0; false negatives: 1.

Abstentions and absent clusters are misses for positive cases, not true negatives. Missing clusters have no invented probability or database ID. Offline IDs identify replay clusters, not live database rows. Reasons are descriptive categories, not proven causes.

Run provenance: `{"adhoc": false, "eval_commit_at_start": "4401d0f", "hand_labels": {"included": 0, "samples": 1, "with_label_records": 0}, "hours_utc": ["2026111319"], "repeats_per_kind": 1, "windows": ["2026-11-13T19:50Z"], "windows_skipped": [{"reason": "recording starts inside this window", "window": "2026-11-13T19:40Z"}]}`

| Error | Case / dataset | Cluster IDs | Families fired | Organic guard | Reason |
| --- | --- | --- | --- | --- | --- |
| FN | 1794599400:L3:2830302642 / synthetic_campaigns | offline:1794599400:L3:2830302642:6bfa2523efc2ebeb | content, network | 1.0000 | slow campaign missed |

## Changes and limits

This change adds measurement and error visibility only; no detector thresholds were tuned. Use the dev split for follow-up fixes, then report a fresh, untouched held-out split. Synthetic campaign/organic labels come from the generator, not observations of real coordination. Human labels, when supplied, are reported separately by dataset. Synthetic class balance is not real-world prevalence. Shared-window repetitions are correlated; Wilson intervals are descriptive, not a correction for that dependence.
