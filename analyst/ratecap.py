"""Hourly cap on Gemini requests made by the analyst worker.

The Gemini key belongs to a temporary hackathon project that is free to use but watched for
abuse ("thousands of requests" can get a team disqualified), so the worker spends a fixed number
of model requests per sliding hour.

- Counted at the client: `HourlyCap.wrap` puts a counter in front of `generate_content`, so every
  request counts, including turns of an agent run that later fails.
- Reserved up front: a verdict starts only if the worst case it can still spend (the agent's
  max turns + 1) fits under the limit, so one long agent run can't overshoot it.
- Seeded on start from the analyst rows stored in the last hour (their `model_calls`; verdicts
  written without a model call count 0), so a restart doesn't reset the window. Requests of
  failed runs are not stored, so a restart forgets those; manual CLI, compare and eval runs on
  the same key are not counted at all.
"""

import time
from collections import deque
from collections.abc import Callable

import psycopg

WINDOW_S = 3600.0


class HourlyCap:
    """Sliding-window budget of `limit` requests per hour; `limit <= 0` means no cap."""

    def __init__(self, limit: int, *, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self._clock = clock
        self._spent: deque[tuple[float, int]] = deque()  # (monotonic time, requests)

    def _expire(self) -> float:
        now = self._clock()
        while self._spent and self._spent[0][0] <= now - WINDOW_S:
            self._spent.popleft()
        return now

    def used(self) -> int:
        self._expire()
        return sum(n for _, n in self._spent)

    def allows(self, reserve: int = 1) -> bool:
        """True if `reserve` more requests fit in the current window."""
        return self.limit <= 0 or self.used() + reserve <= self.limit

    def record(self, n: int = 1, *, age_s: float = 0.0) -> None:
        if n > 0:
            self._spent.append((self._clock() - age_s, n))

    def seconds_until_free(self, reserve: int = 1) -> float:
        """How long until enough spend leaves the window for `reserve` more requests."""
        now = self._expire()
        if self.allows(reserve):
            return 0.0
        used = self.used()
        for t, n in self._spent:
            used -= n
            if used + reserve <= self.limit:
                return max(0.0, t + WINDOW_S - now)
        return WINDOW_S  # reserve > limit: never fits; check again in an hour

    def wrap(self, client):
        """A genai.Client stand-in whose `models.generate_content` records one request first."""
        return _CountingClient(client, self)

    def seed_from_db(self, conn: psycopg.Connection) -> int:
        """Count analyst verdicts stored in the last hour against the window; returns requests."""
        rows = conn.execute(
            """
            SELECT extract(epoch FROM now() - created_at)::float8 AS age_s,
                   CASE WHEN model = 'none' THEN 0
                        ELSE greatest(1, coalesce(jsonb_array_length(detail->'model_calls'), 0))
                   END AS calls
            FROM evidence
            WHERE kind = 'analyst' AND created_at > now() - interval '1 hour'
            """
        ).fetchall()
        total = 0
        for r in rows:
            self.record(int(r["calls"]), age_s=float(r["age_s"]))
            total += int(r["calls"])
        return total


class _CountingModels:
    def __init__(self, models, cap: HourlyCap):
        self._models = models
        self._cap = cap

    def generate_content(self, **kwargs):
        self._cap.record(1)
        return self._models.generate_content(**kwargs)

    def __getattr__(self, name):
        return getattr(self._models, name)


class _CountingClient:
    def __init__(self, client, cap: HourlyCap):
        self._client = client
        self.models = _CountingModels(client.models, cap)

    def __getattr__(self, name):
        return getattr(self._client, name)
