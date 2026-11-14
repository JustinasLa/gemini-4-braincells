"""Eval CLI: `python -m eval <subcommand>`.

recall        recall on synthetic campaigns injected into recorded windows (eval/recall.py)
copy-slices   keep recorded Jetstream hours (the recorder prunes after 24 h) (eval/slices.py)
calibration   reliability of scores and verdicts on held-out injections (eval/calibration.py)
errors        FP/FN report from the latest calibration run (eval/errors.py)
audit-versions  read-only check that clusters and eval runs name a detector version
labels        sample|serve|score: hand labels on live clusters -> precision, FPR, kappa
organic       select|run: false-positive rate on organic hard cases (eval/organic.py)
takedowns     share of flagged accounts Bluesky later takes down vs. random accounts
"""

import argparse
import logging
import sys

from eval import calibration, errors, labels, organic, provenance, recall, slices, takedowns


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval")
    sub = p.add_subparsers(dest="command", required=True)
    recall.add_arguments(sub.add_parser("recall", help="recall on synthetic campaigns"))
    slices.add_arguments(sub.add_parser("copy-slices", help="copy recorded hours for eval"))
    calibration.add_arguments(sub.add_parser("calibration", help="held-out reliability data"))
    errors.add_arguments(sub.add_parser("errors", help="latest calibration FP/FN report"))
    provenance.add_arguments(sub.add_parser("audit-versions", help="read-only provenance audit"))
    labels.add_arguments(sub.add_parser("labels", help="hand labels on live clusters"))
    organic.add_arguments(sub.add_parser("organic", help="FPR on organic hard cases"))
    takedowns.add_arguments(sub.add_parser("takedowns", help="Bluesky moderation vs. our flags"))
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if a.command == "recall":
        return recall.main_args(a)
    if a.command == "copy-slices":
        return slices.main_args(a)
    if a.command == "calibration":
        return calibration.main_args(a)
    if a.command == "errors":
        return errors.main_args(a)
    if a.command == "audit-versions":
        return provenance.main_args(a)
    if a.command == "labels":
        return labels.main_args(a)
    if a.command == "organic":
        return organic.main_args(a)
    if a.command == "takedowns":
        return takedowns.main_args(a)
    return 2


if __name__ == "__main__":
    sys.exit(main())
