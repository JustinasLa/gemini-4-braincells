"""Held-out reliability of raw coordination scores and decided-verdict confidence."""

import argparse
import hashlib
import json
import math
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from eval.calibration_labels import load_hand_labels
from eval.recall import LEVELS, _git_sha, _synthetic_info, judge, trial_seed, wilson
from eval.slices import SLICE_ROOT, hour_files, iter_windows, split_hours, split_of_hour
from eval.store import write_eval_run
from signals.config import DEFAULT
from signals.detect import detect
from signals.keys import MinHashCache
from signals.synth import campaign, hard_negative, inject

DATASET = "calibration"
NEGATIVES = ("viral", "news_link", "live_event", "fandom")
STATUSES = ("coordinated", "likely_organic", "insufficient", "no_cluster")


def reliability(probabilities: list[float], targets: list[int], bins: int = 10) -> dict:
    """Equal-mass bins; Wilson intervals describe the observed binary target rate."""
    if bins < 1 or len(probabilities) != len(targets):
        raise ValueError("positive bins and equally sized probabilities/targets required")
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities):
        raise ValueError("probabilities must be finite and in [0, 1]")
    if any(type(target) is not int or target not in (0, 1) for target in targets):
        raise ValueError("targets must be binary integers")
    ordered = sorted(zip(probabilities, targets, strict=True), key=lambda pair: pair[0])
    count = len(ordered)
    actual_bins = min(bins, count)
    output = []
    for index in range(actual_bins):
        start, end = index * count // actual_bins, (index + 1) * count // actual_bins
        group = ordered[start:end]
        positives = sum(target for _, target in group)
        output.append(
            {
                "index": index,
                "n": len(group),
                "positives": positives,
                "min_prediction": group[0][0],
                "max_prediction": group[-1][0],
                "mean_prediction": sum(prediction for prediction, _ in group) / len(group),
                "observed_rate": positives / len(group),
                "wilson95": list(wilson(positives, len(group))),
            }
        )
    return {
        "n": count,
        "requested_bins": bins,
        "bins": output,
        "brier": sum((prediction - target) ** 2 for prediction, target in ordered) / count
        if count
        else None,
        "ece": sum(
            group["n"] * abs(group["mean_prediction"] - group["observed_rate"]) for group in output
        )
        / count
        if count
        else None,
    }


