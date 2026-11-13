# Recorded calibration reports

`heldout-calibration.json` and `heldout-errors.md` were generated from clean commit
`4401d0f` with detector `v1.3`, at 2026-11-13 22:01 UTC (November 14, 00:01 in Vilnius).

```bash
uv run python -m eval calibration --split heldout --json-out eval/reports/heldout-calibration.json
uv run python -m eval errors --input eval/reports/heldout-calibration.json --output eval/reports/heldout-errors.md
```

The retained split at run start had one complete window, November 13, 19:50 UTC.
Seven cases were measured: three synthetic campaigns and four synthetic organic hard negatives,
each injected separately into the real recording. There were zero false positives and one
false negative (L3 slow campaign; organic guard 1.0). This is a **tiny, designed synthetic sample**,
not evidence of zero real-world false positives or measured live campaign recall. The inherited
eval README discloses prior smoke-test exposure of this window. No detector thresholds were tuned.

Five cases had a representative cluster score: Brier 0.242707282, ECE 0.29242.
Three had decided-verdict confidence: Brier 0.083336, ECE 0.168.
Two missing clusters have no fabricated zero score; abstentions are excluded only from
verdict-confidence reliability, not campaign miss accounting. All counts, bin intervals and
per-dataset metrics are in the JSON. Repetitions sharing a window are correlated; the Wilson
intervals do not account for that dependence. The next retained hours should be evaluated as
new evidence, not used to tune against this report.

One frozen human sample existed, but no human label records were available at run start.
No real users' post text or labeller identities are included in these committed artifacts.
The recorder warned about a truncated historical gzip tail; the shared loader recovered readable
events and excluded the incomplete 19:40 window, as listed in the report provenance.

The configured local database refused connections. This run was **not** published to eval_runs;
database-backed persistence and the historical-row version audit remain unverified here.
Run `python -m eval calibration --split heldout --write` and `python -m eval audit-versions`
where the configured database is reachable. The same writer and actual cluster INSERT/UPDATE
version paths are covered by offline tests; missing historic versions are never guessed.

Built with Codex. Full validation at this revision: 123 passed, 4 DB-dependent tests skipped;
focused Ruff checks/formatting passed. Local review correctness findings were fixed, while
sequential replay deliberately bounds memory and inherited detector visualization overhead
is outside this eval change.
