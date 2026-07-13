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
    r"^\s*(?:#\s*)?"
    r"(?P<label>[A-Za-z0-9_ /-]*(?:unrouted|routing errors?|not completely routed|routable nets|fully routed nets)[A-Za-z0-9_ /-]*)"
    r"\s*(?:\.{2,})?\s*[:=]\s*(?P<count>\d+)\b\s*:?",
    re.IGNORECASE,
)
FLOORPLAN_COUNT_RE = re.compile(
    r"^\s*(?P<label>Pblock Count|Placed Cell Count)\s*:\s*(?P<count>\d+)\s*$",
    re.IGNORECASE,
)
HEX_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F_]+")
# These rows are useful evidence but are not independent programmable-logic
# resource budgets. PS8 is a hard system block. CLB is the number of physical
# sites touched by placement and double-counts the LUT/register resources that
# are checked independently; timing-driven placement may deliberately spread
# otherwise lightly used CLBs.
INFORMATIONAL_UTILIZATION_ROWS = {"CLB", "PS8"}
REQUIRED_ADDRESS_MAP_INTERFACES = (
    ("hjpeg_0", "s_axi_lite"),
    ("axi_dma_0", "s_axi_lite"),
)
REQUIRED_EVIDENCE_CATEGORIES = (
    "artifacts",
    "address_map",
    "timing",
    "utilization",
    "drc",
    "route_status",
    "clock_utilization",
    "floorplan",
)
REQUIRED_ARTIFACT_SUFFIXES = (".bit", ".xsa", ".dcp")
REQUIRED_ARTIFACT_FILENAMES = ("hjpeg_kv260.bit", "hjpeg_kv260.xsa", "post_impl.dcp")
REQUIRED_ADDRESS_MAP_FILENAMES = ("hjpeg_kv260_address_map.rpt",)
REQUIRED_REPORT_FILENAMES = {
    "timing": ("post_synth_timing_summary.rpt", "post_impl_timing_summary.rpt"),
    "utilization": ("post_synth_utilization.rpt", "post_impl_utilization.rpt"),
    "drc": ("post_impl_drc.rpt",),
    "route_status": ("post_impl_route_status.rpt",),
    "clock_utilization": ("post_impl_clock_utilization.rpt",),
    "floorplan": ("post_impl_floorplan.rpt",),
}
REQUIRED_HOLD_TIMING_FILENAMES = ("post_impl_timing_summary.rpt",)
REQUIRED_ROUTE_STATUS_COUNTS = (
    "number_of_unrouted_nets",
    "number_of_nets_with_routing_errors",
)


def finite_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be finite") from None
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


def strict_json_dumps(value: object, **kwargs: object) -> str:
    return json.dumps(value, allow_nan=False, **kwargs)


def clock_target_record(clock_period_ns: float) -> dict[str, object]:
    clock_period_finite = math.isfinite(clock_period_ns)
    clock_period_positive = clock_period_ns > 0.0
    if clock_period_finite and clock_period_ns != 0.0:
        clock_frequency_mhz = 1000.0 / clock_period_ns
    else:
        clock_frequency_mhz = None
    clock_frequency_finite = (
        clock_frequency_mhz is not None and math.isfinite(clock_frequency_mhz)
    )
    clock_frequency_positive = (
        clock_frequency_mhz is not None and clock_frequency_mhz > 0.0
    )
    period_frequency_match = (
        clock_period_finite
        and clock_period_positive
        and clock_frequency_finite
        and clock_frequency_positive
        and clock_frequency_mhz is not None
        and math.isclose(
            clock_period_ns * clock_frequency_mhz,
            1000.0,
            rel_tol=1e-12,
            abs_tol=1e-9,
        )
    )
    valid = bool(
        clock_period_finite
        and clock_period_positive
        and clock_frequency_finite
        and clock_frequency_positive
        and period_frequency_match
    )
    return {
        "clock_period_ns": clock_period_ns if clock_period_finite else None,
        "clock_frequency_mhz": clock_frequency_mhz,
        "clock_period_finite": clock_period_finite,
        "clock_period_positive": clock_period_positive,
        "clock_frequency_finite": clock_frequency_finite,
        "clock_frequency_positive": clock_frequency_positive,
        "period_frequency_match": period_frequency_match,
        "valid": valid,
    }


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


