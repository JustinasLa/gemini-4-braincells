"""Base rates for the identity test, measured on a random sample of active accounts.

Each detection pass samples a few accounts that were active in the window (seeded by the window
end, so a replay samples the same ones), enriches them like cluster members and records them in
`baseline_accounts`. The base rates are the share of fresh accounts and of empty profiles among
the sampled accounts with data, over the last day.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime

import psycopg

from signals.config import DetectorConfig
from signals.enrich import enrich
from signals.window import WindowData


@dataclass(frozen=True)
class BaseRates:
    n_age: int  # sampled accounts with a known age
    fresh: float  # share younger than cfg.fresh_account_days
    n_profile: int  # sampled accounts with profile data
    empty: float  # share with no avatar, no bio and < 5 followers

    def as_dict(self) -> dict:
        return {
            "sample_with_age": self.n_age,
            "fresh_rate": round(self.fresh, 4),
            "sample_with_profile": self.n_profile,
            "empty_rate": round(self.empty, 4),
        }


def active_dids(wd: WindowData) -> list[str]:
    return sorted({p.did for p in wd.posts} | {a.did for a in wd.actions})


def sample(wd: WindowData, n: int, seed: str) -> list[str]:
    """n active accounts, chosen by a hash of (seed, did): deterministic and uniform."""
    ranked = sorted(
        active_dids(wd),
        key=lambda d: hashlib.blake2b(f"{seed}|{d}".encode(), digest_size=8).digest(),
    )
    return ranked[:n]


def sample_and_enrich(conn: psycopg.Connection, wd: WindowData, end: datetime, n: int = 40) -> int:
    dids = sample(wd, n, seed=end.isoformat())
    if not dids:
        return 0
    conn.execute(
        "INSERT INTO baseline_accounts (did, window_end) SELECT unnest(%s::text[]), %s "
        "ON CONFLICT (did) DO NOTHING",
        (dids, end),
    )
    return enrich(conn, dids, budget=n)


def load_rates(
    conn: psycopg.Connection, now: datetime, cfg: DetectorConfig, hours: int = 24
) -> BaseRates | None:
    row = conn.execute(
        """
        SELECT
          count(*) FILTER (WHERE born IS NOT NULL),
          count(*) FILTER (
            WHERE born IS NOT NULL AND born > %(now)s - make_interval(days => %(fd)s)),
          count(*) FILTER (WHERE has_avatar IS NOT NULL),
          count(*) FILTER (WHERE has_avatar IS NOT NULL AND NOT has_avatar
                           AND coalesce(description_len, 0) = 0 AND coalesce(followers, 0) < 5)
        FROM (
          SELECT least(a.did_created_at, a.created_at) AS born, a.has_avatar, a.description_len,
                 a.followers
          FROM baseline_accounts b JOIN accounts a USING (did)
          WHERE b.sampled_at > %(now)s - make_interval(hours => %(h)s)
            AND b.sampled_at <= %(now)s  -- no look-ahead: replays/eval never see later samples
        ) s
        """,
        {"now": now, "fd": cfg.fresh_account_days, "h": hours},
    ).fetchone()
    n_age, n_fresh, n_prof, n_empty = row
    if not n_age and not n_prof:
        return None
    return BaseRates(
        n_age=n_age,
        fresh=n_fresh / n_age if n_age else 0.0,
        n_profile=n_prof,
        empty=n_empty / n_prof if n_prof else 0.0,
    )
