"""eval/takedowns.py: the pure table logic (no DB)."""

import json
from datetime import UTC, datetime, timedelta

from eval.takedowns import Flag, table, wilson

T0 = datetime(2026, 11, 14, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=10)


def test_wilson_bounds():
    lo, hi = wilson(0, 600)
    assert lo == 0.0 and 0.005 < hi < 0.007
    lo, hi = wilson(33, 229)
    assert lo < 33 / 229 < hi


def test_only_outcomes_inside_the_horizon_count():
    flags = [
        Flag("a", "coordinated", T0),
        Flag("b", "coordinated", T0),
        Flag("c", "coordinated", T0),
    ]
    events = {
        "a": [(T0 + timedelta(hours=1), "takendown")],  # inside 3 h -> counts
        "b": [(T0 + timedelta(hours=5), "takendown")],  # after the horizon -> doesn't
        "c": [(T0 + timedelta(hours=2), "deactivated")],  # voluntary -> not an outcome
    }
    row = table(flags, events, NOW, horizon_h=3)["coordinated"]
    assert row["accounts"] == 3
    assert row["moderation"]["k"] == 1


def test_recent_flags_and_already_actioned_accounts_are_excluded():
    flags = [
        Flag("recent", "coordinated", NOW - timedelta(hours=1)),
        Flag("prior", "coordinated", T0),
        Flag("ok", "coordinated", T0),
    ]
    events = {
        "prior": [(T0 - timedelta(minutes=5), "suspended")],
        "ok": [(T0 + timedelta(minutes=30), "suspended")],
    }
    row = table(flags, events, NOW, horizon_h=3)["coordinated"]
    assert row["excluded_too_recent"] == 1
    assert row["excluded_already_actioned"] == 1
    assert row["accounts"] == 1 and row["moderation"]["k"] == 1


def test_deleted_and_throttled_are_reported_separately_from_moderation():
    flags = [Flag("d", "baseline", T0), Flag("t", "baseline", T0)]
    events = {
        "d": [(T0 + timedelta(hours=1), "deleted")],
        "t": [(T0 + timedelta(hours=1), "throttled")],
    }
    row = table(flags, events, NOW, horizon_h=3)["baseline"]
    assert row["moderation"]["k"] == 0
    assert row["deleted"]["k"] == 1 and row["throttled"]["k"] == 1


def test_p_only_abstentions_get_their_own_row():
    flags = [Flag("a", "insufficient", T0), Flag("a", "insufficient_p_only", T0)]
    events = {"a": [(T0 + timedelta(hours=1), "takendown")]}
    t = table(flags, events, NOW, horizon_h=3)
    assert t["insufficient"]["moderation"]["k"] == 1
    assert t["insufficient_p_only"]["accounts"] == 1
    assert t["insufficient_p_only"]["moderation"]["k"] == 1


def test_write_goes_through_the_shared_eval_runs_writer():
    from types import SimpleNamespace

    from eval.takedowns import DATASET, write

    calls = []

    class Conn:
        def execute(self, sql, args):
            calls.append((sql, args))
            return SimpleNamespace(fetchone=lambda: (7,))

    result = {"groups": {"coordinated": {"accounts": 229}}, "horizon_h": 3.0}
    assert write(Conn(), result) == 7
    sql, args = calls[0]
    assert "INSERT INTO eval_runs" in sql and "::jsonb" in sql
    assert args == (DATASET, "live", None, None, None, None, 229, json.dumps(result))