@dataclass(frozen=True)
class AddressMapEntry:
    interface: str
    base_address: int
    high_address: int | None


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
    raw_counts = {}
    counts = {}
    for line in report.splitlines():
        match = ROUTE_STATUS_RE.match(line)
        if match is None:
            continue
        label = "_".join(match.group("label").lower().split())
        label = label.replace("-", "_").replace("/", "_")
        if label.startswith("of_"):
            label = f"number_{label}"
        count = int(match.group("count"))
        raw_counts[label] = count

        if label in {
            "number_of_unrouted_nets",
            "number_of_nets_with_routing_errors",
        }:
            counts[label] = count
        elif label == "number_of_nets_not_completely_routed":
            counts["number_of_unrouted_nets"] = count

    if (
        "number_of_unrouted_nets" not in counts
        and "number_of_routable_nets" in raw_counts
        and "number_of_fully_routed_nets" in raw_counts
    ):
        counts["number_of_unrouted_nets"] = (
            raw_counts["number_of_routable_nets"] - raw_counts["number_of_fully_routed_nets"]
        )
    return counts


def parse_floorplan_counts(report: str) -> dict[str, int]:
    counts = {}
    for line in report.splitlines():
        match = FLOORPLAN_COUNT_RE.match(line)
        if match is None:
            continue
        label = "_".join(match.group("label").lower().split())
        counts[label] = int(match.group("count"))
    return counts


def normalize_address_map_text(text: str) -> str:
    return text.lower().replace("\\", "/")


def parse_address_map_entries(report: str) -> list[AddressMapEntry]:
    entries = []
    for line in report.splitlines():
        normalized_line = normalize_address_map_text(line)
        for component, interface in REQUIRED_ADDRESS_MAP_INTERFACES:
            interface_name = f"{component}/{interface}"
            if component not in normalized_line or interface not in normalized_line:
                continue
            addresses = [
                int(address.replace("_", ""), 16)
                for address in HEX_ADDRESS_RE.findall(line)
            ]
            if not addresses:
                continue
            entries.append(
                AddressMapEntry(
                    interface=interface_name,
                    base_address=addresses[0],
                    high_address=addresses[1] if len(addresses) > 1 else None,
                )
            )
    return entries


def check_address_map(path: Path) -> list[str]:
    missing_record = missing_report_record(path, "address map")
    if missing_record is not None:
        _, failures = missing_record
        return failures

    report = path.read_text()
    if not report:
        return [f"{path}: address map report is empty"]

    entries = parse_address_map_entries(report)
    _, failures = address_map_validation(entries, path)
    return failures


def address_map_validation(
    entries: list[AddressMapEntry], path: Path
) -> tuple[dict[str, object], list[str]]:
    required_interfaces = [
        f"{component}/{interface}"
        for component, interface in REQUIRED_ADDRESS_MAP_INTERFACES
    ]
    entries_by_interface = {
        interface_name: [
            entry for entry in entries if entry.interface == interface_name
        ]
        for interface_name in required_interfaces
    }
    missing_interfaces = [
        interface_name
        for interface_name, interface_entries in entries_by_interface.items()
        if not interface_entries
    ]
    duplicate_interfaces = [
        interface_name
        for interface_name, interface_entries in entries_by_interface.items()
        if len(interface_entries) > 1
    ]
    invalid_range_interfaces = [
        entry.interface
        for entry in entries
        if entry.high_address is not None and entry.high_address < entry.base_address
    ]

    range_overlaps = []
    checked_entries = [
        entry
        for entry in entries
        if entry.interface in required_interfaces
        and entry.high_address is not None
        and entry.high_address >= entry.base_address
    ]
    for index, first in enumerate(checked_entries):
        for second in checked_entries[index + 1 :]:
            if first.interface == second.interface:
                continue
            if first.base_address <= second.high_address and second.base_address <= first.high_address:
                range_overlaps.append(
                    {
                        "first_interface": first.interface,
                        "first_base_address_hex": f"0x{first.base_address:08x}",
                        "first_high_address_hex": f"0x{first.high_address:08x}",
                        "second_interface": second.interface,
                        "second_base_address_hex": f"0x{second.base_address:08x}",
                        "second_high_address_hex": f"0x{second.high_address:08x}",
                    }
                )

    failures = [
        f"{path}: address map missing {interface_name} base address"
        for interface_name in missing_interfaces
    ]
    failures.extend(
        f"{path}: address map has duplicate {interface_name} base addresses"
        for interface_name in duplicate_interfaces
    )
    failures.extend(
        f"{path}: address map {entry.interface} high address "
        f"0x{entry.high_address:08x} is below base address 0x{entry.base_address:08x}"
        for entry in entries
        if entry.high_address is not None
        and entry.high_address < entry.base_address
    )
    failures.extend(
        f"{path}: address map {overlap['first_interface']} range "
        f"{overlap['first_base_address_hex']}-{overlap['first_high_address_hex']} "
        f"overlaps {overlap['second_interface']} range "
        f"{overlap['second_base_address_hex']}-{overlap['second_high_address_hex']}"
        for overlap in range_overlaps
    )

    present_interfaces = sorted(
        interface_name
        for interface_name, interface_entries in entries_by_interface.items()
        if interface_entries
    )
    return (
        {
            "required_interfaces": required_interfaces,
            "present_interfaces": present_interfaces,
            "missing_interfaces": missing_interfaces,
            "duplicate_interfaces": duplicate_interfaces,
            "invalid_range_interfaces": invalid_range_interfaces,
            "range_overlaps": range_overlaps,
        },
        failures,
    )


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
        if row.name in INFORMATIONAL_UTILIZATION_ROWS:
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
    for label in REQUIRED_ROUTE_STATUS_COUNTS:
        if label not in counts:
            failures.append(f"{path}: route status missing {label} count")
    for label, count in counts.items():
        if count != 0:
            failures.append(f"{path}: route status {label} is {count}, expected 0")
    return failures


