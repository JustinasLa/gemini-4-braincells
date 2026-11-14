"""Process-wide caches shared by lookups (the API runs one process).

The job store already returns the earlier result for the same input. These caches go one level
down: the same Oxylabs search or page read (a copier's profile, a copy page, a news story two
lookups both find), and the same image, within a short TTL. A cache hit is never passed off as
a fresh read: its receipt says `cached` and keeps the time of the original read, and X counts
keep that time as `metrics_as_of`. Failures are never cached.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any


class TTLCache:
    """Thread-safe LRU with a TTL; `get` returns None for a missing or expired key."""

    def __init__(
        self, ttl_s: float, max_items: int = 256, *, clock: Callable[[], float] = time.monotonic
    ):
        self.ttl_s, self.max_items, self._clock = ttl_s, max_items, clock
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return default
            if self._clock() - hit[0] > self.ttl_s:
                del self._data[key]
                return default
            self._data.move_to_end(key)
            return hit[1]

    def __contains__(self, key: Any) -> bool:
        return self.get(key) is not None

    def __getitem__(self, key: Any) -> Any:
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v

    def __setitem__(self, key: Any, value: Any) -> None:
        if self.ttl_s <= 0:
            return
        with self._lock:
            self._data[key] = (self._clock(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_items:
                self._data.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class CachedOxylabs:
    """Wraps sources.oxylabs.OxylabsClient: same methods, results cached by URL or query.

    `last_call` is per thread, like the client's: the call dict of this thread's last request,
    with `fetched_at` (UTC, when the provider was actually asked) and `cached`."""

    def __init__(self, inner: Any, cache: TTLCache):
        self._inner = inner
        self._cache = cache
        self._local = threading.local()

    @property
    def last_call(self) -> dict:
        return getattr(self._local, "last_call", {})

    def _call(self, key: tuple, fn: Callable[[], Any]) -> Any:
        hit = self._cache.get(key)
        if hit is not None:
            value, call = hit
            self._local.last_call = {**call, "cached": True, "latency_ms": 0}
            return value
        value = fn()
        call = {**dict(self._inner.last_call), "fetched_at": datetime.now(UTC), "cached": False}
        self._cache[key] = (value, call)
        self._local.last_call = call
        return value

    def search(self, query: str, max_results: int = 10, *, deadline: float | None = None):
        return self._call(
            ("search", query, max_results),
            lambda: self._inner.search(query, max_results=max_results, deadline=deadline),
        )

    def scrape_markdown(self, url: str, *, deadline: float | None = None):
        return self._call(
            ("markdown", url), lambda: self._inner.scrape_markdown(url, deadline=deadline)
        )

    def scrape_facts(self, url: str, *, deadline: float | None = None):
        return self._call(("facts", url), lambda: self._inner.scrape_facts(url, deadline=deadline))


# One of each per process (lookup/jobs.py runs every lookup in the API process).
_OXYLABS: TTLCache | None = None
MEDIA = TTLCache(ttl_s=24 * 3600, max_items=512)  # Nemotron image readings by image hash
MEDIA_URLS = TTLCache(ttl_s=24 * 3600, max_items=1024)  # image URL -> hash (CDN URLs are immutable)


def oxylabs_cache(ttl_s: float) -> TTLCache:
    global _OXYLABS
    if _OXYLABS is None or _OXYLABS.ttl_s != ttl_s:
        _OXYLABS = TTLCache(ttl_s=ttl_s, max_items=256)
    return _OXYLABS
