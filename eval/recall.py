"""Recall on synthetic coordinated campaigns injected into recorded Jetstream windows.

For each evaluated window of a split, and for each difficulty level (L1-L3, see signals/synth.py),
inject one synthetic campaign at a time (in memory, `did:plc:synth*` accounts, never posted
anywhere) into the real recorded window and run the detector's own entry point
(`signals.detect.detect`). A campaign is *caught* when at least MIN_COVERAGE of its accounts end up
in clusters the detector marks `coordinated`. Recall = caught / injected.

Only synthetic accounts get account info (age, followers, ...): the real accounts in an offline
window have no enrichment, as the live worker would only fetch it for suspicious clusters.
"""

import hashlib
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass

from signals.config import DEFAULT, DetectorConfig
from signals.detect import detect
from signals.keys import MinHashCache
from signals.score import ClusterScore
from signals.store import worth_storing
from signals.synth import Synthetic, campaign, inject
from signals.window import WindowData

DATASET = "synthetic_campaigns"
LEVELS = ("L1", "L2", "L3")
MIN_COVERAGE = 0.5  # share of a campaign's accounts that must be in `coordinated` clusters
MIN_STORE_SCORE = 0.5  # same default as the live worker (SIGNALS_MIN_STORE_SCORE)


@dataclass
class Trial:
    window: str
    level: str
    seed: int
    n_accounts: int
    caught: bool  # >= MIN_COVERAGE of the campaign's accounts in `coordinated` clusters
    surfaced: bool  # >= MIN_COVERAGE in clusters the worker would store (any status)
    coverage: float  # share of campaign accounts in `coordinated` clusters
    best_status: str | None  # status of the cluster holding most campaign accounts
    best_size: int
    best_purity: float  # share of that cluster's members that are synthetic campaign accounts
    best_score: float | None
    best_p: float | None
    families_fired: list[str]


def judge(
    s: Synthetic,
    clusters: list[ClusterScore],
    cfg: DetectorConfig = DEFAULT,
    min_coverage: float = MIN_COVERAGE,
    window: str = "",
    seed: int = 0,
) -> Trial:
    """Score one injected campaign against the detector's clusters. Pure."""
    dids = s.dids
    n = len(dids)
    flagged = set().union(*(set(c.members) & dids for c in clusters if c.status == "coordinated"))
    stored = set().union(
        *(set(c.members) & dids for c in clusters if worth_storing(c, cfg, MIN_STORE_SCORE))
    )
    best = max(
        (c for c in clusters if set(c.members) & dids),
        key=lambda c: (len(set(c.members) & dids), c.status == "coordinated", c.score),
        default=None,
    )
    coverage = len(flagged) / n if n else 0.0
    return Trial(
        window=window,
        level=s.name.split("_")[1] if s.name.startswith("campaign_") else s.name,
        seed=seed,
        n_accounts=n,
        caught=n > 0 and coverage >= min_coverage,
        surfaced=n > 0 and len(stored) / n >= min_coverage,
        coverage=round(coverage, 4),
        best_status=best.status if best else None,
        best_size=len(best.members) if best else 0,
        best_purity=round(len(set(best.members) & dids) / len(best.members), 4) if best else 0.0,
        best_score=round(best.score, 4) if best else None,
        best_p=best.p_value if best else None,
        families_fired=list(best.families_fired) if best else [],
    )


def trial_seed(window_start: float, level: str, k: int) -> int:
    """Deterministic, different for every window/level/repeat (so dev and heldout never share)."""
    h = hashlib.blake2b(f"{int(window_start)}|{level}|{k}".encode(), digest_size=4)
    return int.from_bytes(h.digest(), "big")


def run_trial(
    wd: WindowData,
    level: str,
    k: int,
    label: str = "",
    cfg: DetectorConfig = DEFAULT,
    mh_cache: MinHashCache | None = None,
) -> Trial:
    """Inject repeat `k` of a `level` campaign into this window, run the detector, score it."""
    seed = trial_seed(wd.start, level, k)
    s = campaign(level, wd, seed=seed)
    res = detect(inject(wd, s), _synthetic_info(s), cfg, mh_cache)
    return judge(s, res.clusters, cfg, window=label, seed=seed)


def _synthetic_info(s: Synthetic) -> Callable:
    return lambda dids: {d: s.accounts[d] for d in dids if d in s.accounts}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for k successes out of n."""
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)


def _rate(trials: list[Trial], attr: str) -> dict:
    n = len(trials)
    k = sum(getattr(t, attr) for t in trials)
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "ci95": wilson(k, n)}


def summarize(trials: list[Trial]) -> dict:
    """Overall and per-level recall (with Wilson 95% CIs), plus 'surfaced' (stored at any
    status, i.e. visible on the dashboard even when the detector abstains)."""
    levels = sorted({t.level for t in trials})
    per_level = {}
    for lv in levels:
        ts = [t for t in trials if t.level == lv]
        statuses: dict[str, int] = {}
        for t in ts:
            statuses[t.best_status or "none"] = statuses.get(t.best_status or "none", 0) + 1
        per_level[lv] = {
            "recall": _rate(ts, "caught"),
            "surfaced": _rate(ts, "surfaced"),
            "mean_coverage": round(sum(t.coverage for t in ts) / len(ts), 4),
            "best_cluster_status": statuses,
        }
    return {"overall": _rate(trials, "caught"), "per_level": per_level}


def format_table(summary: dict, split: str) -> str:
    rows = [f"recall on synthetic campaigns ({split})", ""]
    head = f"{'level':<8}{'caught':>10}{'recall':>9}{'95% CI':>17}{'surfaced':>11}"
    rows.append(head + "  best status")
    items = [*summary["per_level"].items(), ("ALL", {"recall": summary["overall"]})]
    for lv, d in items:
        r = d["recall"]
        ci = f"[{r['ci95'][0]:.2f}, {r['ci95'][1]:.2f}]" if r["ci95"] else "-"
        rate = f"{r['rate']:.2f}" if r["rate"] is not None else "-"
        surf = f"{d['surfaced']['k']}/{d['surfaced']['n']}" if "surfaced" in d else ""
        status = ", ".join(f"{k} {v}" for k, v in sorted(d.get("best_cluster_status", {}).items()))
        rows.append(f"{lv:<8}{r['k']:>5}/{r['n']:<4}{rate:>9}{ci:>17}{surf:>11}  {status}")
    return "\n".join(rows)


def trial_dicts(trials: list[Trial]) -> list[dict]:
    return [asdict(t) for t in trials]
