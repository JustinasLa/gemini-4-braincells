"""Hourly Gemini request cap for the analyst worker (analyst/ratecap.py, analyst/__main__.py)."""

from contextlib import contextmanager

import pytest

import analyst.__main__ as cli
from analyst.ratecap import HourlyCap


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


def test_reserve_needs_room_for_the_worst_case():
    clock = Clock()
    cap = HourlyCap(10, clock=clock)
    cap.record(1)
    clock.t += 100
    cap.record(1)
    assert cap.allows(8)
    assert not cap.allows(9)
    # the oldest entry frees 1, enough for 9
    assert cap.seconds_until_free(9) == pytest.approx(3500)
    cap.record(5)
    # 7 used: for 9 we need used <= 1, i.e. the first two entries still leave and so must the 5
    assert cap.seconds_until_free(9) == pytest.approx(3600)


def test_zero_limit_means_no_cap():
    cap = HourlyCap(0)
    cap.record(10_000)
    assert cap.allows(50)
    assert cap.seconds_until_free() == 0


def test_seeded_spend_keeps_its_age():
    clock = Clock()
    cap = HourlyCap(4, clock=clock)
    cap.record(4, age_s=3500)
    assert not cap.allows()
    clock.t += 101
    assert cap.allows()


class FakeModels:
    def __init__(self):
        self.calls = 0

    def generate_content(self, **kw):
        self.calls += 1
        if kw.get("fail"):
            raise RuntimeError("429")
        return "ok"

    def count_tokens(self, **kw):
        return 7


class FakeClient:
    def __init__(self):
        self.models = FakeModels()
        self.other = "kept"


def test_wrapped_client_counts_every_request_even_failed_ones():
    cap = HourlyCap(100)
    raw = FakeClient()
    client = cap.wrap(raw)
    assert client.models.generate_content(model="m") == "ok"
    with pytest.raises(RuntimeError):
        client.models.generate_content(model="m", fail=True)
    assert cap.used() == 2 and raw.models.calls == 2
    assert client.models.count_tokens() == 7  # other methods pass through, uncounted
    assert client.other == "kept"
    assert cap.used() == 2


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class SeedConn:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""

    def execute(self, sql):
        self.sql = sql
        return FakeResult(self.rows)


def test_seed_records_rows_with_their_age():
    clock = Clock()
    cap = HourlyCap(10, clock=clock)
    conn = SeedConn([{"age_s": 3590.0, "calls": 6}, {"age_s": 60.0, "calls": 3}])
    assert cap.seed_from_db(conn) == 9
    assert "model = 'none' THEN 0" in conn.sql and "kind = 'analyst'" in conn.sql
    assert not cap.allows(2)
    clock.t += 11  # the 6 from 3590 s ago leave the window
    assert cap.used() == 3


class Stop(Exception):
    pass


class FakeConn:
    def rollback(self):
        pass


class FakeAnalyst(cli.Analyst):
    """Spends `per_cluster` requests through the cap per verdict, like the wrapped client."""

    def __init__(self, per_cluster=3, max_requests=4):
        self.per_cluster = per_cluster
        self._max = max_requests
        self.cap = None

    @property
    def max_requests(self):
        return self._max

    def count_requests(self, cap):
        self.cap = cap


def run_one_pass(monkeypatch, model, cap, queue, fail=()):
    ran = []

    @contextmanager
    def fake_connect():
        yield FakeConn()

    def fake_run_cluster(conn, m, cid):
        ran.append(cid)
        m.cap.record(m.per_cluster)
        if cid in fail:
            raise RuntimeError("agent gave up")

    def stop(_):
        raise Stop

    monkeypatch.setattr(cli, "connect", fake_connect)
    monkeypatch.setattr(cli.store, "clusters_needing_verdict", lambda conn, **kw: list(queue))
    monkeypatch.setattr(cli, "run_cluster", fake_run_cluster)
    monkeypatch.setattr(cli.time, "sleep", stop)
    with pytest.raises(Stop):
        cli.run_worker(
            model, interval_s=1, min_score=0.5, settle_s=0, refresh_s=0, batch=10, cap=cap
        )
    return ran


def test_worker_never_goes_over_the_cap(monkeypatch):
    cap = HourlyCap(10)
    monkeypatch.setattr(cap, "seed_from_db", lambda conn: 0)
    model = FakeAnalyst(per_cluster=3, max_requests=4)
    ran = run_one_pass(monkeypatch, model, cap, [1, 2, 3, 4])
    assert model.cap is cap
    assert ran == [1, 2, 3]  # 0+4, 3+4 and 6+4 fit; 9+4 > 10 stops the pass
    assert cap.used() == 9  # never above the limit


def test_failed_runs_count_what_they_spent(monkeypatch):
    cap = HourlyCap(10)
    monkeypatch.setattr(cap, "seed_from_db", lambda conn: 0)
    model = FakeAnalyst(per_cluster=4, max_requests=4)
    ran = run_one_pass(monkeypatch, model, cap, [7, 8, 9], fail={7, 8})
    assert ran == [7, 8]
    assert cap.used() == 8


def test_worker_skips_the_queue_when_seeded_full(monkeypatch):
    cap = HourlyCap(5)
    monkeypatch.setattr(cap, "seed_from_db", lambda conn: (cap.record(3), 3)[1])
    assert run_one_pass(monkeypatch, FakeAnalyst(max_requests=4), cap, [1, 2]) == []


def test_no_cap_does_not_wrap_the_model(monkeypatch):
    model = FakeAnalyst(per_cluster=0)
    model.cap = HourlyCap(0)  # fake_run_cluster records into it
    run_one_pass(monkeypatch, model, HourlyCap(0), [1, 2, 3])
    assert model.cap.limit == 0


def test_wrapping_twice_counts_once():
    cap = HourlyCap(100)
    raw = FakeClient()
    client = cap.wrap(cap.wrap(raw))
    client.models.generate_content(model="m")
    assert cap.used() == 1


def test_limit_below_the_reserve_still_runs(monkeypatch):
    cap = HourlyCap(3)
    monkeypatch.setattr(cap, "seed_from_db", lambda conn: 0)
    ran = run_one_pass(monkeypatch, FakeAnalyst(per_cluster=1, max_requests=9), cap, [1, 2, 3])
    assert ran == [1]  # reserve is clamped to the limit (3), so one verdict fits


class PlainModel:
    """A non-Analyst model with its own counting hook (like GeminiVerdictModel)."""

    per_cluster = 1

    def __init__(self):
        self.cap = None

    def count_requests(self, cap):
        self.cap = cap


def test_plain_model_is_counted_too(monkeypatch):
    cap = HourlyCap(2)
    monkeypatch.setattr(cap, "seed_from_db", lambda conn: 0)
    model = PlainModel()
    ran = run_one_pass(monkeypatch, model, cap, [1, 2, 3])
    assert model.cap is cap
    assert ran == [1, 2]  # reserve 1 each; the third doesn't fit
