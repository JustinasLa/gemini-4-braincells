"""Adapt the hand-label owner's frozen samples without copying their adjudication logic."""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from eval import labels as hand_labels


def load_hand_labels(root: Path, split: str, version: str) -> tuple[list[dict], dict]:
    samples = sorted((root / "samples").glob("*.json"))
    ready = [path for path in samples if (root / f"{path.stem}.labels.jsonl").exists()]
    metadata = {"samples": len(samples), "with_label_records": len(ready), "included": 0}
    if not ready:
        return [], metadata
    cases, excluded, seen = [], Counter(), set()
    for path in ready:
        sample = json.loads(path.read_text())
        labels, adjudications = hand_labels.load_records(root / f"{path.stem}.labels.jsonl")
        pair = hand_labels.pick_pair(labels)  # same labelling pair as eval.labels.score
        finals = hand_labels.final_labels(
            [item["item"] for item in sample["items"]],
            {name: labels[name] for name in pair} if pair else {},
            adjudications,
        )
        for item in sample["items"]:
            detector = item["detector"]
            label = finals[item["item"]]["label"]
            if item["split"] != split:
                excluded["other_split"] += 1
                continue
            if detector["detector_version"] != version:
                excluded["other_version"] += 1
                continue
            if label not in ("coordinated", "organic"):
                excluded["unresolved_or_cant_tell"] += 1
                continue
            cluster_id = detector["cluster_id"]
            if cluster_id in seen:
                raise ValueError("cluster occurs in multiple labelled samples; select one snapshot")
            seen.add(cluster_id)
            signals = detector.get("signals", {})
            families = signals.get("families_fired", detector.get("families_fired", []))
            if not isinstance(families, list):
                families = []
            window_start = datetime.fromisoformat(detector["window_start"])
            if window_start.tzinfo is None:
                raise ValueError("hand-label window_start must include timezone")
            cases.append(
                {
                    "case_id": f"{sample['name']}:{item['item']}",
                    "dataset": "live_hand_labels",
                    "case_type": "human_reviewed",
                    "label": int(label == "coordinated"),
                    "label_source": "human",
                    "detector_version": version,
                    "window_start": window_start.timestamp(),
                    "cluster_id": cluster_id,
                    "cluster_ids": [cluster_id],
                    "cluster_id_kind": "database primary key",
                    "status": detector["status"],
                    "score": detector["score"],
                    "confidence": detector["confidence"],
                    "families_fired": families,
                    "organic_guard_score": signals.get("organic_guard_score"),
                    "signals_available": bool(signals),
                    "sample_weight": item["weight"],
                    "sampling_limit": "unweighted reliability of labelled stratified sample; "
                    "not population",
                }
            )
    return cases, {**metadata, "included": len(cases), "excluded": dict(excluded)}
