#!/usr/bin/env python3
"""Check Vivado timing and utilization reports for hjpeg KV260 builds."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path


TIMING_HEADER_RE = re.compile(r"WNS\(ns\)")
NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")
UTIL_ROW_RE = re.compile(
    r"^\|\s*(?P<name>[A-Za-z0-9_./ +()-]+?)\s*\|\s*"
    r"(?P<used>\d+)\s*\|\s*(?P<fixed>\d+)\s*\|\s*"
    r"(?:(?P<prohibited>\d+)\s*\|\s*)?"
    r"(?P<available>\d+)\s*\|\s*(?P<percent>[0-9.]+)\s*\|"
)
DRC_TABLE_ROW_RE = re.compile(
    r"^\|\s*(?P<rule>[A-Za-z0-9_.-]+)\s*\|\s*"
    r"(?P<severity>Critical Warning|Error|Warning|Advisory)\s*\|",
    re.IGNORECASE,
)
DRC_MESSAGE_RE = re.compile(r"\b(?P<severity>CRITICAL WARNING|ERROR):\s*(?P<message>.+)", re.IGNORECASE)
DRC_ZERO_RE = re.compile(
    r"\b(?:no drc violations found|violations found\s*[:=]\s*0|0\s+violations found)\b",
    re.IGNORECASE,
)
ROUTE_STATUS_RE = re.compile(
    r"^\s*(?:#\s*)?(?P<label>[A-Za-z0-9_ /-]*(?:unrouted|routing errors?|not completely routed)[A-Za-z0-9_ /-]*)\s*[:=]\s*(?P<count>\d+)\b",
    re.IGNORECASE,
)
IGNORED_UTILIZATION_ROWS = {"PS8"}
REQUIRED_EVIDENCE_CATEGORIES = (
    "artifacts",
    "timing",
    "utilization",
    "drc",
    "route_status",
    "clock_utilization",
)
REQUIRED_ARTIFACT_SUFFIXES = (".bit", ".xsa")


def finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("value must be finite")
    return parsed


def positive_float(value: str) -> float:
    parsed = finite_float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be finite and positive")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = finite_float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be finite and nonnegative")
    return parsed


@dataclass(frozen=True)
class UtilizationRow:
    name: str
    used: int
    fixed: int
    prohibited: int
    available: int
    percent: float


@dataclass(frozen=True)
class DrcViolation:
    rule: str
    severity: str
    message: str


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
                prohibited=int(match.group("prohibited") or 0),
                available=int(match.group("available")),
                percent=float(match.group("percent")),
            )
        )
    return rows


def parse_drc_violations(report: str) -> tuple[list[DrcViolation], bool]:
    violations = []
    saw_zero_summary = DRC_ZERO_RE.search(report) is not None
    for line in report.splitlines():
        table_match = DRC_TABLE_ROW_RE.match(line)
        if table_match is not None:
            violations.append(
                DrcViolation(
                    rule=table_match.group("rule"),
                    severity=" ".join(table_match.group("severity").lower().split()),
                    message=line.strip(),
                )
            )
            continue

        message_match = DRC_MESSAGE_RE.search(line)
        if message_match is not None:
            violations.append(
                DrcViolation(
                    rule="",
                    severity=" ".join(message_match.group("severity").lower().split()),
                    message=message_match.group("message").strip(),
                )
            )
    return violations, saw_zero_summary


def parse_route_status_counts(report: str) -> dict[str, int]:
    counts = {}
    for line in report.splitlines():
        match = ROUTE_STATUS_RE.match(line)
        if match is None:
            continue
        label = "_".join(match.group("label").lower().split())
        label = label.replace("-", "_").replace("/", "_")
        counts[label] = int(match.group("count"))
    return counts


def check_timing(path: Path, min_wns: float, min_whs: float = 0.0, check_whs: bool = False) -> list[str]:
    report = path.read_text()
    wns = parse_wns(report)
    whs = parse_whs(report)
    failures = []
    if wns < min_wns:
        failures.append(f"{path}: WNS {wns:.3f} ns is below required {min_wns:.3f} ns")
    if check_whs and whs < min_whs:
        failures.append(f"{path}: WHS {whs:.3f} ns is below required {min_whs:.3f} ns")
    return failures


def check_utilization(path: Path, max_percent: float) -> list[str]:
    rows = parse_utilization_rows(path.read_text())
    if not rows:
        return [f"{path}: no utilization rows found"]

    failures = []
    for row in rows:
        if row.name in IGNORED_UTILIZATION_ROWS:
            continue
        if row.available > 0 and row.percent > max_percent:
            failures.append(
                f"{path}: {row.name} utilization {row.percent:.2f}% exceeds {max_percent:.2f}%"
            )
    return failures


def check_drc(path: Path) -> list[str]:
    violations, saw_zero_summary = parse_drc_violations(path.read_text())
    blocking = [violation for violation in violations if violation.severity in {"error", "critical warning"}]
    if blocking:
        return [f"{path}: DRC {violation.severity}: {violation.message}" for violation in blocking]
    if not violations and not saw_zero_summary:
        return [f"{path}: could not find DRC violation summary"]
    return []


def check_route_status(path: Path) -> list[str]:
    counts = parse_route_status_counts(path.read_text())
    if not counts:
        return [f"{path}: could not find route status counts"]

    failures = []
    for label, count in counts.items():
        if count != 0:
            failures.append(f"{path}: route status {label} is {count}, expected 0")
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

    data = path.read_bytes()
    record = _file_record(path, data)
    if not data:
        record.update({"exists": True, "passed": False})
        return record, [f"{path}: artifact is empty"]
    record.update({"exists": True, "passed": True})
    return record, []


def evidence_file_record(path: Path, report_kind: str) -> tuple[dict[str, object], list[str]]:
    missing_record = missing_report_record(path, report_kind)
    if missing_record is not None:
        return missing_record

    data = path.read_bytes()
    record = _file_record(path, data)
    if not data:
        record.update({"exists": True, "passed": False})
        return record, [f"{path}: {report_kind} report is empty"]
    record.update({"exists": True, "passed": True})
    return record, []


def missing_report_record(path: Path, report_kind: str) -> tuple[dict[str, object], list[str]] | None:
    if not path.exists():
        return (
            {"path": str(path), "exists": False, "passed": False},
            [f"{path}: {report_kind} report not found"],
        )
    if not path.is_file():
        return (
            {"path": str(path), "exists": True, "passed": False},
            [f"{path}: {report_kind} report is not a file"],
        )
    return None


def timing_record(
    path: Path,
    min_wns: float,
    min_whs: float,
    check_whs: bool,
) -> tuple[dict[str, object], list[str]]:
    missing_record = missing_report_record(path, "timing")
    if missing_record is not None:
        record, failures = missing_record
        record.update(
            {
                "min_wns_ns": min_wns,
                "min_whs_ns": min_whs,
                "check_whs": check_whs,
            }
        )
        return record, failures

    report_bytes = path.read_bytes()
    report = report_bytes.decode(errors="replace")
    failures = []
    try:
        wns = parse_wns(report)
        whs = parse_whs(report)
    except ValueError as exc:
        record = _file_record(path, report_bytes)
        record.update(
            {
                "exists": True,
                "min_wns_ns": min_wns,
                "min_whs_ns": min_whs,
                "check_whs": check_whs,
                "passed": False,
            }
        )
        return record, [f"{path}: {exc}"]

    if wns < min_wns:
        failures.append(f"{path}: WNS {wns:.3f} ns is below required {min_wns:.3f} ns")
    if check_whs and whs < min_whs:
        failures.append(f"{path}: WHS {whs:.3f} ns is below required {min_whs:.3f} ns")
    record = _file_record(path, report_bytes)
    record.update(
        {
            "exists": True,
            "wns_ns": wns,
            "whs_ns": whs,
            "min_wns_ns": min_wns,
            "min_whs_ns": min_whs,
            "check_whs": check_whs,
            "passed": not failures,
        }
    )
    return record, failures


def utilization_record(path: Path, max_percent: float) -> tuple[dict[str, object], list[str]]:
    missing_record = missing_report_record(path, "utilization")
    if missing_record is not None:
        record, failures = missing_record
        record.update({"max_percent": max_percent, "rows": []})
        return record, failures

    report_bytes = path.read_bytes()
    report = report_bytes.decode(errors="replace")
    rows = parse_utilization_rows(report)
    failures = []
    if not rows:
        failures.append(f"{path}: no utilization rows found")

    row_records = []
    for row in rows:
        checked = row.name not in IGNORED_UTILIZATION_ROWS
        passed = not checked or row.available == 0 or row.percent <= max_percent
        row_record = {
            "name": row.name,
            "used": row.used,
            "fixed": row.fixed,
            "prohibited": row.prohibited,
            "available": row.available,
            "percent": row.percent,
            "checked": checked,
            "passed": passed,
        }
        row_records.append(row_record)
        if not row_record["passed"]:
            failures.append(
                f"{path}: {row.name} utilization {row.percent:.2f}% exceeds {max_percent:.2f}%"
            )

    record = _file_record(path, report_bytes)
    record.update(
        {
            "exists": True,
            "max_percent": max_percent,
            "rows": row_records,
            "passed": not failures,
        }
    )
    return record, failures


def drc_record(path: Path) -> tuple[dict[str, object], list[str]]:
    missing_record = missing_report_record(path, "DRC")
    if missing_record is not None:
        record, failures = missing_record
        record.update({"violations": []})
        return record, failures

    report_bytes = path.read_bytes()
    report = report_bytes.decode(errors="replace")
    violations, saw_zero_summary = parse_drc_violations(report)
    blocking = [violation for violation in violations if violation.severity in {"error", "critical warning"}]
    failures = [f"{path}: DRC {violation.severity}: {violation.message}" for violation in blocking]
    if not violations and not saw_zero_summary:
        failures.append(f"{path}: could not find DRC violation summary")

    record = _file_record(path, report_bytes)
    record.update(
        {
            "exists": True,
            "saw_zero_summary": saw_zero_summary,
            "violations": [
                {
                    "rule": violation.rule,
                    "severity": violation.severity,
                    "message": violation.message,
                    "blocking": violation.severity in {"error", "critical warning"},
                }
                for violation in violations
            ],
            "passed": not failures,
        }
    )
    return record, failures


def route_status_record(path: Path) -> tuple[dict[str, object], list[str]]:
    missing_record = missing_report_record(path, "route status")
    if missing_record is not None:
        record, failures = missing_record
        record.update({"counts": {}})
        return record, failures

    report_bytes = path.read_bytes()
    report = report_bytes.decode(errors="replace")
    counts = parse_route_status_counts(report)
    failures = []
    if not counts:
        failures.append(f"{path}: could not find route status counts")
    for label, count in counts.items():
        if count != 0:
            failures.append(f"{path}: route status {label} is {count}, expected 0")

    record = _file_record(path, report_bytes)
    record.update(
        {
            "exists": True,
            "counts": counts,
            "passed": not failures,
        }
    )
    return record, failures


def evidence_category_record(
    evidence_records: dict[str, list[dict[str, object]]]
) -> dict[str, object]:
    passing_counts = {
        category: sum(
            1
            for record in evidence_records.get(category, [])
            if record.get("passed") is True
        )
        for category in REQUIRED_EVIDENCE_CATEGORIES
    }
    failing_counts = {
        category: sum(
            1
            for record in evidence_records.get(category, [])
            if record.get("passed") is not True
        )
        for category in REQUIRED_EVIDENCE_CATEGORIES
    }
    present = {
        category: passing_counts[category] > 0
        for category in REQUIRED_EVIDENCE_CATEGORIES
    }
    present_categories = [
        category for category, is_present in present.items() if is_present
    ]
    failing_categories = [
        category for category in REQUIRED_EVIDENCE_CATEGORIES
        if failing_counts[category] > 0
    ]
    missing = [category for category, is_present in present.items() if not is_present]
    present_count = sum(1 for is_present in present.values() if is_present)
    return {
        "required_categories": list(REQUIRED_EVIDENCE_CATEGORIES),
        "required_category_count": len(REQUIRED_EVIDENCE_CATEGORIES),
        "present": present,
        "present_category_count": present_count,
        "missing_category_count": len(missing),
        "passing_counts": passing_counts,
        "failing_counts": failing_counts,
        "present_required_categories": present_categories,
        "failing_categories": failing_categories,
        "missing_required_categories": missing,
        "all_required_present": not missing,
    }


def artifact_suffix_record(artifact_records: list[dict[str, object]]) -> dict[str, object]:
    suffix_counts: dict[str, int] = {}
    passing_suffix_counts: dict[str, int] = {}
    failing_suffix_counts: dict[str, int] = {}
    present = {suffix: False for suffix in REQUIRED_ARTIFACT_SUFFIXES}
    for record in artifact_records:
        suffix = Path(str(record.get("path", ""))).suffix.lower()
        if not suffix:
            continue
        suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1
        if record.get("passed") is True and suffix in present:
            passing_suffix_counts[suffix] = passing_suffix_counts.get(suffix, 0) + 1
            present[suffix] = True
        elif record.get("passed") is not True and suffix in present:
            failing_suffix_counts[suffix] = failing_suffix_counts.get(suffix, 0) + 1

    present_suffixes = [
        suffix for suffix, is_present in present.items() if is_present
    ]
    failing_suffixes = [
        suffix for suffix in REQUIRED_ARTIFACT_SUFFIXES
        if failing_suffix_counts.get(suffix, 0) > 0
    ]
    missing = [suffix for suffix, is_present in present.items() if not is_present]
    present_count = sum(1 for is_present in present.values() if is_present)
    return {
        "required_suffixes": list(REQUIRED_ARTIFACT_SUFFIXES),
        "required_suffix_count": len(REQUIRED_ARTIFACT_SUFFIXES),
        "suffix_counts": suffix_counts,
        "passing_suffix_counts": passing_suffix_counts,
        "failing_suffix_counts": failing_suffix_counts,
        "required_suffixes_present": present,
        "present_suffix_count": present_count,
        "missing_suffix_count": len(missing),
        "present_required_suffixes": present_suffixes,
        "failing_required_suffixes": failing_suffixes,
        "missing_required_suffixes": missing,
        "all_required_suffixes_present": not missing,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="check Vivado timing and utilization reports")
    parser.add_argument(
        "--timing",
        type=Path,
        action="append",
        default=[],
        help="Vivado timing summary report to check for setup WNS; may be passed multiple times",
    )
    parser.add_argument(
        "--hold-timing",
        type=Path,
        action="append",
        default=[],
        help="Timing summary report that must also pass hold WHS; may be passed multiple times",
    )
    parser.add_argument(
        "--utilization",
        type=Path,
        action="append",
        default=[],
        help="Vivado utilization report to check; may be passed multiple times",
    )
    parser.add_argument(
        "--drc",
        type=Path,
        action="append",
        default=[],
        help="Vivado DRC report to check for Error or Critical Warning violations; may be passed multiple times",
    )
    parser.add_argument(
        "--route-status",
        type=Path,
        action="append",
        default=[],
        help="Vivado route status report to check for unrouted nets or routing errors; may be passed multiple times",
    )
    parser.add_argument(
        "--clock-utilization",
        type=Path,
        action="append",
        default=[],
        help="Vivado clock utilization report to require and hash in evidence; may be passed multiple times",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        action="append",
        default=[],
        help="Generated artifact to hash in evidence; may be passed multiple times",
    )
    parser.add_argument("--min-wns", type=finite_float, default=0.0)
    parser.add_argument("--min-whs", type=finite_float, default=0.0)
    parser.add_argument("--max-utilization", type=nonnegative_float, default=90.0)
    parser.add_argument(
        "--clock-period-ns",
        type=positive_float,
        default=10.0,
        help="target clock period to record in JSON evidence, default 10.0 ns",
    )
    parser.add_argument(
        "--require-complete-evidence",
        action="store_true",
        help="fail unless all required report categories and .bit/.xsa artifacts passed",
    )
    parser.add_argument("--json", action="store_true", help="print parsed report evidence as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    failures = []
    artifact_records = []
    timing_records = []
    utilization_records = []
    drc_records = []
    route_status_records = []
    clock_utilization_records = []

    for artifact in args.artifact:
        record, record_failures = artifact_record(artifact)
        artifact_records.append(record)
        failures.extend(record_failures)
    timing_paths = []
    hold_timing_paths = {str(path) for path in args.hold_timing}
    seen_timing_paths = set()
    for timing in [*args.timing, *args.hold_timing]:
        key = str(timing)
        if key in seen_timing_paths:
            continue
        seen_timing_paths.add(key)
        timing_paths.append(timing)

    for timing in timing_paths:
        record, record_failures = timing_record(
            timing,
            args.min_wns,
            args.min_whs,
            str(timing) in hold_timing_paths,
        )
        timing_records.append(record)
        failures.extend(record_failures)
    for utilization in args.utilization:
        record, record_failures = utilization_record(utilization, args.max_utilization)
        utilization_records.append(record)
        failures.extend(record_failures)
    for drc in args.drc:
        record, record_failures = drc_record(drc)
        drc_records.append(record)
        failures.extend(record_failures)
    for route_status in args.route_status:
        record, record_failures = route_status_record(route_status)
        route_status_records.append(record)
        failures.extend(record_failures)
    for clock_utilization in args.clock_utilization:
        record, record_failures = evidence_file_record(clock_utilization, "clock utilization")
        clock_utilization_records.append(record)
        failures.extend(record_failures)

    checked = (
        len(args.artifact)
        + len(timing_paths)
        + len(args.utilization)
        + len(args.drc)
        + len(args.route_status)
        + len(args.clock_utilization)
    )
    checked_counts = {
        "artifacts": len(args.artifact),
        "timing": len(timing_paths),
        "utilization": len(args.utilization),
        "drc": len(args.drc),
        "route_status": len(args.route_status),
        "clock_utilization": len(args.clock_utilization),
    }
    checked_records = [
        *artifact_records,
        *timing_records,
        *utilization_records,
        *drc_records,
        *route_status_records,
        *clock_utilization_records,
    ]
    passed_count = sum(1 for record in checked_records if record.get("passed") is True)
    failed_count = len(checked_records) - passed_count
    checked_paths = [str(record.get("path")) for record in checked_records]
    passed_paths = [
        str(record.get("path"))
        for record in checked_records
        if record.get("passed") is True
    ]
    failed_paths = [
        str(record.get("path"))
        for record in checked_records
        if record.get("passed") is not True
    ]
    evidence_categories = evidence_category_record(
        {
            "artifacts": artifact_records,
            "timing": timing_records,
            "utilization": utilization_records,
            "drc": drc_records,
            "route_status": route_status_records,
            "clock_utilization": clock_utilization_records,
        }
    )
    artifact_suffixes = artifact_suffix_record(artifact_records)
    complete_vivado_flow_evidence = bool(
        evidence_categories["all_required_present"]
        and artifact_suffixes["all_required_suffixes_present"]
    )
    missing_categories = evidence_categories["missing_required_categories"]
    missing_suffixes = artifact_suffixes["missing_required_suffixes"]
    failing_categories = evidence_categories["failing_categories"]
    failing_suffixes = artifact_suffixes["failing_required_suffixes"]
    if args.require_complete_evidence and not complete_vivado_flow_evidence:
        if missing_categories:
            failures.append(
                "complete Vivado flow evidence missing required categories: "
                + ", ".join(str(category) for category in missing_categories)
            )
        if missing_suffixes:
            failures.append(
                "complete Vivado flow evidence missing required artifact suffixes: "
                + ", ".join(str(suffix) for suffix in missing_suffixes)
            )

    if args.json:
        arguments = {
            "artifacts": [str(path) for path in args.artifact],
            "timing": [str(path) for path in args.timing],
            "hold_timing": [str(path) for path in args.hold_timing],
            "utilization": [str(path) for path in args.utilization],
            "drc": [str(path) for path in args.drc],
            "route_status": [str(path) for path in args.route_status],
            "clock_utilization": [str(path) for path in args.clock_utilization],
            "min_wns": args.min_wns,
            "min_whs": args.min_whs,
            "max_utilization": args.max_utilization,
            "clock_period_ns": args.clock_period_ns,
            "require_complete_evidence": args.require_complete_evidence,
        }
        print(
            json.dumps(
                {
                    "passed": not failures,
                    "failures": failures,
                    "failure_count": len(failures),
                    "checked_count": checked,
                    "passed_count": passed_count,
                    "failed_count": failed_count,
                    "checked_paths": checked_paths,
                    "passed_paths": passed_paths,
                    "failed_paths": failed_paths,
                    "checked_counts": checked_counts,
                    "evidence_categories": evidence_categories,
                    "artifact_suffixes": artifact_suffixes,
                    "complete_vivado_flow_evidence": complete_vivado_flow_evidence,
                    "complete_vivado_flow_evidence_required": (
                        args.require_complete_evidence
                    ),
                    "complete_vivado_flow_evidence_missing_categories": (
                        missing_categories
                    ),
                    "complete_vivado_flow_evidence_missing_suffixes": (
                        missing_suffixes
                    ),
                    "complete_vivado_flow_evidence_failing_categories": (
                        failing_categories
                    ),
                    "complete_vivado_flow_evidence_failing_suffixes": (
                        failing_suffixes
                    ),
                    "arguments": arguments,
                    "clock_period_ns": args.clock_period_ns,
                    "clock_frequency_mhz": 1000.0 / args.clock_period_ns,
                    "artifacts": artifact_records,
                    "timing": timing_records,
                    "utilization": utilization_records,
                    "drc": drc_records,
                    "route_status": route_status_records,
                    "clock_utilization": clock_utilization_records,
                },
                sort_keys=True,
            )
        )

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1

    if not args.json:
        print(f"PASS: checked {checked} Vivado report(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
