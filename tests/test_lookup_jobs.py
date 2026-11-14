"""Job expiry must not hide active work from polling or concurrency limits."""

import threading

import pytest

from lookup.jobs import JobStore, RateLimited
from lookup.models import LookupInput
from lookup.service import Deps, Lookup

TEXT = "A lookup that is still collecting its evidence"


@pytest.fixture
def running_job(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    workers = []

    def run(job):
        workers.append(threading.current_thread())
        entered.set()
        release.wait(5)
        with job.lock:
            job.result.state = "done"

    monkeypatch.setattr(Lookup, "run", run)
    now = [0.0]
    store = JobStore(
        lambda: Deps(oxylabs=None),
        cache_ttl_s=10,
        job_ttl_s=20,
        max_running=1,
        clock=lambda: now[0],
    )
    job = store.start(LookupInput(text=TEXT), "client")
    assert entered.wait(1)
    try:
        yield store, now, job, release, workers
    finally:
        release.set()
        for worker in workers:
            worker.join(1)
            assert not worker.is_alive()


@pytest.mark.parametrize("age", [11, 21])
def test_running_job_is_reused_after_cache_or_job_ttl(running_job, age):
    store, now, job, _, _ = running_job
    now[0] = age
    assert store.start(LookupInput(text=TEXT), "other-client") is job
    assert len(store._started) == 1  # reuse spends no additional hourly budget


def test_running_job_remains_pollable_after_job_ttl(running_job):
    store, now, job, _, _ = running_job
    now[0] = 21
    assert store.get(job.result.id) is job


def test_expiry_cannot_bypass_running_limit(running_job):
    store, now, _, _, _ = running_job
    now[0] = 21
    with pytest.raises(RateLimited, match="Too many lookups"):
        store.start(LookupInput(text=TEXT + " from another input"), "other-client")
    with pytest.raises(RateLimited, match="Too many lookups"):
        store.check_budget("other-client")


def test_completed_job_still_expires(running_job):
    store, now, job, release, workers = running_job
    release.set()
    workers[0].join(1)
    assert job.result.state == "done"
    now[0] = 21
    assert store.get(job.result.id) is None


@pytest.mark.parametrize("state,age", [("done", 11), ("failed", 1)])
def test_completed_cache_expiry_and_failed_retries(running_job, state, age):
    store, now, job, release, workers = running_job
    release.set()
    workers[0].join(1)
    assert job.result.state == "done"
    job.result.state = state
    now[0] = age
    assert store.start(LookupInput(text=TEXT), "client") is not job
