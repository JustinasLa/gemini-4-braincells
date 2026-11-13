"""Eval CLI.

python -m eval copy-slices --split dev                 # recorder files -> SLICE_ROOT (keeps them)
python -m eval recall --split heldout [--slice DIR] [--database-url URL] [--json-out FILE]
"""

import argparse
import json
import logging
import multiprocessing as mp
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from eval import recall as R
from eval.slices import RECORD_ROOT, SLICE_ROOT, SPLITS, hour_files, iter_windows
from signals.config import DEFAULT
from signals.detect import detect
from signals.keys import MinHashCache

log = logging.getLogger("eval")

_WINDOWS: list = []  # filled before the worker pool forks, read by workers (copy-on-write)
_MH: MinHashCache | None = None


def _work(task: tuple[int, str | None, int]):
    global _MH
    idx, level, k = task
    label, wd = _WINDOWS[idx]
    _MH = _MH or MinHashCache(DEFAULT.minhash_perm)
    if level is None:  # baseline: the window without any injection (context only)
        return ("base", label, detect(wd, None, DEFAULT, _MH).stats["by_status"])
    return ("trial", label, R.run_trial(wd, level, k, label, DEFAULT, _MH))


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


def cmd_recall(args) -> int:
    if args.hours and args.database_url:
        # ad-hoc hours are not the split's hours: never publish them under a split's name
        print(
            "--hours is for ad-hoc runs and can't be combined with --database-url", file=sys.stderr
        )
        return 2
    hours = args.hours or SPLITS[args.split]
    label = f"{args.split}, AD-HOC hours" if args.hours else args.split
    files = hour_files(Path(args.slice), hours)
    if not files:
        print(f"no recorded files for hours {hours} under {args.slice}", file=sys.stderr)
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
        for n_done, (kind, label, out) in enumerate(pool.imap_unordered(_work, tasks), 1):
            if kind == "base":
                baseline[label] = out
            else:
                trials.append(out)
            if n_done % 10 == 0 or n_done == len(tasks):
                log.info(
                    "%d/%d detector runs done (%.0fs)", n_done, len(tasks), time.monotonic() - t0
                )
    trials.sort(key=lambda t: (t.window, t.level, t.seed))
    summary = R.summarize(trials)
    details = {
        "synthetic": True,
        "what": "share of injected synthetic campaigns (did:plc:synth*) whose accounts the "
        "detector marks coordinated",
        "detector_version": DEFAULT.version,
        "eval_commit": _git_sha(),
        "hit_rule": f">= {R.MIN_COVERAGE:.0%} of the campaign's accounts in clusters with "
        "status 'coordinated'",
        "per_level": summary["per_level"],
        "recall_ci95": summary["overall"]["ci95"],
        "window_s": args.window_s,
        "hours_utc": hours,
        "windows": [label for label, _ in _WINDOWS],
        "windows_skipped": skipped,
        "repeats_per_level_per_window": args.repeats,
        "baseline_clusters_by_window": dict(sorted(baseline.items())),
        "trials": R.trial_dicts(trials),
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "runtime_s": round(time.monotonic() - t0),
    }
    print(R.format_table(summary, label))
    print(
        f"\n{len(_WINDOWS)} windows of {args.window_s:.0f}s from hours {', '.join(hours)} UTC; "
        f"skipped {len(skipped)}; detector {DEFAULT.version}; {details['runtime_s']}s"
    )
    overall = summary["overall"]
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "dataset": R.DATASET,
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
            args.database_url,
            R.DATASET,
            args.split,
            recall=overall["rate"],
            n=overall["n"],
            details=details,
        )
        print(f"wrote eval_runs id={rid} (dataset={R.DATASET}, split={args.split})")
    return 0


def cmd_copy(args) -> int:
    from eval.slices import copy_hours

    hours = args.hours or SPLITS[args.split]
    for p in copy_hours(Path(args.src), Path(args.dst), hours):
        print(p)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m eval")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("recall", help="recall on synthetic campaigns injected into a slice")
    r.add_argument("--slice", default=str(SLICE_ROOT), help="root of the copied recordings")
    r.add_argument("--split", required=True, choices=list(SPLITS))
    r.add_argument("--hours", nargs="+", help="override the split's UTC hours (YYYYMMDDHH)")
    r.add_argument("--levels", nargs="+", default=list(R.LEVELS), choices=list(R.LEVELS))
    r.add_argument("--repeats", type=int, default=5, help="campaigns per level per window")
    r.add_argument("--window-s", type=float, default=600.0)
    r.add_argument("--max-windows", type=int, default=0)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--database-url", help="write an eval_runs row here (nothing written if unset)")
    r.add_argument("--json-out", help="also write the result as JSON")
    c = sub.add_parser("copy-slices", help="copy recorded hours to the eval slice folder")
    c.add_argument("--src", default=str(RECORD_ROOT))
    c.add_argument("--dst", default=str(SLICE_ROOT))
    c.add_argument("--split", choices=list(SPLITS), default="dev")
    c.add_argument("--hours", nargs="+")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return {"recall": cmd_recall, "copy-slices": cmd_copy}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
