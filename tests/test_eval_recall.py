"""Eval scoring and slice loading. Offline: no DB, no network, no recorded files."""

import gzip
import json
import os

import pytest

from eval.recall import judge, run_trial, summarize, trial_seed, wilson
from eval.slices import file_hour, hour_files, iter_windows, split_hours, split_of_hour
from eval.store import write_eval_run
from signals.score import ClusterScore
from signals.synth import Synthetic, empty_window


def _synth(n=10) -> Synthetic:
    s = Synthetic(name="campaign_L2_s1", coordinated=True)
    s.accounts = {f"did:plc:synthl2{i:02d}": None for i in range(n)}
    return s


def _cluster(members, status="coordinated", score=0.9, fam=0.9) -> ClusterScore:
    return ClusterScore(
        members=list(members),
        signals=[],
        family_scores={"content": fam, "timing": 0.0, "network": 0.0, "identity": 0.0},
        families_fired=["content"] if fam >= 0.5 else [],
        score=score,
        p_value=0.002,
        p_detail={},
        guard_score=0.0,
        status=status,
        confidence=0.5,
        post_uris=[],
        core=[],
    )


# ----------------------------------------------------------------- judge ---------------------
def test_caught_when_majority_in_coordinated_cluster():
    s = _synth()
    dids = sorted(s.dids)
    t = judge(s, [_cluster(dids[:6] + ["did:plc:real1", "did:plc:real2"])])
    assert t.caught and t.surfaced
    assert t.coverage == 0.6
    assert t.level == "L2" and t.best_status == "coordinated" and t.best_size == 8
    assert t.best_purity == 0.75


def test_not_caught_below_coverage():
    s = _synth()
    t = judge(s, [_cluster(sorted(s.dids)[:4])])
    assert not t.caught and t.coverage == 0.4


def test_coverage_is_union_over_coordinated_clusters():
    s = _synth()
    d = sorted(s.dids)
    t = judge(s, [_cluster(d[:3]), _cluster(d[3:6]), _cluster(d[6:], status="insufficient")])
    assert t.caught and t.coverage == 0.6


def test_abstention_is_not_a_catch_but_is_surfaced():
    s = _synth()
    t = judge(s, [_cluster(sorted(s.dids), status="insufficient", fam=0.8)])
    assert not t.caught and t.surfaced and t.best_status == "insufficient"


def test_quiet_insufficient_cluster_is_not_surfaced():
    s = _synth()
    t = judge(s, [_cluster(sorted(s.dids), status="insufficient", score=0.2, fam=0.2)])
    assert not t.caught and not t.surfaced


def test_missed_entirely():
    s = _synth()
    t = judge(s, [_cluster(["did:plc:a", "did:plc:b", "did:plc:c", "did:plc:d"])])
    assert not t.caught and not t.surfaced and t.best_status is None and t.coverage == 0.0


# ----------------------------------------------------------------- stats ---------------------
def test_wilson():
    assert wilson(0, 0) is None
    lo, hi = wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)
    assert wilson(0, 10)[0] == 0.0 and wilson(10, 10)[1] == 1.0


def test_summarize_per_level():
    s = _synth()
    d = sorted(s.dids)
    caught = judge(s, [_cluster(d)])
    missed = judge(s, [])
    other = judge(Synthetic(name="campaign_L1_s2", coordinated=True, accounts=s.accounts), [])
    out = summarize([caught, missed, other])
    assert out["overall"]["k"] == 1 and out["overall"]["n"] == 3
    assert out["per_level"]["L2"]["recall"]["rate"] == 0.5
    assert out["per_level"]["L1"]["recall"]["rate"] == 0.0
    assert out["per_level"]["L2"]["best_cluster_status"] == {"coordinated": 1, "none": 1}


def test_trial_seed_is_stable_and_distinct():
    assert trial_seed(1800000000, "L1", 0) == trial_seed(1800000000, "L1", 0)
    seeds = {
        trial_seed(1800000000 + 600 * w, lv, k) for w in range(5) for lv in "AB" for k in [0, 1]
    }
    assert len(seeds) == 20


def test_run_trial_l1_on_synthetic_background_is_caught():
    wd = empty_window(seed=3)
    t = run_trial(wd, "L1", 0, label="bg")
    assert t.caught and t.n_accounts == 12 and t.best_purity == 1.0


# ----------------------------------------------------------------- slices --------------------
T0 = 1_800_000_000  # a multiple of 600: windows start at T0, T0+600, ...


