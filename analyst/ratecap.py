"""Hourly cap on Gemini requests made by the analyst worker.

The Gemini key belongs to a temporary hackathon project that is free to use but watched for
abuse ("thousands of requests" can get a team disqualified), so the worker spends a fixed number
of model requests per sliding hour. A verdict counts one request per model turn it logged
(`Verdict.model_calls`) and at least one, so the single-shot batch path counts too. On start the
window is seeded from the analyst rows stored in the last hour, so a restart doesn't reset it.
"""

import time
from collections import deque
from collections.abc import Callable

import psycopg

from analyst.verdict import Verdict

WINDOW_S = 3600.0


def calls_in(verdict: Verdict | None) -> int:
    """Gemini requests a verdict used (failed runs count as one: the model may have been hit)."""
    if verdict is None:
        return 1
    return max(1, len(verdict.model_calls))


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

    def allows(self) -> bool:
        return self.limit <= 0 or self.used() < self.limit

    def record(self, n: int, *, age_s: float = 0.0) -> None:
        if n > 0:
            self._spent.append((self._clock() - age_s, n))

    def seconds_until_free(self) -> float:
        """How long until the oldest spend leaves the window (0 when there is room now)."""
        now = self._expire()
        if self.allows() or not self._spent:
            return 0.0
        return max(0.0, self._spent[0][0] + WINDOW_S - now)

    def seed_from_db(self, conn: psycopg.Connection) -> int:
        """Count analyst verdicts stored in the last hour against the window; returns requests."""
        rows = conn.execute(
            """
            SELECT extract(epoch FROM now() - created_at) AS age_s,
                   greatest(1, coalesce(jsonb_array_length(detail->'model_calls'), 0)) AS calls
            FROM evidence
            WHERE kind = 'analyst' AND created_at > now() - interval '1 hour'
            """
        ).fetchall()
        total = 0
        for r in rows:
            self.record(int(r["calls"]), age_s=float(r["age_s"]))
            total += int(r["calls"])
        return total