def check_floorplan(path: Path) -> list[str]:
    record, failures = floorplan_record(path)
    return failures


def _file_record(path: Path, data: bytes) -> dict[str, object]:
    return {
        "path": str(path),
        "path_resolved": str(path.resolve(strict=False)),
        "byte_length": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _path_record(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "path_resolved": str(path.resolve(strict=False)),
    }


def artifact_record(path: Path) -> tuple[dict[str, object], list[str]]:
    if not path.exists():
        record = _path_record(path)
        record.update({"exists": False, "passed": False})
        return record, [f"{path}: artifact not found"]
    if not path.is_file():
        record = _path_record(path)
        record.update({"exists": True, "passed": False})
        return record, [f"{path}: artifact is not a file"]

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
        record = _path_record(path)
        record.update({"exists": False, "passed": False})
        return (
            record,
            [f"{path}: {report_kind} report not found"],
        )
    if not path.is_file():
        record = _path_record(path)
        record.update({"exists": True, "passed": False})
        return (
            record,
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
        checked = row.name not in INFORMATIONAL_UTILIZATION_ROWS
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
        record.update(
            {
                "required_counts": list(REQUIRED_ROUTE_STATUS_COUNTS),
                "counts": {},
                "missing_counts": list(REQUIRED_ROUTE_STATUS_COUNTS),
            }
        )
        return record, failures

    report_bytes = path.read_bytes()
    report = report_bytes.decode(errors="replace")
    counts = parse_route_status_counts(report)
    failures = []
    if not counts:
        failures.append(f"{path}: could not find route status counts")
    missing_counts = [label for label in REQUIRED_ROUTE_STATUS_COUNTS if label not in counts]
    for label in missing_counts:
        failures.append(f"{path}: route status missing {label} count")
    for label, count in counts.items():
        if count != 0:
            failures.append(f"{path}: route status {label} is {count}, expected 0")

    record = _file_record(path, report_bytes)
    record.update(
        {
            "exists": True,
            "required_counts": list(REQUIRED_ROUTE_STATUS_COUNTS),
            "counts": counts,
            "missing_counts": missing_counts,
            "passed": not failures,
        }
    )
    return record, failures


def floorplan_record(path: Path) -> tuple[dict[str, object], list[str]]:
    missing_record = missing_report_record(path, "floorplan")
    if missing_record is not None:
        record, failures = missing_record
        record.update(
            {
                "counts": {},
                "pblock_count": None,
                "placed_cell_count": None,
            }
        )
        return record, failures

    report_bytes = path.read_bytes()
    report = report_bytes.decode(errors="replace")
    counts = parse_floorplan_counts(report)
    pblock_count = counts.get("pblock_count")
    placed_cell_count = counts.get("placed_cell_count")
    failures = []
    if not report_bytes:
        failures.append(f"{path}: floorplan report is empty")
    if pblock_count is None:
        failures.append(f"{path}: floorplan report missing Pblock Count")
    if placed_cell_count is None:
        failures.append(f"{path}: floorplan report missing Placed Cell Count")
    elif placed_cell_count <= 0:
        failures.append(
            f"{path}: floorplan placed cell count is {placed_cell_count}, expected positive"
        )

    record = _file_record(path, report_bytes)
    record.update(
        {
            "exists": True,
            "counts": counts,
            "pblock_count": pblock_count,
            "placed_cell_count": placed_cell_count,
            "passed": not failures,
        }
    )
    return record, failures


def address_map_record(path: Path) -> tuple[dict[str, object], list[str]]:
    missing_record = missing_report_record(path, "address map")
    if missing_record is not None:
        record, failures = missing_record
        record.update(
            {
                "required_interfaces": [
                    f"{component}/{interface}"
                    for component, interface in REQUIRED_ADDRESS_MAP_INTERFACES
                ],
                "entries": [],
            }
        )
        return record, failures

    report_bytes = path.read_bytes()
    report = report_bytes.decode(errors="replace")
    entries = parse_address_map_entries(report)
    validation, failures = address_map_validation(entries, path)
    if not report_bytes:
        failures.append(f"{path}: address map report is empty")

    entry_records = []
    for entry in entries:
        high_address_valid = (
            entry.high_address is None or entry.high_address >= entry.base_address
        )
        entry_records.append(
            {
                "interface": entry.interface,
                "base_address": entry.base_address,
                "base_address_hex": f"0x{entry.base_address:08x}",
                "high_address": entry.high_address,
                "high_address_hex": (
                    f"0x{entry.high_address:08x}"
                    if entry.high_address is not None
                    else None
                ),
                "high_address_valid": high_address_valid,
                "aperture_bytes": (
                    entry.high_address - entry.base_address + 1
                    if entry.high_address is not None and high_address_valid
                    else None
                ),
            }
        )

    record = _file_record(path, report_bytes)
    record.update(
        {
            "exists": True,
            **validation,
            "entries": entry_records,
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


def artifact_filename_record(artifact_records: list[dict[str, object]]) -> dict[str, object]:
    filename_counts: dict[str, int] = {}
    passing_filename_counts: dict[str, int] = {}
    failing_filename_counts: dict[str, int] = {}
    present = {filename: False for filename in REQUIRED_ARTIFACT_FILENAMES}
    for record in artifact_records:
        filename = Path(str(record.get("path", ""))).name
        if not filename:
            continue
        filename_counts[filename] = filename_counts.get(filename, 0) + 1
        if record.get("passed") is True and filename in present:
            passing_filename_counts[filename] = passing_filename_counts.get(filename, 0) + 1
            present[filename] = True
        elif record.get("passed") is not True and filename in present:
            failing_filename_counts[filename] = failing_filename_counts.get(filename, 0) + 1

    present_filenames = [
        filename for filename, is_present in present.items() if is_present
    ]
    failing_filenames = [
        filename for filename in REQUIRED_ARTIFACT_FILENAMES
        if failing_filename_counts.get(filename, 0) > 0
    ]
    missing = [filename for filename, is_present in present.items() if not is_present]
    present_count = sum(1 for is_present in present.values() if is_present)
    return {
        "required_filenames": list(REQUIRED_ARTIFACT_FILENAMES),
        "required_filename_count": len(REQUIRED_ARTIFACT_FILENAMES),
        "filename_counts": filename_counts,
        "passing_filename_counts": passing_filename_counts,
        "failing_filename_counts": failing_filename_counts,
        "required_filenames_present": present,
        "present_filename_count": present_count,
        "missing_filename_count": len(missing),
        "present_required_filenames": present_filenames,
        "failing_required_filenames": failing_filenames,
        "missing_required_filenames": missing,
        "all_required_filenames_present": not missing,
    }


def required_filename_record(
    records: list[dict[str, object]],
    required_filenames: tuple[str, ...],
    label: str,
) -> dict[str, object]:
    filename_counts: dict[str, int] = {}
    passing_filename_counts: dict[str, int] = {}
    failing_filename_counts: dict[str, int] = {}
    present = {filename: False for filename in required_filenames}
    for record in records:
        filename = Path(str(record.get("path", ""))).name
        if not filename:
            continue
        filename_counts[filename] = filename_counts.get(filename, 0) + 1
        if record.get("passed") is True and filename in present:
            passing_filename_counts[filename] = passing_filename_counts.get(filename, 0) + 1
            present[filename] = True
        elif record.get("passed") is not True and filename in present:
            failing_filename_counts[filename] = failing_filename_counts.get(filename, 0) + 1

    present_filenames = [
        filename for filename, is_present in present.items() if is_present
    ]
    failing_filenames = [
        filename for filename in required_filenames
        if failing_filename_counts.get(filename, 0) > 0
    ]
    missing = [filename for filename, is_present in present.items() if not is_present]
    present_count = sum(1 for is_present in present.values() if is_present)
    return {
        "label": label,
        "required_filenames": list(required_filenames),
        "required_filename_count": len(required_filenames),
        "filename_counts": filename_counts,
        "passing_filename_counts": passing_filename_counts,
        "failing_filename_counts": failing_filename_counts,
        "required_filenames_present": present,
        "present_filename_count": present_count,
        "missing_filename_count": len(missing),
        "present_required_filenames": present_filenames,
        "failing_required_filenames": failing_filenames,
        "missing_required_filenames": missing,
        "all_required_filenames_present": not missing,
    }


def report_filename_records(
    records_by_category: dict[str, list[dict[str, object]]]
) -> dict[str, dict[str, object]]:
    return {
        category: required_filename_record(
            records_by_category.get(category, []),
            required_filenames,
            category,
        )
        for category, required_filenames in REQUIRED_REPORT_FILENAMES.items()
    }


def missing_required_filenames_by_category(
    filename_records: dict[str, dict[str, object]]
) -> dict[str, list[str]]:
    return {
        category: [str(filename) for filename in record.get("missing_required_filenames", [])]
        for category, record in filename_records.items()
        if record.get("missing_required_filenames")
    }


def failing_required_filenames_by_category(
    filename_records: dict[str, dict[str, object]]
) -> dict[str, list[str]]:
    return {
        category: [str(filename) for filename in record.get("failing_required_filenames", [])]
        for category, record in filename_records.items()
        if record.get("failing_required_filenames")
    }


def all_required_filenames_present(
    filename_records: dict[str, dict[str, object]]
) -> bool:
    return all(
        record.get("all_required_filenames_present") is True
        for record in filename_records.values()
    )


def diagnostic_summary_record(
    checked_records: list[dict[str, object]],
    checked_counts: dict[str, int],
    evidence_categories: dict[str, object],
    failures: list[str],
) -> dict[str, object]:
    checked_count = len(checked_records)
    passed_count = sum(1 for record in checked_records if record.get("passed") is True)
    failed_count = checked_count - passed_count
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
    passing_counts = evidence_categories.get("passing_counts")
    failing_counts = evidence_categories.get("failing_counts")
    checked_count_values = [
        checked_counts.get(category)
        for category in REQUIRED_EVIDENCE_CATEGORIES
    ]
    checked_counts_categories_match = set(checked_counts.keys()) == set(
        REQUIRED_EVIDENCE_CATEGORIES
    )
    checked_counts_strict_numbers = all(
        type(value) is int and value >= 0 for value in checked_count_values
    )
    checked_counts_sum = sum(
        value
        for value in checked_count_values
        if type(value) is int
    )
    checked_counts_positive = all(
        type(value) is int and value > 0 for value in checked_count_values
    )
    checked_counts_match_categories = bool(
        isinstance(passing_counts, dict)
        and isinstance(failing_counts, dict)
        and all(
            type(checked_counts.get(category)) is int
            and type(passing_counts.get(category)) is int
            and type(failing_counts.get(category)) is int
            and checked_counts.get(category)
            == passing_counts.get(category, 0) + failing_counts.get(category, 0)
            for category in REQUIRED_EVIDENCE_CATEGORIES
        )
    )
    count_balance_valid = checked_count == passed_count + failed_count
    path_counts_valid = (
        len(checked_paths) == checked_count
        and len(passed_paths) == passed_count
        and len(failed_paths) == failed_count
    )
    checked_paths_match_passed_paths = checked_paths == passed_paths
    no_failed_paths = failed_paths == []
    no_failures = failures == []
    valid = bool(
        checked_count > 0
        and passed_count == checked_count
        and failed_count == 0
        and count_balance_valid
        and path_counts_valid
        and checked_paths_match_passed_paths
        and no_failed_paths
        and checked_counts_sum == checked_count
        and checked_counts_categories_match
        and checked_counts_strict_numbers
        and checked_counts_positive
        and checked_counts_match_categories
        and no_failures
    )
    return {
        "checked_count": checked_count,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "failure_count": len(failures),
        "checked_counts_sum": checked_counts_sum,
        "checked_counts_sum_matches": checked_counts_sum == checked_count,
        "checked_counts_categories_match": checked_counts_categories_match,
        "checked_counts_strict_numbers": checked_counts_strict_numbers,
        "checked_counts_positive": checked_counts_positive,
        "checked_counts_match_categories": checked_counts_match_categories,
        "count_balance_valid": count_balance_valid,
        "path_counts_valid": path_counts_valid,
        "checked_paths_match_passed_paths": checked_paths_match_passed_paths,
        "no_failed_paths": no_failed_paths,
        "no_failures": no_failures,
        "valid": valid,
    }


def route_status_counts_present(route_status_records: list[dict[str, object]]) -> bool:
    for record in route_status_records:
        if record.get("passed") is not True:
            continue
        counts = record.get("counts")
        missing_counts = record.get("missing_counts")
        required_counts = record.get("required_counts")
        if (
            isinstance(counts, dict)
            and isinstance(required_counts, list)
            and missing_counts == []
            and all(label in required_counts for label in REQUIRED_ROUTE_STATUS_COUNTS)
            and all(
                type(counts.get(label)) is int and counts.get(label) == 0
                for label in REQUIRED_ROUTE_STATUS_COUNTS
            )
        ):
            return True
    return False


def floorplan_evidence_present(floorplan_records: list[dict[str, object]]) -> bool:
    for record in floorplan_records:
        if (
            record.get("passed") is True
            and record.get("exists") is True
            and type(record.get("pblock_count")) is int
            and record["pblock_count"] >= 0
            and type(record.get("placed_cell_count")) is int
            and record["placed_cell_count"] > 0
            and isinstance(record.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is not None
        ):
            return True
    return False


def address_map_hex_fields_consistent(address_map_records: list[dict[str, object]]) -> bool:
    checked_entries = 0
    for record in address_map_records:
        if record.get("passed") is not True:
            continue
        entries = record.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            base_address = entry.get("base_address")
            high_address = entry.get("high_address")
            base_address_hex = entry.get("base_address_hex")
            high_address_hex = entry.get("high_address_hex")
            if type(base_address) is not int or base_address < 0:
                return False
            try:
                base_address_matches = (
                    isinstance(base_address_hex, str)
                    and int(base_address_hex, 16) == base_address
                )
                high_address_matches = (
                    high_address_hex is None
                    if high_address is None
                    else (
                        isinstance(high_address_hex, str)
                        and int(high_address_hex, 16) == high_address
                    )
                )
            except ValueError:
                return False
            if not base_address_matches:
                return False
            if high_address is None:
                if not high_address_matches:
                    return False
            elif (
                type(high_address) is not int
                or high_address < base_address
                or not high_address_matches
            ):
                return False
            checked_entries += 1
    return checked_entries > 0


def record_hashes_present(
    evidence_records: dict[str, list[dict[str, object]]]
) -> bool:
    for category in REQUIRED_EVIDENCE_CATEGORIES:
        passing_records = [
            record
            for record in evidence_records.get(category, [])
            if record.get("passed") is True
        ]
        if not passing_records:
            return False
        for record in passing_records:
            sha256 = record.get("sha256")
            if (
                record.get("exists") is not True
                or not isinstance(record.get("path"), str)
                or not record["path"]
                or not isinstance(record.get("path_resolved"), str)
                or not record["path_resolved"]
                or record["path_resolved"] != str(Path(record["path"]).resolve(strict=False))
                or type(record.get("byte_length")) is not int
                or record["byte_length"] <= 0
                or not isinstance(sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            ):
                return False
    return True


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
        "--floorplan",
        type=Path,
        action="append",
        default=[],
        help="Post-implementation floorplan summary report to validate and hash in evidence; may be passed multiple times",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        action="append",
        default=[],
        help="Generated artifact to hash in evidence; may be passed multiple times",
    )
    parser.add_argument(
        "--address-map",
        type=Path,
        action="append",
        default=[],
        help="Vivado block-design address map report to parse and hash in evidence; may be passed multiple times",
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
        help="fail unless all required report categories, address-map evidence, and named .bit/.xsa/.dcp artifacts passed",
    )
    parser.add_argument("--json", action="store_true", help="print parsed report evidence as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    failures = []
    artifact_records = []
    address_map_records = []
    timing_records = []
    utilization_records = []
    drc_records = []
    route_status_records = []
    clock_utilization_records = []
    floorplan_records = []

    for artifact in args.artifact:
        record, record_failures = artifact_record(artifact)
        artifact_records.append(record)
        failures.extend(record_failures)
    for address_map in args.address_map:
        record, record_failures = address_map_record(address_map)
        address_map_records.append(record)
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
    for floorplan in args.floorplan:
        record, record_failures = floorplan_record(floorplan)
        floorplan_records.append(record)
        failures.extend(record_failures)

    checked = (
        len(args.artifact)
        + len(args.address_map)
        + len(timing_paths)
        + len(args.utilization)
        + len(args.drc)
        + len(args.route_status)
        + len(args.clock_utilization)
        + len(args.floorplan)
    )
    checked_counts = {
        "artifacts": len(args.artifact),
        "address_map": len(args.address_map),
        "timing": len(timing_paths),
        "utilization": len(args.utilization),
        "drc": len(args.drc),
        "route_status": len(args.route_status),
        "clock_utilization": len(args.clock_utilization),
        "floorplan": len(args.floorplan),
    }
    checked_records = [
        *artifact_records,
        *address_map_records,
        *timing_records,
        *utilization_records,
        *drc_records,
        *route_status_records,
        *clock_utilization_records,
        *floorplan_records,
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
    evidence_records = {
        "artifacts": artifact_records,
        "address_map": address_map_records,
        "timing": timing_records,
        "utilization": utilization_records,
        "drc": drc_records,
        "route_status": route_status_records,
        "clock_utilization": clock_utilization_records,
        "floorplan": floorplan_records,
    }
    evidence_categories = evidence_category_record(evidence_records)
    artifact_suffixes = artifact_suffix_record(artifact_records)
    artifact_filenames = artifact_filename_record(artifact_records)
    address_map_filenames = required_filename_record(
        address_map_records,
        REQUIRED_ADDRESS_MAP_FILENAMES,
        "address_map",
    )
    report_filenames = report_filename_records(
        {
            "timing": timing_records,
            "utilization": utilization_records,
            "drc": drc_records,
            "route_status": route_status_records,
            "clock_utilization": clock_utilization_records,
            "floorplan": floorplan_records,
        }
    )
    hold_timing_filenames = required_filename_record(
        [
            record
            for record in timing_records
            if record.get("check_whs") is True
        ],
        REQUIRED_HOLD_TIMING_FILENAMES,
        "hold_timing",
    )
    missing_categories = evidence_categories["missing_required_categories"]
    missing_suffixes = artifact_suffixes["missing_required_suffixes"]
    missing_filenames = artifact_filenames["missing_required_filenames"]
    missing_address_map_filenames = address_map_filenames["missing_required_filenames"]
    missing_report_filenames = missing_required_filenames_by_category(report_filenames)
    missing_hold_timing_filenames = hold_timing_filenames["missing_required_filenames"]
    failing_categories = evidence_categories["failing_categories"]
    failing_suffixes = artifact_suffixes["failing_required_suffixes"]
    failing_filenames = artifact_filenames["failing_required_filenames"]
    failing_address_map_filenames = address_map_filenames["failing_required_filenames"]
    failing_report_filenames = failing_required_filenames_by_category(report_filenames)
    failing_hold_timing_filenames = hold_timing_filenames["failing_required_filenames"]
    clock_target = clock_target_record(args.clock_period_ns)
    clock_target_valid = clock_target["valid"] is True
    completion_diagnostic_summary = diagnostic_summary_record(
        checked_records,
        checked_counts,
        evidence_categories,
        failures,
    )
    route_status_counts_complete = route_status_counts_present(route_status_records)
    floorplan_evidence_complete = floorplan_evidence_present(floorplan_records)
    address_map_hex_fields_complete = address_map_hex_fields_consistent(
        address_map_records
    )
    record_hashes_complete = record_hashes_present(evidence_records)
    complete_vivado_flow_evidence = bool(
        evidence_categories["all_required_present"]
        and artifact_suffixes["all_required_suffixes_present"]
        and artifact_filenames["all_required_filenames_present"]
        and address_map_filenames["all_required_filenames_present"]
        and all_required_filenames_present(report_filenames)
        and hold_timing_filenames["all_required_filenames_present"]
        and clock_target_valid
        and not failing_categories
        and not failing_suffixes
        and not failing_filenames
        and not failing_address_map_filenames
        and not failing_report_filenames
        and not failing_hold_timing_filenames
        and completion_diagnostic_summary["valid"]
        and route_status_counts_complete
        and floorplan_evidence_complete
        and address_map_hex_fields_complete
        and record_hashes_complete
    )
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
        if missing_filenames:
            failures.append(
                "complete Vivado flow evidence missing required artifact filenames: "
                + ", ".join(str(filename) for filename in missing_filenames)
            )
        if missing_address_map_filenames:
            failures.append(
                "complete Vivado flow evidence missing required address-map filenames: "
                + ", ".join(str(filename) for filename in missing_address_map_filenames)
            )
        if missing_report_filenames:
            failures.append(
                "complete Vivado flow evidence missing required report filenames: "
                + json.dumps(missing_report_filenames, sort_keys=True)
            )
        if missing_hold_timing_filenames:
            failures.append(
                "complete Vivado flow evidence missing required hold-timing filenames: "
                + ", ".join(str(filename) for filename in missing_hold_timing_filenames)
            )
        if failing_categories:
            failures.append(
                "complete Vivado flow evidence has failing required categories: "
                + ", ".join(str(category) for category in failing_categories)
            )
        if failing_suffixes:
            failures.append(
                "complete Vivado flow evidence has failing required artifact suffixes: "
                + ", ".join(str(suffix) for suffix in failing_suffixes)
            )
        if failing_filenames:
            failures.append(
                "complete Vivado flow evidence has failing required artifact filenames: "
                + ", ".join(str(filename) for filename in failing_filenames)
            )
        if failing_address_map_filenames:
            failures.append(
                "complete Vivado flow evidence has failing required address-map filenames: "
                + ", ".join(str(filename) for filename in failing_address_map_filenames)
            )
        if failing_report_filenames:
            failures.append(
                "complete Vivado flow evidence has failing required report filenames: "
                + json.dumps(failing_report_filenames, sort_keys=True)
            )
        if failing_hold_timing_filenames:
            failures.append(
                "complete Vivado flow evidence has failing required hold-timing filenames: "
                + ", ".join(str(filename) for filename in failing_hold_timing_filenames)
            )
        if not clock_target_valid:
            failures.append("complete Vivado flow evidence has invalid clock target")
        if not route_status_counts_complete:
            failures.append(
                "complete Vivado flow evidence is missing required route-status counts"
            )
        if not floorplan_evidence_complete:
            failures.append(
                "complete Vivado flow evidence is missing positive floorplan placed-cell evidence"
            )
        if not address_map_hex_fields_complete:
            failures.append(
                "complete Vivado flow evidence has inconsistent address-map hex fields"
            )
        if not record_hashes_complete:
            failures.append(
                "complete Vivado flow evidence is missing file metadata for passing required records"
            )

    if args.json:
        diagnostic_summary = diagnostic_summary_record(
            checked_records,
            checked_counts,
            evidence_categories,
            failures,
        )
        arguments = {
            "artifacts": [str(path) for path in args.artifact],
            "address_map": [str(path) for path in args.address_map],
            "timing": [str(path) for path in args.timing],
            "hold_timing": [str(path) for path in args.hold_timing],
            "utilization": [str(path) for path in args.utilization],
            "drc": [str(path) for path in args.drc],
            "route_status": [str(path) for path in args.route_status],
            "clock_utilization": [str(path) for path in args.clock_utilization],
            "floorplan": [str(path) for path in args.floorplan],
            "min_wns": args.min_wns,
            "min_whs": args.min_whs,
            "max_utilization": args.max_utilization,
            "clock_period_ns": args.clock_period_ns,
            "require_complete_evidence": args.require_complete_evidence,
        }
        print(
            strict_json_dumps(
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
                    "diagnostic_summary": diagnostic_summary,
                    "artifact_suffixes": artifact_suffixes,
                    "artifact_filenames": artifact_filenames,
                    "address_map_filenames": address_map_filenames,
                    "report_filenames": report_filenames,
                    "hold_timing_filenames": hold_timing_filenames,
                    "clock_target": clock_target,
                    "clock_target_valid": clock_target_valid,
                    "route_status_counts_present": route_status_counts_complete,
                    "floorplan_evidence_present": floorplan_evidence_complete,
                    "address_map_hex_fields_consistent": (
                        address_map_hex_fields_complete
                    ),
                    "record_hashes_present": record_hashes_complete,
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
                    "complete_vivado_flow_evidence_missing_filenames": (
                        missing_filenames
                    ),
                    "complete_vivado_flow_evidence_missing_address_map_filenames": (
                        missing_address_map_filenames
                    ),
                    "complete_vivado_flow_evidence_missing_report_filenames": (
                        missing_report_filenames
                    ),
                    "complete_vivado_flow_evidence_missing_hold_timing_filenames": (
                        missing_hold_timing_filenames
                    ),
                    "complete_vivado_flow_evidence_failing_categories": (
                        failing_categories
                    ),
                    "complete_vivado_flow_evidence_failing_suffixes": (
                        failing_suffixes
                    ),
                    "complete_vivado_flow_evidence_failing_filenames": (
                        failing_filenames
                    ),
                    "complete_vivado_flow_evidence_failing_address_map_filenames": (
                        failing_address_map_filenames
                    ),
                    "complete_vivado_flow_evidence_failing_report_filenames": (
                        failing_report_filenames
                    ),
                    "complete_vivado_flow_evidence_failing_hold_timing_filenames": (
                        failing_hold_timing_filenames
                    ),
                    "arguments": arguments,
                    "clock_period_ns": args.clock_period_ns,
                    "clock_frequency_mhz": clock_target["clock_frequency_mhz"],
                    "artifacts": artifact_records,
                    "address_map": address_map_records,
                    "timing": timing_records,
                    "utilization": utilization_records,
                    "drc": drc_records,
                    "route_status": route_status_records,
                    "clock_utilization": clock_utilization_records,
                    "floorplan": floorplan_records,
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
