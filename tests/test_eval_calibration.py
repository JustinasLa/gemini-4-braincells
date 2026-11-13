"""Pure reliability, frozen error extraction and SQL provenance contracts, offline."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from eval.__main__ import main
from eval.calibration import build_run, reliability, snapshot, validate_cases
from eval.errors import extract_errors, latest_run, markdown_report
from eval.provenance import audit_versions
from eval.store import write_eval_run
from signals.config import DEFAULT
from signals.score import ClusterScore
from signals.store import _store_one
from signals.synth import Synthetic

START = datetime(2026, 11, 13, 19, 50, tzinfo=UTC).timestamp()


def case(identifier="one", label=1, status="coordinated", score=0.8, **changes):
    value = {
        "case_id": identifier,
        "dataset": "synthetic_campaigns" if label else "organic_synth",
        "case_type": "L3" if label else "news_link",
        "label": label,
        "label_source": "synthetic",
        "detector_version": DEFAULT.version,
        "window_start": START,
        "cluster_id": f"offline:{identifier}",
        "status": status,
        "score": score,
        "confidence": 0.7,
        "families_fired": ["content", "timing"],
        "organic_guard_score": 0.2,
    }
    value.update(changes)
    return value


def run(cases):
    return build_run(cases, version=DEFAULT.version, split="heldout", bins=2)


def test_equal_mass_counts_and_wilson_boundaries():
    result = reliability([1, 0, 0.8, 0.2, 0.6, 0.4, 0.5], [1, 0, 1, 0, 1, 0, 1], 3)
    assert [group["n"] for group in result["bins"]] == [2, 2, 3]
    assert sum(group["positives"] for group in result["bins"]) == 4
    assert result["bins"][0]["wilson95"] == [0.0, 0.6576]
    assert result["bins"][-1]["wilson95"] == [0.4385, 1.0]
    assert result["bins"][0]["min_prediction"] == 0
    assert result["bins"][-1]["max_prediction"] == 1


def test_brier_and_ece_known_values():
    result = reliability([0.1, 0.4, 0.8, 0.9], [0, 1, 1, 0], 2)
    assert result["brier"] == pytest.approx(0.305)
    assert result["ece"] == pytest.approx(0.3)
    assert reliability([0, 1], [0, 1])["brier"] == 0
    assert reliability([0, 1], [0, 1])["ece"] == 0


def test_empty_singleton_and_tied_predictions():
    assert reliability([], [])["brier"] is None
    assert reliability([], [])["bins"] == []
    assert reliability([0.5], [1], 10)["bins"][0]["n"] == 1
    result = reliability([0.5] * 5, [1, 0, 1, 0, 1], 3)
    assert [group["n"] for group in result["bins"]] == [1, 2, 2]
    assert result["brier"] == 0.25


@pytest.mark.parametrize(
    ("probabilities", "targets", "bins"),
    [
        ([float("nan")], [1], 1),
        ([float("inf")], [1], 1),
        ([1.1], [1], 1),
        ([-0.1], [1], 1),
        ([0.5], [2], 1),
        ([0.5], [True], 1),
        ([0.5], [], 1),
        ([], [], 0),
    ],
)
def test_invalid_metric_inputs(probabilities, targets, bins):
    with pytest.raises(ValueError):
        reliability(probabilities, targets, bins)


def test_confidence_means_correct_verdict_not_coordination_probability():
    result = run(
        [
            case("organic", 0, "likely_organic", 0.2, confidence=0.9),
            case("abstain", 1, "insufficient", 0.7, confidence=0.0),
            case("absent", 1, "no_cluster", None, cluster_id=None, confidence=None),
        ]
    )
    assert result["n"] == 3
    assert result["recall"] == 0
    assert result["details"]["coordination_score"]["n"] == 2
    confidence = result["details"]["verdict_confidence"]
    assert confidence["n"] == 1
    assert confidence["brier"] == pytest.approx(0.01)
    assert confidence["ece"] == pytest.approx(0.1)
    assert set(result["details"]["by_dataset"]) == {"organic_synth", "synthetic_campaigns"}


def test_fp_fn_extraction_and_categories():
    measured = run(
        [
            case("tp"),
            case("tn", 0, "likely_organic"),
            case("fp", 0),
            case("fn", 1, "insufficient"),
            case("miss", 1, "no_cluster", None, cluster_id=None, confidence=None),
        ]
    )
    errors = extract_errors(measured)
    assert [(value["case_id"], value["error"]) for value in errors] == [
        ("fp", "FP"),
        ("fn", "FN"),
        ("miss", "FN"),
    ]
    assert errors[0]["reason"] == "news burst"
    assert errors[1]["reason"] == "slow campaign missed"
    assert errors[0]["families_fired"] == ["content", "timing"]
    assert errors[0]["organic_guard_score"] == 0.2
    report = markdown_report(measured)
    assert "false positives: 1; false negatives: 2" in report
    assert "none (not grouped)" in report and DEFAULT.version in report


@pytest.mark.parametrize(
    "changes",
    [
        {"detector_version": "different"},
        {"label_source": "heuristic"},
        {"window_start": START + 3600},
        {"score": float("nan")},
        {"label": True},
        {"label": 2},
        {"cluster_id": None},
        {"confidence": None},
    ],
)
def test_reject_leakage_mixed_versions_and_bad_labels(changes):
    with pytest.raises(ValueError):
        validate_cases([case(**changes)], DEFAULT.version, "heldout")


def test_duplicates_and_missing_version_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        run([case(), case()])
    with pytest.raises(ValueError, match="detector_version"):
        validate_cases([], "", "heldout")


def cluster(members, status="coordinated", score=0.8):
    return ClusterScore(
        members=members,
        signals=[],
        family_scores={},
        families_fired=["content", "timing"],
        score=score,
        confidence=0.6,
        guard_score=0.2,
        p_value=0.01,
        p_detail={},
        status=status,
        core=members,
        post_uris=[],
    )


def test_positive_union_hit_rule_and_offline_cluster_ids():
    synthetic = Synthetic(
        "campaign_L2_s1", True, accounts={str(index): None for index in range(10)}
    )
    clusters = [cluster(["0", "1", "2"]), cluster(["3", "4", "5"])]
    value = snapshot(synthetic, clusters, START, 1)
    assert value["predicted_coordinated"]
    assert len(value["cluster_ids"]) == 2
    assert value["cluster_id"].startswith("offline:")
    assert snapshot(synthetic, list(reversed(clusters)), START, 1)["cluster_ids"]
    assert run([value])["recall"] == 1


def test_partial_positive_cluster_is_still_a_false_negative():
    synthetic = Synthetic(
        "campaign_L2_s1", True, accounts={str(index): None for index in range(10)}
    )
    value = snapshot(synthetic, [cluster(["0", "1"])], START, 1)
    assert value["status"] == "coordinated" and not value["predicted_coordinated"]
    assert extract_errors(run([value]))[0]["error"] == "FN"


def test_organic_case_requires_majority_of_cluster():
    synthetic = Synthetic("negative_news_link_s1", False, accounts={"a": None, "b": None})
    value = snapshot(synthetic, [cluster(["a", "b", "c", "d", "e"])], START, 1)
    assert value["status"] == "no_cluster" and value["score"] is None
    assert not extract_errors(run([value]))


def test_cli_recomputes_and_reports_offline(tmp_path, capsys):
    source = tmp_path / "run.json"
    source.write_text(json.dumps(run([case("fp", 0)])))
    report = tmp_path / "report.md"
    assert main(["calibration", "--input", str(source)]) == 0
    assert main(["errors", "--input", str(source), "--output", str(report)]) == 0
    assert "news burst" in report.read_text()
    with pytest.raises(ValueError, match="ad-hoc"):
        main(["calibration", "--input", str(source), "--write"])
    with pytest.raises(ValueError, match="ad-hoc"):
        main(["calibration", "--max-windows", "1", "--write"])


def test_writer_records_measured_detector_version():
    measured = run([case()])
    captured = []
    conn = SimpleNamespace(
        execute=lambda sql, args: (
            captured.append((sql, args)) or SimpleNamespace(fetchone=lambda: (17,))
        )
    )
    assert write_eval_run(**measured, conn=conn) == 17
    assert json.loads(captured[0][1][-1])["detector_version"] == DEFAULT.version
    assert json.loads(captured[0][1][-1])["cases"][0]["detector_version"] == DEFAULT.version


@pytest.mark.parametrize("existing", [False, True])
def test_cluster_insert_and_update_always_write_version(existing):
    statements = []

    class Connection:
        def execute(self, sql, args):
            statements.append((sql, args))
            rows = []
            if "SELECT c.id" in sql and existing:
                rows = [(7, 8, ["a", "b"], "10m", 0.8)]
            return (
                SimpleNamespace(
                    fetchall=lambda: rows, fetchone=lambda: (7,), __iter__=lambda self: iter([])
                )
                if "SELECT id, detail" not in sql
                else []
            )

        def cursor(self):
            return SimpleNamespace(executemany=lambda *args: None)

    start = datetime.fromtimestamp(START, UTC)
    assert (
        _store_one(
            Connection(),
            cluster(["a", "b"]),
            "10m",
            start,
            start + timedelta(seconds=600),
            DEFAULT,
            True,
        )
        == 7
    )
    writes = [
        (sql, args)
        for sql, args in statements
        if ("INSERT INTO clusters" in sql or "UPDATE clusters SET window_start" in sql)
    ]
    assert len(writes) == 1
    assert "detector_version" in writes[0][0] and DEFAULT.version in writes[0][1]
    payloads = [args for sql, args in statements if "SET signals" in sql]
    assert payloads and json.loads(payloads[0][0])["detector_version"] == DEFAULT.version


def test_latest_run_is_filtered_ordered_and_version_audit_is_readonly():
    queries = []

    def execute(sql, args=()):
        queries.append((sql, args))
        row = (
            (2, "calibration", "heldout", 1, {})
            if "ORDER BY" in sql
            else ((10, 0, 0) if "FROM clusters" in sql else (3, 0))
        )
        return SimpleNamespace(fetchone=lambda: row)

    conn = SimpleNamespace(execute=execute)
    assert latest_run(conn)["id"] == 2
    assert queries[0][1] == ("calibration", "heldout")
    assert "created_at DESC, id DESC" in queries[0][0]
    assert audit_versions(conn) == {
        "clusters": 10,
        "clusters_missing_version": 0,
        "clusters_signal_version_mismatch": 0,
        "eval_runs": 3,
        "eval_runs_missing_version": 0,
    }
    assert all(sql.startswith("SELECT") for sql, _ in queries)
