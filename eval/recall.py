"""Recall on synthetic coordinated campaigns injected into recorded Jetstream windows.

For each evaluated window of a split, and for each difficulty level (L1-L3, see signals/synth.py),
inject one synthetic campaign at a time (in memory, `did:plc:synth*` accounts, never posted
anywhere) into the real recorded window and run the detector's own entry point
(`signals.detect.detect`). A campaign is *caught* when at least MIN_COVERAGE of its accounts end up
in clusters the detector marks `coordinated`. Recall = caught / injected.

Only synthetic accounts get account info (age, followers, ...): the real accounts in an offline
window have no enrichment, as the live worker would only fetch it for suspicious clusters.
"""

import argparse
import hashlib
import json
import logging
import math
import multiprocessing as mp
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from eval.slices import SLICE_ROOT, SPLITS, hour_files, iter_windows, split_hours
from signals.config import DEFAULT, DetectorConfig
from signals.detect import detect
from signals.keys import MinHashCache
from signals.score import ClusterScore
from signals.store import worth_storing
from signals.synth import Synthetic, campaign, inject
from signals.window import WindowData

log = logging.getLogger("eval.recall")

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


def add_arguments(r: argparse.ArgumentParser) -> None:
    r.add_argument("--slice", default=str(SLICE_ROOT), help="root of the copied recordings")
    r.add_argument("--split", required=True, choices=list(SPLITS))
    r.add_argument("--hours", nargs="+", help="ad-hoc UTC hours (YYYYMMDDHH) instead of the split")
    r.add_argument("--levels", nargs="+", default=list(LEVELS), choices=list(LEVELS))
    r.add_argument("--repeats", type=int, default=5, help="campaigns per level per window")
    r.add_argument("--window-s", type=float, default=600.0)
    r.add_argument("--max-windows", type=int, default=0)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--database-url", help="write an eval_runs row here (nothing written if unset)")
    r.add_argument("--json-out", help="also write the result as JSON")


# ---------------------------------------------------------------- CLI: python -m eval recall ---

_WINDOWS: list = []  # filled before the worker pool forks, read by workers (copy-on-write)
_MH: MinHashCache | None = None


def _work(task: tuple[int, str | None, int]):
    global _MH
    idx, level, k = task
    label, wd = _WINDOWS[idx]
    _MH = _MH or MinHashCache(DEFAULT.minhash_perm)
    if level is None:  # baseline: the window without any injection (context only)
        return ("base", label, detect(wd, None, DEFAULT, _MH).stats["by_status"])
    return ("trial", label, run_trial(wd, level, k, label, DEFAULT, _MH))


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def main_args(args) -> int:
    _WINDOWS.clear()  # module state for the fork pool: never carry windows over from a past run
    if args.hours and args.database_url:
        # ad-hoc hours are not the split's hours: never publish them under a split's name
        print(
            "--hours is for ad-hoc runs and can't be combined with --database-url", file=sys.stderr
        )
        return 2
    hours = args.hours or split_hours(Path(args.slice), args.split)
    run_label = f"{args.split}, AD-HOC hours" if args.hours else args.split
    files = hour_files(Path(args.slice), hours)
    if not files:
        print(f"no recorded {args.split} hours under {args.slice} ({hours})", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    skipped = []
    for info, wd in iter_windows(files, args.window_s, DEFAULT):
        if wd is None:
            skipped.append({"window": info.label, "reason": info.skipped})
            continue
        _WINDOWS.append((info.label, wd))
        if args.max_windows and len(_WINDOWS) >= args.max_windows:
            break
    log.info(
        "loaded %d windows (%d skipped) in %.0fs",
        len(_WINDOWS),
        len(skipped),
        time.monotonic() - t0,
    )
    if not _WINDOWS:
        print("no complete windows in this slice", file=sys.stderr)
        return 2
    tasks = [(i, None, 0) for i in range(len(_WINDOWS))]
    tasks += [
        (i, lv, k) for i in range(len(_WINDOWS)) for lv in args.levels for k in range(args.repeats)
    ]
    trials, baseline = [], {}
    with mp.get_context("fork").Pool(args.workers) as pool:
        for n_done, (kind, window, out) in enumerate(pool.imap_unordered(_work, tasks), 1):
            if kind == "base":
                baseline[window] = out
            else:
                trials.append(out)
            if n_done % 10 == 0 or n_done == len(tasks):
                log.info(
                    "%d/%d detector runs done (%.0fs)", n_done, len(tasks), time.monotonic() - t0
                )
    trials.sort(key=lambda t: (t.window, t.level, t.seed))
    summary = summarize(trials)
    details = {
        "synthetic": True,
        "what": "share of injected synthetic campaigns (did:plc:synth*) whose accounts the "
        "detector marks coordinated",
        "detector_version": DEFAULT.version,
        "eval_commit": _git_sha(),
        "hit_rule": f">= {MIN_COVERAGE:.0%} of the campaign's accounts in clusters with "
        "status 'coordinated'",
        "per_level": summary["per_level"],
        "recall_ci95": summary["overall"]["ci95"],
        "window_s": args.window_s,
        "hours_utc": hours,
        "windows": [label for label, _ in _WINDOWS],
        "windows_skipped": skipped,
        "repeats_per_level_per_window": args.repeats,
        "baseline_clusters_by_window": dict(sorted(baseline.items())),
        "trials": trial_dicts(trials),
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "runtime_s": round(time.monotonic() - t0),
    }
    print(format_table(summary, run_label))
    print(
        f"\n{len(_WINDOWS)} windows of {args.window_s:.0f}s from hours {', '.join(hours)} UTC; "
        f"skipped {len(skipped)}; detector {DEFAULT.version}; {details['runtime_s']}s"
    )
    overall = summary["overall"]
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "dataset": DATASET,
                    "split": args.split,
                    "adhoc_hours": args.hours or None,
                    "recall": overall["rate"],
                    "n": overall["n"],
                    "details": details,
                },
                indent=1,
            )
        )
    if args.database_url:
        from eval.store import write_eval_run

        rid = write_eval_run(
            DATASET,
            args.split,
            recall=overall["rate"],
            n=overall["n"],
            details=details,
            database_url=args.database_url,
        )
        print(f"wrote eval_runs id={rid} (dataset={DATASET}, split={args.split})")
    return 0
