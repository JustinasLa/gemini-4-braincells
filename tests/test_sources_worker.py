"""Source worker recovery with an unchanged cluster and no external services."""

from collections import Counter
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sources import __main__ as worker
from sources.checker import SourceChecker
from sources.models import ClusterPost, PageFacts
from sources.oxylabs import OxylabsError


@pytest.mark.parametrize("failed_call", ["scrape", "headline", "post_text"])
def test_unchanged_cluster_retries_missing_evidence_after_failure_cooldown(
    monkeypatch, failed_call
):
    clock = [0.0]
    calls = Counter()
    saved = {}
    saved_batches = []
    snapshots = []
    post = ClusterPost(
        did="did:plc:source-worker-test",
        text="The city water supply was poisoned last night",
        embed_urls=["https://news.example/story"],
    )

    class Web:
        def call(self, kind):
            calls[kind] += 1
            if kind == failed_call and calls[kind] == 1:
                raise OxylabsError("temporary provider failure", status=503)

        def scrape_facts(self, url):
            self.call("scrape")
            return PageFacts(url=url, final_url=url, headline="Water supply poisoned")

        def search(self, query, max_results):
            self.call("headline" if query == '"Water supply poisoned"' else "post_text")
            return []

    class StopWorker(Exception):
        pass

    def sleep(_interval):
        snapshots.append(dict(calls))
        if len(snapshots) == 4:
            raise StopWorker
        clock[0] = [60.0, 1801.0, 1861.0][len(snapshots) - 1]

    def save(_conn, _cluster_id, items):
        saved_batches.append([item.detail["key"] for item in items])
        saved.update((item.detail["key"], item.detail) for item in items)

    conn = SimpleNamespace(commit=Mock())
    monkeypatch.setattr(worker, "connect", lambda: nullcontext(conn))
    monkeypatch.setattr(worker.store, "clusters_to_check", lambda *a, **kw: [(1, "unchanged")])
    monkeypatch.setattr(worker.store, "load_cluster_posts", lambda *a: [post])
    monkeypatch.setattr(worker.store, "stored_details", lambda *a: dict(saved))
    monkeypatch.setattr(worker.store, "save_evidence", save)
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker.time, "sleep", sleep)

    with pytest.raises(StopWorker):
        worker.run_worker(SourceChecker(Web()), interval_s=60, min_score=0.5, batch=10)

    assert snapshots[0] == snapshots[1]  # Failure cooldown still prevents repeated paid calls.
    assert calls == Counter(
        {kind: 2 if kind == failed_call else 1 for kind in ("scrape", "headline", "post_text")}
    )
    assert snapshots[2] == snapshots[3]  # Completed evidence is not fetched again.
    assert len(saved) == 3
    assert sum(map(len, saved_batches)) == 3  # Partial successes are stored exactly once.
    assert conn.commit.call_count == 2
