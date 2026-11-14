"""External validation: does Bluesky itself later act against the accounts we flag?

Bluesky's moderation is independent of us: it never sees our scores, and we never tune on its
actions. So the share of flagged accounts that Bluesky later takes down is an honest, external
signal of whether "coordinated" means something. It is NOT precision: moderation misses many
bad accounts (so this is a lower bound for bad accounts among those we flag) and acts for reasons
other than coordination (spam, harassment, impersonation...).

Groups (accounts, not clusters; synthetic clusters excluded):
  coordinated / insufficient / likely_organic   members of a stored cluster with that status,
                                                flagged at the first such cluster's created_at
  insufficient_p_only                           the subset of `insufficient` clusters that met
                                                every other `coordinated` condition (families,
                                                score, organic guard) and were held back only
                                                by the p-value: are our strongest abstentions
                                                real? (accounts also count in `insufficient`)
  baseline                                      random active accounts (signals.baseline), flagged
                                                at their sampling time (sampling began later)
  random_active                                 per UTC hour since ingest began, up to 300 accounts
                                                drawn by hash from everyone who posted, liked or
                                                reposted in that hour, flagged at the hour's end
                                                (retroactive, so it covers the same hours as flags)
Outcomes, from Jetstream account events (stored by ingest since its first minute):
  moderation  status takendown or suspended (platform action)
  deleted     account deleted (by the owner or the platform; reported separately)
  throttled   rate-limited by the platform (reported separately)
Fairness: an account counts only if it was flagged at least `horizon_h` hours ago, and only
outcomes within `horizon_h` hours after its flag count, so every group gets the same time at risk.
Accounts already actioned before they were flagged are dropped.

    uv run python -m eval takedowns                    # print the table (horizon 3 h)
    uv run python -m eval takedowns --horizon-h 6
    uv run python -m eval takedowns --write            # ...and write an eval_runs row
"""

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

from common.config import get_settings
from signals.config import DEFAULT

GROUPS = (
    "coordinated",
    "insufficient",
    "insufficient_p_only",
    "likely_organic",
    "baseline",
    "random_active",
)
OUTCOMES = {
    "moderation": ("takendown", "suspended"),
    "deleted": ("deleted",),
    "throttled": ("throttled",),
}
DATASET = "bluesky_takedowns"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


@dataclass(frozen=True)
class Flag:
    did: str
    group: str
    at: datetime


def table(
    flags: list[Flag],
    events: dict[str, list[tuple[datetime, str]]],
    now: datetime,
    horizon_h: float,
) -> dict:
    """flags: one per (did, group); events: did -> [(time, status)]. Pure, for tests."""
    horizon = timedelta(hours=horizon_h)
    out: dict = {}
    for g in GROUPS:
        eligible = 0
        dropped_prior = 0
        too_recent = 0
        hits = defaultdict(int)
        for f in (f for f in flags if f.group == g):
            if f.at > now - horizon:
                too_recent += 1
                continue
            evs = events.get(f.did, [])
            if any(t <= f.at and s in sum(OUTCOMES.values(), ()) for t, s in evs):
                dropped_prior += 1
                continue
            eligible += 1
            for name, statuses in OUTCOMES.items():
                if any(f.at < t <= f.at + horizon and s in statuses for t, s in evs):
                    hits[name] += 1
        row = {
            "accounts": eligible,
            "excluded_too_recent": too_recent,
            "excluded_already_actioned": dropped_prior,
        }
        for name in OUTCOMES:
            k = hits[name]
            lo, hi = wilson(k, eligible)
            row[name] = {
                "k": k,
                "rate": round(k / eligible, 5) if eligible else None,
                "ci95": [round(lo, 5), round(hi, 5)],
            }
        out[g] = row
    return out


def load(conn: psycopg.Connection) -> tuple[list[Flag], dict]:
    flags = [
        Flag(r[0], r[1], r[2])
        for r in conn.execute(
            """
            SELECT m.did, c.status, min(c.created_at)
            FROM clusters c JOIN cluster_members m ON m.cluster_id = c.id
            WHERE NOT c.synthetic GROUP BY 1, 2
            UNION ALL
            SELECT m.did, 'insufficient_p_only', min(c.created_at)
            FROM clusters c JOIN cluster_members m ON m.cluster_id = c.id
            WHERE NOT c.synthetic AND c.status = 'insufficient'
              AND c.families_fired >= %(families)s AND c.score >= %(score)s
              AND coalesce((c.signals->>'organic_guard_score')::float, 0) < %(guard)s
            GROUP BY 1
            UNION ALL
            SELECT did, 'baseline', sampled_at FROM baseline_accounts
            """,
            {
                "families": DEFAULT.min_families_coordinated,
                "score": DEFAULT.min_score_coordinated,
                "guard": DEFAULT.organic_guard_strong,
            },
        )
    ]
    flags += random_active(conn)
    events: dict[str, list] = defaultdict(list)
    for did, t, status in conn.execute(
        "SELECT did, coalesce(event_time, received_at), status FROM account_events "
        "WHERE kind = 'account' AND status = ANY(%s)",
        (sorted(sum(OUTCOMES.values(), ())),),
    ):
        events[did].append((t, status))
    return flags, events