def _post(did, rkey, t, text="hello there this is a post"):
    return {
        "did": did,
        "time_us": int(t * 1e6),
        "kind": "commit",
        "commit": {
            "operation": "create",
            "collection": "app.bsky.feed.post",
            "rkey": rkey,
            "record": {"text": text, "createdAt": "2027-01-15T08:00:00Z", "langs": ["en"]},
        },
    }


def _like(did, rkey, t, subject):
    return {
        "did": did,
        "time_us": int(t * 1e6),
        "kind": "commit",
        "commit": {
            "operation": "create",
            "collection": "app.bsky.feed.like",
            "rkey": rkey,
            "record": {"subject": {"uri": subject, "cid": "x"}, "createdAt": "2027"},
        },
    }


def _write(root, events, hour="2027011508", name="00-1"):
    p = root / hour[:8] / f"{hour[8:]}-{name}.jsonl.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return p


def _ticks(start, end, step=20.0):
    """A heartbeat of posts so no window has a recording hole."""
    t, i = start, 0
    while t < end:
        yield _post(f"did:plc:tick{i % 7}", f"tick{i}", t, text=f"tick {i}")
        t += step
        i += 1


def test_iter_windows_matches_load_window_semantics(tmp_path):
    shared = "at://did:plc:author/app.bsky.feed.post/shared"
    lonely = "at://did:plc:author/app.bsky.feed.post/lonely"
    ev = list(_ticks(T0 + 5, T0 + 1800))
    ev += [_like(f"did:plc:liker{i}", f"l{i}", T0 + 650 + i, shared) for i in range(3)]
    ev += [_like("did:plc:liker9", "l9", T0 + 700, lonely)]
    ev += [_post("did:plc:dup", "same", T0 + 660), _post("did:plc:dup", "same", T0 + 661)]
    ev.sort(key=lambda e: e["time_us"])
    _write(tmp_path, ev)
    out = list(iter_windows(hour_files(tmp_path, ["2027011508"]), 600.0))
    assert [(i.start - T0, i.skipped) for i, _ in out] == [
        (0, None),
        (600, None),
        (1200, None),
    ]
    wd = out[1][1]
    # likes of a subject with >= 2 distinct accounts are kept; a subject liked once is not
    assert {a.subject for a in wd.actions} == {shared} and len(wd.actions) == 3
    # ...but every event counts in the background (null model)
    assert wd.background["like"].sum() == 4
    # duplicate events (failover hosts) are kept once
    assert sum(p.uri.endswith("/same") for p in wd.posts) == 1
    assert wd.background["post"].sum() == len(wd.posts)
    assert wd.active_accounts == len({p.did for p in wd.posts})
    assert all(wd.start <= p.t < wd.end for p in wd.posts)


def test_iter_windows_skips_partial_and_holes(tmp_path):
    ev = list(_ticks(T0 + 300, T0 + 900)) + list(_ticks(T0 + 1200, T0 + 1400))
    ev += list(_ticks(T0 + 1500, T0 + 1800))
    _write(tmp_path, ev)
    out = {i.start - T0: i.skipped for i, _ in iter_windows(hour_files(tmp_path, ["2027011508"]))}
    assert out[0] == "recording starts inside this window"
    assert out[600] == "recording ends inside this window"
    assert out[1200].startswith("hole of")


def test_split_rule_odd_hours_are_heldout(tmp_path):
    assert split_of_hour("2026111321") == "heldout" and split_of_hour("2026111320") == "dev"
    _write(tmp_path, [], hour="2027011508")
    _write(tmp_path, [], hour="2027011509", name="00-2")
    _write(tmp_path, [], hour="2027011510", name="00-3")
    assert split_hours(tmp_path, "dev") == ["2027011508", "2027011510"]
    assert split_hours(tmp_path, "heldout") == ["2027011509"]


def test_hour_files_select_by_name(tmp_path):
    a = _write(tmp_path, [], hour="2027011508")
    b = _write(tmp_path, [], hour="2027011509", name="00-2")
    assert file_hour(a) == "2027011508"
    assert hour_files(tmp_path, ["2027011509"]) == [b]


