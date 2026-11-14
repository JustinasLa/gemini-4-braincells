"""In-process job store for lookups: start, poll, cache, rate limits.

The API runs one uvicorn process, so a dict is enough. Each lookup costs real Oxylabs, Nemotron
and Gemini calls, so there is a global hourly cap and a per-client cap, and the same input
within the cache TTL returns the earlier job.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable

from lookup.models import LookupInput
from lookup.service import Deps, Lookup
from lookup.urls import classify

log = logging.getLogger(__name__)


class RateLimited(Exception):
    pass


def cache_key(inp: LookupInput) -> str:
    if inp.url:
        t = classify(inp.url)
        return f"url:{t.url if t else inp.url.strip()}"
    return "text:" + " ".join((inp.text or "").lower().split())


class JobStore:
    def __init__(
        self,
        make_deps: Callable[[], Deps],
        *,
        max_per_hour: int = 60,
        max_per_client_per_hour: int = 10,
        cache_ttl_s: float = 1800,
        job_ttl_s: float = 3600,
        max_running: int = 4,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._make_deps = make_deps
        self.max_per_hour = max_per_hour
        self.max_per_client = max_per_client_per_hour
        self.cache_ttl_s = cache_ttl_s
        self.job_ttl_s = job_ttl_s
        self.max_running = max_running
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, tuple[float, Lookup]] = {}
        self._by_key: dict[str, str] = {}
        self._started: deque[tuple[float, str]] = deque()  # (time, client)

    def _expire(self, now: float) -> None:
        for jid, (t0, _) in list(self._jobs.items()):
            if now - t0 > self.job_ttl_s:
                del self._jobs[jid]
        for k, jid in list(self._by_key.items()):
            if jid not in self._jobs:
                del self._by_key[k]
        while self._started and now - self._started[0][0] > 3600:
            self._started.popleft()

    def get(self, job_id: str) -> Lookup | None:
        with self._lock:
            self._expire(self._clock())
            hit = self._jobs.get(job_id)
            return hit[1] if hit else None

    def start(self, inp: LookupInput, client: str) -> Lookup:
        key = cache_key(inp)
        with self._lock:
            now = self._clock()
            self._expire(now)
            jid = self._by_key.get(key)
            if jid and jid in self._jobs:
                t0, job = self._jobs[jid]
                failed = job.result.state == "failed"
                if not failed and now - t0 <= self.cache_ttl_s:
                    return job
            running = sum(1 for _, j in self._jobs.values() if j.result.state == "running")
            if running >= self.max_running:
                raise RateLimited("Too many lookups are running right now; try again in a minute.")
            if self.max_per_hour > 0 and len(self._started) >= self.max_per_hour:
                raise RateLimited("The hourly lookup budget is used up; try again later.")
            mine = sum(1 for _, c in self._started if c == client)
            if self.max_per_client > 0 and mine >= self.max_per_client:
                raise RateLimited("You've run many lookups this hour; try again later.")
            job = Lookup(f"lk_{secrets.token_hex(8)}", inp, self._make_deps())
            self._jobs[job.result.id] = (now, job)
            self._by_key[key] = job.result.id
            self._started.append((now, client))
        threading.Thread(target=job.run, name=f"lookup-{job.result.id}", daemon=True).start()
        log.info("lookup %s started kind=%s client=%s", job.result.id, job.result.kind, client)
        return job