def random_active(conn: psycopg.Connection, per_hour: int = 300) -> list[Flag]:
    """Deterministic random sample of active accounts per UTC hour (hash of hour + did)."""
    hours = conn.execute(
        "SELECT generate_series(date_trunc('hour', min(seen_at)), date_trunc('hour', max(seen_at)),"
        " interval '1 hour') FROM posts"
    ).fetchall()
    out = []
    for (h,) in hours:
        rows = conn.execute(
            """
            SELECT did FROM (
                SELECT did FROM posts WHERE seen_at >= %(h)s AND seen_at < %(h)s + interval '1 hour'
                UNION SELECT did FROM likes
                    WHERE seen_at >= %(h)s AND seen_at < %(h)s + interval '1 hour'
                UNION SELECT did FROM reposts WHERE seen_at >= %(h)s
                    AND seen_at < %(h)s + interval '1 hour'
            ) a ORDER BY md5(%(seed)s || did) LIMIT %(n)s
            """,
            {"h": h, "seed": h.isoformat(), "n": per_hour},
        ).fetchall()
        out += [Flag(r[0], "random_active", h + timedelta(hours=1)) for r in rows]
    # one flag per account: the earliest hour it was drawn
    first: dict[str, Flag] = {}
    for f in sorted(out, key=lambda f: f.at):
        first.setdefault(f.did, f)
    return list(first.values())


def cluster_table(conn: psycopg.Connection, events: dict, now: datetime, horizon_h: float) -> dict:
    """Per stored cluster: did Bluesky moderate at least one member within the horizon? Shows
    whether the account-level rate comes from many clusters or from one big ring."""
    horizon = timedelta(hours=horizon_h)
    mod = OUTCOMES["moderation"]
    out: dict = {}
    rows = conn.execute(
        """
        SELECT c.id, c.status, c.created_at, array_agg(m.did) FROM clusters c
        JOIN cluster_members m ON m.cluster_id = c.id
        WHERE NOT c.synthetic AND c.created_at <= %s GROUP BY 1, 2, 3
        """,
        (now - horizon,),
    ).fetchall()
    for _id, status, at, dids in rows:
        r = out.setdefault(status, {"clusters": 0, "with_moderated_member": 0})
        r["clusters"] += 1
        if any(at < t <= at + horizon and s in mod for d in dids for t, s in events.get(d, [])):
            r["with_moderated_member"] += 1
    for r in out.values():
        lo, hi = wilson(r["with_moderated_member"], r["clusters"])
        r["rate"] = round(r["with_moderated_member"] / r["clusters"], 4) if r["clusters"] else None
        r["ci95"] = [round(lo, 4), round(hi, 4)]
    return out


def run(conn: psycopg.Connection, horizon_h: float, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    flags, events = load(conn)
    versions = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT detector_version FROM clusters WHERE NOT synthetic ORDER BY 1"
        )
    ]
    first_event = conn.execute(
        "SELECT min(received_at) FROM account_events WHERE kind = 'account'"
    ).fetchone()[0]
    t = table(flags, events, now, horizon_h)
    ct = cluster_table(conn, events, now, horizon_h)
    coord, base = t["coordinated"]["moderation"], t["baseline"]["moderation"]
    return {
        "computed_at": now.isoformat(),
        "horizon_h": horizon_h,
        "groups": t,
        "clusters": ct,
        "headline": {
            "coordinated_moderated_rate": coord["rate"],
            "coordinated_ci95": coord["ci95"],
            "baseline_moderated_rate": base["rate"],
            "baseline_ci95": base["ci95"],
            "coordinated_accounts": t["coordinated"]["accounts"],
            "baseline_accounts": t["baseline"]["accounts"],
            "random_active_moderated_rate": t["random_active"]["moderation"]["rate"],
            "random_active_ci95": t["random_active"]["moderation"]["ci95"],
            "random_active_accounts": t["random_active"]["accounts"],
        },
        "detector_versions": versions,
        "account_events_since": first_event.isoformat() if first_event else None,
        "note": "Share of accounts Bluesky took down or suspended within horizon_h hours after we "
        "flagged them, vs. random active accounts. External and untuned, but not "
        "precision: moderation misses bad accounts and acts for other reasons too.",
    }


def write(conn: psycopg.Connection, result: dict) -> int:
    n = result["groups"]["coordinated"]["accounts"]
    return conn.execute(
        "INSERT INTO eval_runs (dataset, split, n, details) VALUES (%s, %s, %s, %s) RETURNING id",
        (DATASET, "live", n, json.dumps(result)),
    ).fetchone()[0]


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--horizon-h",
        type=float,
        default=3.0,
        help="hours at risk after the flag, the same for every group",
    )
    p.add_argument("--write", action="store_true", help="also write an eval_runs row")
    p.add_argument("--database-url", default=None)


def main_args(args: argparse.Namespace) -> int:
    with psycopg.connect(args.database_url or get_settings().database_url, autocommit=True) as c:
        result = run(c, args.horizon_h)
        if args.write:
            result["eval_run_id"] = write(c, result)
    for g, row in result["groups"].items():
        m = row["moderation"]
        rate = f"{m['rate']:.2%}" if m["rate"] is not None else "-"
        print(
            f"{g:19} accounts={row['accounts']:6}  moderated={m['k']:4} ({rate}, "
            f"95% CI {m['ci95'][0]:.2%}-{m['ci95'][1]:.2%})  deleted={row['deleted']['k']}  "
            f"throttled={row['throttled']['k']}  (too recent: {row['excluded_too_recent']}, "
            f"already actioned: {row['excluded_already_actioned']})"
        )
    if args.write:
        print("eval_runs id", result["eval_run_id"])
    for status, r in sorted(result["clusters"].items()):
        print(
            f"clusters {status:15} {r['with_moderated_member']}/{r['clusters']} have a member "
            f"moderated within the horizon ({(r['rate'] or 0):.1%})"
        )
    return 0