@pytest.mark.skipif(
    not os.environ.get("EVAL_TEST_DATABASE_URL"), reason="set EVAL_TEST_DATABASE_URL (scratch DB)"
)
def test_store_roundtrip():
    import psycopg

    url = os.environ["EVAL_TEST_DATABASE_URL"]
    rid = write_eval_run(
        "synthetic_campaigns", "dev", recall=0.5, n=4, details={"x": 1}, database_url=url
    )
    with psycopg.connect(url) as conn:
        row = conn.execute(
            "SELECT dataset, split, precision, recall, fpr, n, details FROM eval_runs"
            " WHERE id = %s",
            (rid,),
        ).fetchone()
    assert row == ("synthetic_campaigns", "dev", None, 0.5, None, 4, {"x": 1})


def test_iter_windows_gap_check_tolerates_reordered_events(tmp_path):
    ev = list(_ticks(T0, T0 + 1800))
    # deliver the event at +100 s right after the first one (within the reorder allowance)
    ev.insert(1, ev.pop(5))
    _write(tmp_path, ev)
    out = list(iter_windows(hour_files(tmp_path, ["2027011508"])))
    assert [i.skipped for i, _ in out] == [None, None, None]
    assert out[0][0].max_gap_s == 20.0


def test_adhoc_hours_cannot_be_published(tmp_path, capsys):
    from eval.__main__ import main

    db = ["--split", "heldout", "--slice", str(tmp_path), "--database-url", "postgresql://unused"]
    adhoc = [
        ["--hours", "2026111320"],
        ["--max-windows", "1"],
        ["--levels", "L1"],
        ["--window-s", "3600"],
    ]
    for extra in adhoc:
        assert main(["recall", *db, *extra]) == 2
        assert "ad-hoc" in capsys.readouterr().err, extra


def test_adhoc_run_table_is_labelled(tmp_path, capsys):
    from eval.__main__ import main

    _write(tmp_path, list(_ticks(T0, T0 + 1200)))
    argv = ["recall", "--split", "dev", "--slice", str(tmp_path), "--hours", "2027011508"]
    assert main([*argv, "--levels", "L1", "--repeats", "1", "--workers", "1"]) == 0
    out = capsys.readouterr().out
    assert "recall on synthetic campaigns (dev, AD-HOC run)" in out
    assert "2 windows of 600s" in out


def test_consecutive_runs_do_not_share_windows(tmp_path, capsys):
    from eval.__main__ import main

    _write(tmp_path, list(_ticks(T0, T0 + 600)), hour="2027011508")  # dev (even hour)
    _write(tmp_path, list(_ticks(T0 + 3600, T0 + 4200)), hour="2027011509", name="00-2")
    common = ["--slice", str(tmp_path), "--levels", "L1", "--repeats", "1", "--workers", "1"]
    assert main(["recall", "--split", "dev", *common]) == 0
    capsys.readouterr()
    assert main(["recall", "--split", "heldout", *common]) == 0
    out = capsys.readouterr().out
    assert "1 windows of 600s from hours 2027011509" in out and "1/1" in out


def test_load_base_rates_both_shapes(tmp_path):
    from eval.recall import load_base_rates

    rates = {
        "sample_with_age": 400,
        "fresh_rate": 0.0125,
        "sample_with_profile": 399,
        "empty_rate": 0.0,
    }
    flat, wrapped = tmp_path / "flat.json", tmp_path / "wrapped.json"
    flat.write_text(json.dumps(rates))
    wrapped.write_text(json.dumps({"detector_version": "v1.5", "base_rates": rates}))
    for p in (flat, wrapped):
        r = load_base_rates(str(p))
        assert (r.n_age, r.fresh, r.n_profile, r.empty) == (400, 0.0125, 399, 0.0)
    assert load_base_rates("signals/fixtures/attack_results_live_v15_20261113T2000Z.json").n_age


def test_base_rates_reach_the_detector_and_details(tmp_path, capsys):
    from eval.__main__ import main

    _write(tmp_path, list(_ticks(T0, T0 + 600)))
    rates = tmp_path / "rates.json"
    rates.write_text(
        json.dumps(
            {
                "sample_with_age": 400,
                "fresh_rate": 0.0125,
                "sample_with_profile": 399,
                "empty_rate": 0.0,
            }
        )
    )
    out = tmp_path / "out.json"
    argv = ["recall", "--split", "dev", "--slice", str(tmp_path), "--repeats", "1"]
    assert main([*argv, "--workers", "1", "--base-rates", str(rates), "--json-out", str(out)]) == 0
    det = json.loads(out.read_text())["details"]
    assert det["base_rates"]["fresh_rate"] == 0.0125 and det["base_rates"]["source"] == str(rates)