def validate_cases(cases: list[dict], version: str, split: str) -> None:
    """Reject mixed versions, split leakage, duplicate cases, and invalid labels."""
    if not isinstance(version, str) or not version.strip():
        raise ValueError("detector_version is required")
    identities = set()
    for case in cases:
        identity = (case["dataset"], case["case_id"])
        if identity in identities:
            raise ValueError("duplicate case")
        identities.add(identity)
        if case["detector_version"] != version:
            raise ValueError("mixed detector versions")
        if case["status"] not in STATUSES or type(case["label"]) is not int:
            raise ValueError("invalid status or label")
        if case["label"] not in (0, 1):
            raise ValueError("ground truth label must be 0 or 1")
        if "predicted_coordinated" in case and type(case["predicted_coordinated"]) is not bool:
            raise ValueError("predicted_coordinated must be a boolean")
        if (
            split_of_hour(datetime.fromtimestamp(case["window_start"], UTC).strftime("%Y%m%d%H"))
            != split
        ):
            raise ValueError("case is in the wrong split")
        if case["label_source"] not in ("synthetic", "human"):
            raise ValueError("heuristic labels are not ground truth")
        for field in ("score", "confidence", "organic_guard_score"):
            value = case[field]
            if value is not None and (not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError(f"invalid {field}")
        if case["status"] == "no_cluster":
            if case["cluster_id"] is not None or case["score"] is not None:
                raise ValueError("missing clusters must not have invented IDs or scores")
        elif case["cluster_id"] is None or case["score"] is None:
            raise ValueError("clustered cases need cluster_id and score")
        if case["status"] in ("coordinated", "likely_organic") and case["confidence"] is None:
            raise ValueError("decided verdicts need confidence")


def metrics(cases: list[dict], bins: int) -> dict:
    scored = [case for case in cases if case["score"] is not None]
    decided = [case for case in cases if case["status"] in STATUSES[:2]]
    return {
        "n_cases": len(cases),
        "positives": sum(case["label"] for case in cases),
        "outcomes": dict(Counter(case["status"] for case in cases)),
        "coordination_score": reliability(
            [case["score"] for case in scored], [case["label"] for case in scored], bins
        ),
        "verdict_confidence": reliability(
            [case["confidence"] for case in decided],
            [int((case["status"] == "coordinated") == bool(case["label"])) for case in decided],
            bins,
        ),
    }


def build_run(
    cases: list[dict], *, version: str, split: str, bins: int = 10, provenance: dict | None = None
) -> dict:
    validate_cases(cases, version, split)
    summary = metrics(cases, bins)
    true_positive = sum(case["label"] == 1 and predicted(case) for case in cases)
    false_positive = sum(case["label"] == 0 and predicted(case) for case in cases)
    positives = sum(case["label"] for case in cases)
    negatives = len(cases) - positives
    predicted_count = true_positive + false_positive
    precision = true_positive / predicted_count if predicted_count else None
    recall = true_positive / positives if positives else None
    denominator = positives + predicted_count
    details = {
        "schema_version": 1,
        "detector_version": version,
        "eval_commit": _git_sha(),
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "method": "equal_mass",
        "unit": "labelled case; most-overlapping cluster for positives, "
        "highest-score attributed cluster for negatives; "
        "absent cluster excluded from reliability",
        "score_target": "coordination label (raw score is not a calibrated probability)",
        "confidence_target": "correctness of a decided verdict (abstentions excluded)",
        "interval_limit": "Wilson intervals assume independent cases; "
        "shared windows are correlated",
        "mixture_limit": "Synthetic class mix is designed, not real-world prevalence; "
        "see by_dataset",
        "provenance": provenance or {},
        **summary,
        "by_dataset": {
            dataset: metrics([case for case in cases if case["dataset"] == dataset], bins)
            for dataset in sorted({case["dataset"] for case in cases})
        },
        "cases": cases,
    }
    return {
        "dataset": DATASET,
        "split": split,
        "n": len(cases),
        "precision": precision,
        "recall": recall,
        "f1": 2 * true_positive / denominator if denominator else None,
        "fpr": false_positive / negatives if negatives else None,
        "details": details,
    }


def predicted(case: dict) -> bool:
    return case.get("predicted_coordinated", case["status"] == "coordinated")


def snapshot(synthetic, clusters: list, window_start: float, seed: int, cfg=DEFAULT) -> dict:
    """One labelled injection; use the shared positive and organic attribution rules."""
    positives = synthetic.coordinated
    qualifying = []
    for cluster in clusters:
        overlap = len(set(cluster.members) & synthetic.dids)
        eligible = (
            overlap > 0 if positives else (overlap >= 2 and overlap >= 0.5 * len(cluster.members))
        )
        if eligible:
            qualifying.append(cluster)
    caught = (
        judge(synthetic, clusters, cfg).caught
        if positives
        else any(cluster.status == "coordinated" for cluster in qualifying)
    )
    qualifying.sort(
        key=lambda cluster: (
            len(set(cluster.members) & synthetic.dids) if positives else 0,
            cluster.status == "coordinated" if positives else False,
            cluster.score,
            sorted(cluster.members),
        ),
        reverse=True,
    )
    representative = next(iter(qualifying), None)
    kind = synthetic.name.split("_s")[0].removeprefix("campaign_").removeprefix("negative_")
    case_id = f"{int(window_start)}:{kind}:{seed}"
    digest = (
        hashlib.sha256("\n".join(sorted(representative.members)).encode()).hexdigest()[:16]
        if representative
        else None
    )
    cluster_snapshots = [
        {
            "cluster_id": f"offline:{case_id}:"
            + hashlib.sha256("\n".join(sorted(cluster.members)).encode()).hexdigest()[:16],
            "status": cluster.status,
            "score": cluster.score,
            "confidence": cluster.confidence,
            "families_fired": list(cluster.families_fired),
            "organic_guard_score": cluster.guard_score,
            "overlap": len(set(cluster.members) & synthetic.dids),
        }
        for cluster in qualifying
    ]
    return {
        "case_id": case_id,
        "dataset": "synthetic_campaigns" if positives else "organic_synth",
        "case_type": kind,
        "label": int(positives),
        "label_source": "synthetic",
        "window_start": window_start,
        "detector_version": cfg.version,
        "cluster_id": f"offline:{case_id}:{digest}" if representative else None,
        "cluster_id_kind": "offline (not a database primary key)",
        "status": representative.status if representative else "no_cluster",
        "predicted_coordinated": caught,
        "score": representative.score if representative else None,
        "confidence": representative.confidence if representative else None,
        "families_fired": representative.families_fired if representative else [],
        "organic_guard_score": representative.guard_score if representative else None,
        "cluster_ids": [cluster["cluster_id"] for cluster in cluster_snapshots],
        "clusters": cluster_snapshots,
    }


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--split", choices=("heldout", "dev"), default="heldout")
    parser.add_argument("--slice", type=Path, default=SLICE_ROOT)
    parser.add_argument("--bins", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-windows", type=int)
    parser.add_argument("--input", type=Path, help="recompute a saved calibration JSON, offline")
    parser.add_argument("--labels", type=Path, help="human-labelled case snapshots JSON array")
    parser.add_argument(
        "--labels-dir",
        type=Path,
        default=Path(os.environ.get("LABELS_DIR", "/srv/junction-espoo/data/labels")),
        help="read available agreed/adjudicated hand labels with eval.labels",
    )
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--database-url", help="opt-in persistence through eval.store")
    parser.add_argument("--write", action="store_true", help="use configured DATABASE_URL")


def main_args(args) -> int:
    publishing = args.write or bool(args.database_url)
    if args.bins < 1 or args.repeats < 1 or (args.max_windows is not None and args.max_windows < 1):
        raise ValueError("bins, repeats and max-windows must be positive")
    if publishing and (args.max_windows or args.input):
        raise ValueError("ad-hoc / imported runs cannot replace a published split")
    if args.input:
        previous = json.loads(args.input.read_text())
        if previous["split"] != args.split:
            raise ValueError("saved run is in the wrong split")
        details = previous["details"]
        cases, version = details["cases"], details["detector_version"]
        validate_cases(cases, version, args.split)
        provenance = {"imported_from": str(args.input), "adhoc": True}
    else:
        version = DEFAULT.version
        commit = _git_sha()
        human_cases, human_metadata = load_hand_labels(args.labels_dir, args.split, version)
        additional = json.loads(args.labels.read_text()) if args.labels else []
        if any(case["label_source"] != "human" for case in additional):
            raise ValueError("--labels requires human ground truth, not heuristic labels")
        human_cases.extend(additional)
        validate_cases(human_cases, version, args.split)
        hours = split_hours(args.slice, args.split)
        files = hour_files(args.slice, hours)
        if not files:
            raise ValueError("no recorded hours in requested split")
        cases, windows, skipped = [], [], []
        cache = MinHashCache(DEFAULT.minhash_perm)
        for info, window in iter_windows(files, 600, DEFAULT):
            if window is None:
                skipped.append({"window": info.label, "reason": info.skipped})
                continue
            windows.append(info.label)
            for kind in (*LEVELS, *NEGATIVES):
                for repeat in range(args.repeats):
                    seed = trial_seed(window.start, kind, repeat)
                    synthetic = (
                        campaign(kind, window, seed=seed)
                        if kind in LEVELS
                        else (hard_negative(kind, window, seed=seed))
                    )
                    result = detect(
                        inject(window, synthetic),
                        _synthetic_info(synthetic),
                        DEFAULT,
                        cache,
                    )
                    cases.append(snapshot(synthetic, result.clusters, window.start, seed))
            if args.max_windows and len(windows) >= args.max_windows:
                break
        if not windows:
            raise ValueError("no complete windows in requested split")
        provenance = {
            "hours_utc": hours,
            "windows": windows,
            "windows_skipped": skipped,
            "repeats_per_kind": args.repeats,
            "adhoc": bool(args.max_windows),
            "eval_commit_at_start": commit,
        }
    if not args.input:
        cases.extend(human_cases)
        provenance["hand_labels"] = human_metadata
    elif args.labels:
        labels = json.loads(args.labels.read_text())
        if any(case["label_source"] != "human" for case in labels):
            raise ValueError("--labels requires human ground truth, not heuristic labels")
        cases = [*cases, *labels]
    run = build_run(cases, version=version, split=args.split, bins=args.bins, provenance=provenance)
    if args.input:
        run["details"]["recomputed_at_commit"] = run["details"]["eval_commit"]
        run["details"]["eval_commit"] = details["eval_commit"]
        run["details"]["provenance"]["source_provenance"] = details["provenance"]
    else:
        run["details"]["eval_commit"] = provenance["eval_commit_at_start"]
    if publishing:
        run["id"] = write_eval_run(**run, database_url=args.database_url)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(run, indent=2) + "\n")
    print(json.dumps({key: value for key, value in run.items() if key != "details"}))
    print(
        json.dumps(
            {
                key: run["details"][key]
                for key in ("coordination_score", "verdict_confidence", "outcomes")
            }
        )
    )
    return 0
