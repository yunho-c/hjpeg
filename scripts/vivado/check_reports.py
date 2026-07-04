#!/usr/bin/env python3
"""Check Vivado timing and utilization reports for hjpeg KV260 builds."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


WNS_HEADER_RE = re.compile(r"WNS\(ns\)")
NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")
UTIL_ROW_RE = re.compile(
    r"^\|\s*(?P<name>[A-Za-z0-9_./ +()-]+?)\s*\|\s*"
    r"(?P<used>\d+)\s*\|\s*(?P<fixed>\d+)\s*\|\s*"
    r"(?P<available>\d+)\s*\|\s*(?P<percent>[0-9.]+)\s*\|"
)


@dataclass(frozen=True)
class UtilizationRow:
    name: str
    used: int
    fixed: int
    available: int
    percent: float


def parse_wns(report: str) -> float:
    lines = report.splitlines()
    for index, line in enumerate(lines):
        if WNS_HEADER_RE.search(line):
            for candidate in lines[index + 1 : index + 6]:
                numbers = NUMBER_RE.findall(candidate)
                if numbers:
                    return float(numbers[0])

    match = re.search(r"\bWNS(?:\(ns\))?\s*[:=]\s*([-+]?\d+(?:\.\d+)?)", report)
    if match:
        return float(match.group(1))

    raise ValueError("could not find WNS in timing report")


def parse_utilization_rows(report: str) -> list[UtilizationRow]:
    rows = []
    for line in report.splitlines():
        match = UTIL_ROW_RE.match(line)
        if match is None:
            continue
        rows.append(
            UtilizationRow(
                name=" ".join(match.group("name").split()),
                used=int(match.group("used")),
                fixed=int(match.group("fixed")),
                available=int(match.group("available")),
                percent=float(match.group("percent")),
            )
        )
    return rows


def check_timing(path: Path, min_wns: float) -> list[str]:
    wns = parse_wns(path.read_text())
    if wns < min_wns:
        return [f"{path}: WNS {wns:.3f} ns is below required {min_wns:.3f} ns"]
    return []


def check_utilization(path: Path, max_percent: float) -> list[str]:
    rows = parse_utilization_rows(path.read_text())
    if not rows:
        return [f"{path}: no utilization rows found"]

    failures = []
    for row in rows:
        if row.available > 0 and row.percent > max_percent:
            failures.append(
                f"{path}: {row.name} utilization {row.percent:.2f}% exceeds {max_percent:.2f}%"
            )
    return failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="check Vivado timing and utilization reports")
    parser.add_argument(
        "--timing",
        type=Path,
        action="append",
        default=[],
        help="Vivado timing summary report to check; may be passed multiple times",
    )
    parser.add_argument(
        "--utilization",
        type=Path,
        action="append",
        default=[],
        help="Vivado utilization report to check; may be passed multiple times",
    )
    parser.add_argument("--min-wns", type=float, default=0.0)
    parser.add_argument("--max-utilization", type=float, default=90.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    failures = []

    for timing in args.timing:
        failures.extend(check_timing(timing, args.min_wns))
    for utilization in args.utilization:
        failures.extend(check_utilization(utilization, args.max_utilization))

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1

    checked = len(args.timing) + len(args.utilization)
    print(f"PASS: checked {checked} Vivado report(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
