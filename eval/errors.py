"""Read stored eval snapshots, never reclassify mutable live clusters."""

import argparse
import json
from collections import Counter
from pathlib import Path

from eval.calibration import DATASET, predicted, validate_cases


def reason(case: dict) -> str:
    kind = case["case_type"]
    if not case["label"]:
        return {
            "news_link": "news burst",
            "fandom": "fan event",
            "live_event": "live event",
            "viral": "viral engagement burst",
        }.get(kind, "bot-like but organic")
    if kind == "L3":
        return "slow campaign missed"
    if case["status"] == "no_cluster":
        return "campaign not grouped"
    if case["status"] == "likely_organic":
        return "organic guard suppressed campaign"
    return "campaign evidence below decision threshold"


def extract_errors(run: dict) -> list[dict]:
    details = run["details"]
    cases = details["cases"]
    validate_cases(cases, details["detector_version"], run["split"])
    return [
        {**case, "error": "FP" if not case["label"] else "FN", "reason": reason(case)}
        for case in cases
        if predicted(case) != bool(case["label"])
    ]


def markdown_report(run: dict) -> str:
    errors = extract_errors(run)
    details = run["details"]
    counts = Counter(case["error"] for case in errors)
    lines = [
        "# Held-out error analysis" if run["split"] == "heldout" else "# Dev error analysis",
        "",
        f"Dataset: `{run['dataset']}`; split: `{run['split']}`; run: `{run.get('id', 'local')}`.",
        f"Detector: `{details['detector_version']}`; eval commit: `{details['eval_commit']}`.",
        f"Measured at: {details['ran_at']}; cases: {run['n']}; "
        f"false positives: {counts['FP']}; false negatives: {counts['FN']}.",
        "",
        "Abstentions and absent clusters are misses for positive cases, not true negatives. "
        "Missing clusters have no invented probability or database ID. "
        "Offline IDs identify replay clusters, not live database rows. "
        "Reasons are descriptive categories, not proven causes.",
        "",
        f"Run provenance: `{json.dumps(details['provenance'], sort_keys=True)}`",
        "",
        "| Error | Case / dataset | Cluster IDs | Families fired | Organic guard | Reason |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    def cell(value) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("`", "'")

    for case in errors:
        clusters = case.get("clusters") or [case]
        if case["error"] == "FP":
            clusters = [cluster for cluster in clusters if cluster["status"] == "coordinated"]
        for cluster in clusters:
            guard = cluster["organic_guard_score"]
            row = [
                case["error"],
                f"{case['case_id']} / {case['dataset']}",
                cluster["cluster_id"]
                if cluster["cluster_id"] is not None
                else "none (not grouped)",
                (", ".join(cluster["families_fired"]) or "none")
                if case.get("signals_available", True)
                else "not snapshotted",
                f"{guard:.4f}" if guard is not None else "not scored",
                case["reason"],
            ]
            lines.append("| " + " | ".join(cell(value) for value in row) + " |")
    if not errors:
        lines.extend(["", "No FP/FN in this run; this is not evidence of zero population error."])
    lines.extend(
        [
            "",
            "## Changes and limits",
            "",
            "This change adds measurement and error visibility only; "
            "no detector thresholds were tuned. "
            "Use the dev split for follow-up fixes, then report a fresh, untouched held-out split. "
            "Synthetic campaign/organic labels come from the generator, not observations of real "
            "coordination. Human labels, when supplied, are reported separately by dataset. "
            "Synthetic class balance is not real-world prevalence. Shared-window repetitions are "
            "correlated; Wilson intervals are descriptive, not a correction for that dependence.",
            "",
        ]
    )
    return "\n".join(lines)


def latest_run(conn, split: str = "heldout") -> dict:
    row = conn.execute(
        "SELECT id, dataset, split, n, details FROM eval_runs "
        "WHERE dataset = %s AND split = %s ORDER BY created_at DESC, id DESC LIMIT 1",
        (DATASET, split),
    ).fetchone()
    if row is None:
        raise ValueError("no calibration run for this split; run calibration first")
    return dict(zip(("id", "dataset", "split", "n", "details"), row, strict=True))


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--split", choices=("heldout", "dev"), default="heldout")
    parser.add_argument("--input", type=Path, help="saved calibration JSON (offline)")
    parser.add_argument("--database-url")
    parser.add_argument("--output", type=Path, default=Path("eval/reports/latest-errors.md"))


def main_args(args) -> int:
    if args.input:
        run = json.loads(args.input.read_text())
        if run["split"] != args.split:
            raise ValueError("saved run is in the wrong split")
    else:
        import psycopg

        from common.config import get_settings

        with psycopg.connect(
            args.database_url or get_settings().database_url, connect_timeout=5
        ) as conn:
            run = latest_run(conn, args.split)
    report = markdown_report(run)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report)
    print(report)
    return 0
