"""Hourly Gemini request cap for the analyst worker (analyst/ratecap.py, analyst/__main__.py)."""

from contextlib import contextmanager

import pytest

import analyst.__main__ as cli
from analyst.ratecap import HourlyCap, calls_in
from analyst.verdict import Verdict


class Clock:
    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t


def test_cap_blocks_at_limit_and_frees_after_an_hour():
    clock = Clock()
    cap = HourlyCap(5, clock=clock)
    cap.record(3)
    clock.t += 1800
    cap.record(2)
    assert not cap.allows()
    assert cap.seconds_until_free() == pytest.approx(1800)
    clock.t += 1800  # the first 3 leave the window
    assert cap.used() == 2
    assert cap.allows()


def test_zero_limit_means_no_cap():
    cap = HourlyCap(0)
    cap.record(10_000)
    assert cap.allows()
    assert cap.seconds_until_free() == 0


def test_seeded_spend_keeps_its_age():
    clock = Clock()
    cap = HourlyCap(4, clock=clock)
    cap.record(4, age_s=3500)
    assert not cap.allows()
    clock.t += 101
    assert cap.allows()


def test_calls_in_counts_turns_and_at_least_one():
    assert calls_in(None) == 1
    v = Verdict.model_construct(model_calls=[])
    assert calls_in(v) == 1
    v = Verdict.model_construct(model_calls=[{}, {}, {}])
    assert calls_in(v) == 3


class Stop(Exception):
    pass


class FakeConn:
    def rollback(self):
        pass


def run_one_pass(monkeypatch, cap, queue, calls_per_verdict=2, fail=()):
    ran = []

    @contextmanager
    def fake_connect():
        yield FakeConn()

    def fake_run_cluster(conn, model, cid):
        ran.append(cid)
        if cid in fail:
            raise RuntimeError("model error")
        return Verdict.model_construct(model_calls=[{}] * calls_per_verdict)

    def stop(_):
        raise Stop

    monkeypatch.setattr(cli, "connect", fake_connect)
    monkeypatch.setattr(cli.store, "clusters_needing_verdict", lambda conn, **kw: list(queue))
    monkeypatch.setattr(cli, "run_cluster", fake_run_cluster)
    monkeypatch.setattr(cli.time, "sleep", stop)
    with pytest.raises(Stop):
        cli.run_worker(
            object(), interval_s=1, min_score=0.5, settle_s=0, refresh_s=0, batch=10, cap=cap
        )
    return ran


def test_worker_stops_the_pass_when_the_cap_is_full(monkeypatch):
    cap = HourlyCap(5)
    monkeypatch.setattr(cap, "seed_from_db", lambda conn: 0)
    ran = run_one_pass(monkeypatch, cap, [1, 2, 3, 4], calls_per_verdict=2)
    assert ran == [1, 2, 3]  # 2+2 < 5, so cluster 3 runs; then 6 >= 5 stops the pass
    assert cap.used() == 6


def test_worker_counts_failed_runs_and_seeds_from_db(monkeypatch):
    cap = HourlyCap(3)
    monkeypatch.setattr(cap, "seed_from_db", lambda conn: (cap.record(2), 2)[1])
    ran = run_one_pass(monkeypatch, cap, [7, 8], fail={7})
    assert ran == [7]  # seeded 2 + failed run 1 = 3: cap full
    assert cap.used() == 3


def test_worker_skips_the_queue_when_already_capped(monkeypatch):
    cap = HourlyCap(1)
    monkeypatch.setattr(cap, "seed_from_db", lambda conn: (cap.record(1), 1)[1])
    assert run_one_pass(monkeypatch, cap, [1, 2]) == []
