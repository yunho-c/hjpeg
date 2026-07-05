#!/usr/bin/env python3
"""Check Vivado timing and utilization reports for hjpeg KV260 builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


TIMING_HEADER_RE = re.compile(r"WNS\(ns\)")
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


def parse_timing_metric(report: str, metric: str) -> float:
    match = re.search(
        rf"\b{re.escape(metric)}(?:\(ns\))?\s*[:=]\s*([-+]?\d+(?:\.\d+)?)",
        report,
    )
    if match:
        return float(match.group(1))

    lines = report.splitlines()
    for index, line in enumerate(lines):
        if TIMING_HEADER_RE.search(line):
            has_metric = re.search(rf"\b{re.escape(metric)}\(ns\)", line) is not None
            if not has_metric:
                continue
            for candidate in lines[index + 1 : index + 6]:
                numbers = NUMBER_RE.findall(candidate)
                if not numbers:
                    continue
                if metric == "WNS" and len(numbers) >= 1:
                    return float(numbers[0])
                if metric == "WHS" and len(numbers) >= 5:
                    return float(numbers[4])

    raise ValueError(f"could not find {metric} in timing report")


def parse_wns(report: str) -> float:
    return parse_timing_metric(report, "WNS")


def parse_whs(report: str) -> float:
    return parse_timing_metric(report, "WHS")


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


def check_timing(path: Path, min_wns: float, min_whs: float = 0.0) -> list[str]:
    report = path.read_text()
    wns = parse_wns(report)
    whs = parse_whs(report)
    failures = []
    if wns < min_wns:
        failures.append(f"{path}: WNS {wns:.3f} ns is below required {min_wns:.3f} ns")
    if whs < min_whs:
        failures.append(f"{path}: WHS {whs:.3f} ns is below required {min_whs:.3f} ns")
    return failures


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


def _file_record(path: Path, data: bytes) -> dict[str, object]:
    return {
        "path": str(path),
        "byte_length": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def artifact_record(path: Path) -> tuple[dict[str, object], list[str]]:
    if not path.exists():
        return {"path": str(path), "exists": False, "passed": False}, [f"{path}: artifact not found"]
    if not path.is_file():
        return {"path": str(path), "exists": True, "passed": False}, [f"{path}: artifact is not a file"]

    record = _file_record(path, path.read_bytes())
    record.update({"exists": True, "passed": True})
    return record, []


def timing_record(path: Path, min_wns: float, min_whs: float) -> tuple[dict[str, object], list[str]]:
    report_bytes = path.read_bytes()
    report = report_bytes.decode(errors="replace")
    wns = parse_wns(report)
    whs = parse_whs(report)
    failures = []
    if wns < min_wns:
        failures.append(f"{path}: WNS {wns:.3f} ns is below required {min_wns:.3f} ns")
    if whs < min_whs:
        failures.append(f"{path}: WHS {whs:.3f} ns is below required {min_whs:.3f} ns")
    record = _file_record(path, report_bytes)
    record.update(
        {
            "wns_ns": wns,
            "whs_ns": whs,
            "min_wns_ns": min_wns,
            "min_whs_ns": min_whs,
            "passed": not failures,
        }
    )
    return record, failures


def utilization_record(path: Path, max_percent: float) -> tuple[dict[str, object], list[str]]:
    report_bytes = path.read_bytes()
    report = report_bytes.decode(errors="replace")
    rows = parse_utilization_rows(report)
    failures = []
    if not rows:
        failures.append(f"{path}: no utilization rows found")

    row_records = []
    for row in rows:
        row_record = {
            "name": row.name,
            "used": row.used,
            "fixed": row.fixed,
            "available": row.available,
            "percent": row.percent,
            "passed": row.available == 0 or row.percent <= max_percent,
        }
        row_records.append(row_record)
        if not row_record["passed"]:
            failures.append(
                f"{path}: {row.name} utilization {row.percent:.2f}% exceeds {max_percent:.2f}%"
            )

    record = _file_record(path, report_bytes)
    record.update(
        {
            "max_percent": max_percent,
            "rows": row_records,
            "passed": not failures,
        }
    )
    return record, failures


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
    parser.add_argument(
        "--artifact",
        type=Path,
        action="append",
        default=[],
        help="Generated artifact to hash in evidence; may be passed multiple times",
    )
    parser.add_argument("--min-wns", type=float, default=0.0)
    parser.add_argument("--min-whs", type=float, default=0.0)
    parser.add_argument("--max-utilization", type=float, default=90.0)
    parser.add_argument("--json", action="store_true", help="print parsed report evidence as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    failures = []
    artifact_records = []
    timing_records = []
    utilization_records = []

    for artifact in args.artifact:
        record, record_failures = artifact_record(artifact)
        artifact_records.append(record)
        failures.extend(record_failures)
    for timing in args.timing:
        record, record_failures = timing_record(timing, args.min_wns, args.min_whs)
        timing_records.append(record)
        failures.extend(record_failures)
    for utilization in args.utilization:
        record, record_failures = utilization_record(utilization, args.max_utilization)
        utilization_records.append(record)
        failures.extend(record_failures)

    if args.json:
        print(
            json.dumps(
                {
                    "passed": not failures,
                    "failures": failures,
                    "artifacts": artifact_records,
                    "timing": timing_records,
                    "utilization": utilization_records,
                },
                sort_keys=True,
            )
        )

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1

    checked = len(args.artifact) + len(args.timing) + len(args.utilization)
    if not args.json:
        print(f"PASS: checked {checked} Vivado report(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
