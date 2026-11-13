"""Eval CLI: `python -m eval <subcommand>`.

recall        recall on synthetic campaigns injected into recorded windows (eval/recall.py)
copy-slices   keep recorded Jetstream hours (the recorder prunes after 24 h) (eval/slices.py)
"""

import argparse
import logging
import sys

from eval import calibration, errors, provenance, recall, slices


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval")
    sub = p.add_subparsers(dest="command", required=True)
    recall.add_arguments(sub.add_parser("recall", help="recall on synthetic campaigns"))
    slices.add_arguments(sub.add_parser("copy-slices", help="copy recorded hours for eval"))
    calibration.add_arguments(sub.add_parser("calibration", help="held-out reliability data"))
    errors.add_arguments(sub.add_parser("errors", help="latest calibration FP/FN report"))
    provenance.add_arguments(sub.add_parser("audit-versions", help="read-only provenance audit"))
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
    return 2


if __name__ == "__main__":
    sys.exit(main())
