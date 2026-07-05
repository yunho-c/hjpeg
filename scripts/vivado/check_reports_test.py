#!/usr/bin/env python3

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import check_reports


TIMING_TABLE = """
Design Timing Summary
---------------------

  WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints  WHS(ns)      THS(ns)
  -------      -------  ---------------------  -------------------  -------      -------
    0.125        0.000                      0                    8    0.050        0.000
"""

UTILIZATION_TABLE = """
1. CLB Logic
------------

| Site Type | Used | Fixed | Available | Util% |
| LUT as Logic | 1234 | 0 | 117120 | 1.05 |
| Register as Flip Flop | 5678 | 0 | 234240 | 2.42 |
| Block RAM Tile | 12 | 0 | 144 | 8.33 |
"""

VIVADO_UTILIZATION_TABLE = """
1. CLB Logic
------------

| Site Type | Used | Fixed | Prohibited | Available | Util% |
| LUT as Logic | 26291 | 0 | 0 | 117120 | 22.45 |
| Register as Flip Flop | 25859 | 0 | 0 | 234240 | 11.04 |
| Block RAM Tile | 2 | 0 | 0 | 144 | 1.39 |
| PS8 | 1 | 0 | 0 | 1 | 100.00 |
"""

DRC_CLEAN_REPORT = """
Report DRC
----------

No DRC violations found.
"""

DRC_ZERO_COUNT_REPORT = """
Report DRC
----------

0 Violations found.
"""

DRC_VIOLATION_TABLE = """
Report DRC
----------

| Rule | Severity | Description |
| UCIO-1 | Critical Warning | Unconstrained Logical Port |
| NSTD-1 | Warning | Unspecified I/O Standard |
"""

ROUTE_STATUS_CLEAN_REPORT = """
Design Route Status
-------------------

Number of Unrouted Nets: 0
Number of Nets with Routing Errors: 0
"""

ROUTE_STATUS_VIVADO_VARIANT_REPORT = """
Design Route Status
-------------------

# of nets not completely routed: 0
# of nets with routing errors: 0
"""

ROUTE_STATUS_BAD_REPORT = """
Design Route Status
-------------------

Number of Unrouted Nets: 2
Number of Nets with Routing Errors: 1
"""


class CheckReportsTest(unittest.TestCase):
    def test_parse_wns_from_timing_table(self) -> None:
        self.assertEqual(check_reports.parse_wns(TIMING_TABLE), 0.125)

    def test_parse_whs_from_timing_table(self) -> None:
        self.assertEqual(check_reports.parse_whs(TIMING_TABLE), 0.05)

    def test_parse_wns_from_key_value_summary(self) -> None:
        self.assertEqual(check_reports.parse_wns("WNS(ns): -0.250\n"), -0.25)
        self.assertEqual(check_reports.parse_whs("WHS(ns): -0.125\n"), -0.125)

    def test_parse_utilization_rows(self) -> None:
        rows = check_reports.parse_utilization_rows(UTILIZATION_TABLE)
        self.assertEqual(
            [row.name for row in rows],
            ["LUT as Logic", "Register as Flip Flop", "Block RAM Tile"],
        )
        self.assertEqual(rows[0].used, 1234)
        self.assertEqual(rows[0].prohibited, 0)
        self.assertEqual(rows[2].percent, 8.33)

    def test_parse_vivado_utilization_rows_with_prohibited_column(self) -> None:
        rows = check_reports.parse_utilization_rows(VIVADO_UTILIZATION_TABLE)
        self.assertEqual(
            [row.name for row in rows],
            ["LUT as Logic", "Register as Flip Flop", "Block RAM Tile", "PS8"],
        )
        self.assertEqual(rows[0].used, 26291)
        self.assertEqual(rows[0].fixed, 0)
        self.assertEqual(rows[0].prohibited, 0)
        self.assertEqual(rows[0].available, 117120)
        self.assertEqual(rows[0].percent, 22.45)
        self.assertEqual(rows[3].available, 1)
        self.assertEqual(rows[3].percent, 100.0)

    def test_parse_drc_violations(self) -> None:
        violations, saw_zero_summary = check_reports.parse_drc_violations(DRC_VIOLATION_TABLE)
        self.assertFalse(saw_zero_summary)
        self.assertEqual([violation.rule for violation in violations], ["UCIO-1", "NSTD-1"])
        self.assertEqual(
            [violation.severity for violation in violations],
            ["critical warning", "warning"],
        )

    def test_parse_route_status_counts(self) -> None:
        self.assertEqual(
            check_reports.parse_route_status_counts(ROUTE_STATUS_CLEAN_REPORT),
            {
                "number_of_unrouted_nets": 0,
                "number_of_nets_with_routing_errors": 0,
            },
        )

    def test_check_utilization_ignores_expected_hard_system_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "util.rpt"
            report.write_text(
                "| Site Type | Used | Fixed | Prohibited | Available | Util% |\n"
                "| PS8 | 1 | 0 | 0 | 1 | 100.00 |\n"
            )
            self.assertEqual(check_reports.check_utilization(report, max_percent=90.0), [])

    def test_check_timing_reports_negative_slack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "timing.rpt"
            report.write_text("WNS(ns): -0.010\nWHS(ns): 0.000\n")
            self.assertEqual(
                check_reports.check_timing(report, min_wns=0.0),
                [f"{report}: WNS -0.010 ns is below required 0.000 ns"],
            )

    def test_check_timing_reports_negative_hold_slack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "timing.rpt"
            report.write_text("WNS(ns): 0.000\nWHS(ns): -0.020\n")
            self.assertEqual(check_reports.check_timing(report, min_wns=0.0), [])
            self.assertEqual(
                check_reports.check_timing(report, min_wns=0.0, min_whs=0.0, check_whs=True),
                [f"{report}: WHS -0.020 ns is below required 0.000 ns"],
            )

    def test_check_utilization_reports_over_threshold_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "util.rpt"
            report.write_text(
                "| Site Type | Used | Fixed | Prohibited | Available | Util% |\n"
                "| LUT as Logic | 95 | 0 | 0 | 100 | 95.00 |\n"
                "| Register as Flip Flop | 10 | 0 | 0 | 100 | 10.00 |\n"
            )
            self.assertEqual(
                check_reports.check_utilization(report, max_percent=90.0),
                [f"{report}: LUT as Logic utilization 95.00% exceeds 90.00%"],
            )

    def test_check_drc_reports_blocking_violations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_drc.rpt"
            report.write_text(DRC_VIOLATION_TABLE)

            failures = check_reports.check_drc(report)
            self.assertEqual(len(failures), 1)
            self.assertIn("critical warning", failures[0])
            self.assertIn("UCIO-1", failures[0])

    def test_check_drc_accepts_zero_violation_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_drc.rpt"
            report.write_text(DRC_CLEAN_REPORT)

            self.assertEqual(check_reports.check_drc(report), [])

    def test_check_drc_accepts_zero_count_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_drc.rpt"
            report.write_text(DRC_ZERO_COUNT_REPORT)

            self.assertEqual(check_reports.check_drc(report), [])

    def test_check_route_status_reports_unrouted_nets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_route_status.rpt"
            report.write_text(ROUTE_STATUS_BAD_REPORT)

            self.assertEqual(
                check_reports.check_route_status(report),
                [
                    f"{report}: route status number_of_unrouted_nets is 2, expected 0",
                    f"{report}: route status number_of_nets_with_routing_errors is 1, expected 0",
                ],
            )

    def test_check_route_status_accepts_zero_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_route_status.rpt"
            report.write_text(ROUTE_STATUS_CLEAN_REPORT)

            self.assertEqual(check_reports.check_route_status(report), [])

    def test_check_route_status_accepts_vivado_not_completely_routed_wording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_route_status.rpt"
            report.write_text(ROUTE_STATUS_VIVADO_VARIANT_REPORT)

            self.assertEqual(check_reports.check_route_status(report), [])

    def test_cli_passes_valid_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timing = root / "timing.rpt"
            utilization = root / "util.rpt"
            drc = root / "post_impl_drc.rpt"
            route_status = root / "post_impl_route_status.rpt"
            clock_utilization = root / "post_impl_clock_utilization.rpt"
            timing.write_text(TIMING_TABLE)
            utilization.write_text(UTILIZATION_TABLE)
            drc.write_text(DRC_CLEAN_REPORT)
            route_status.write_text(ROUTE_STATUS_CLEAN_REPORT)
            clock_utilization.write_text("Clock Utilization\n")

            self.assertEqual(
                check_reports.main(
                    [
                        "--timing",
                        str(timing),
                        "--utilization",
                        str(utilization),
                        "--drc",
                        str(drc),
                        "--route-status",
                        str(route_status),
                        "--clock-utilization",
                        str(clock_utilization),
                        "--min-wns",
                        "0",
                        "--min-whs",
                        "0",
                        "--max-utilization",
                        "90",
                    ]
                ),
                0,
            )

    def test_cli_can_print_json_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "hjpeg_kv260.bit"
            xsa = root / "hjpeg_kv260.xsa"
            address_map = root / "hjpeg_kv260_address_map.rpt"
            timing = root / "timing.rpt"
            utilization = root / "util.rpt"
            drc = root / "post_impl_drc.rpt"
            route_status = root / "post_impl_route_status.rpt"
            clock_utilization = root / "post_impl_clock_utilization.rpt"
            artifact.write_bytes(b"bitstream")
            xsa.write_bytes(b"xsa")
            address_map.write_text("Address Map\nhjpeg_0/s_axi_lite\n")
            timing.write_text(TIMING_TABLE)
            utilization.write_text(VIVADO_UTILIZATION_TABLE)
            drc.write_text(DRC_CLEAN_REPORT)
            route_status.write_text(ROUTE_STATUS_CLEAN_REPORT)
            clock_utilization.write_text("Clock Utilization\n")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--artifact",
                            str(artifact),
                            "--artifact",
                            str(xsa),
                            "--address-map",
                            str(address_map),
                            "--timing",
                            str(timing),
                            "--hold-timing",
                            str(timing),
                            "--utilization",
                            str(utilization),
                            "--drc",
                            str(drc),
                            "--route-status",
                            str(route_status),
                            "--clock-utilization",
                            str(clock_utilization),
                            "--clock-period-ns",
                            "8.0",
                            "--json",
                        ]
                    ),
                    0,
                )

            record = json.loads(stdout.getvalue())
            self.assertTrue(record["passed"])
            self.assertEqual(record["failures"], [])
            self.assertEqual(record["failure_count"], 0)
            self.assertEqual(record["checked_count"], 8)
            self.assertEqual(record["passed_count"], 8)
            self.assertEqual(record["failed_count"], 0)
            self.assertEqual(
                record["checked_paths"],
                [
                    str(artifact),
                    str(xsa),
                    str(address_map),
                    str(timing),
                    str(utilization),
                    str(drc),
                    str(route_status),
                    str(clock_utilization),
                ],
            )
            self.assertEqual(
                record["passed_paths"],
                [
                    str(artifact),
                    str(xsa),
                    str(address_map),
                    str(timing),
                    str(utilization),
                    str(drc),
                    str(route_status),
                    str(clock_utilization),
                ],
            )
            self.assertEqual(record["failed_paths"], [])
            self.assertEqual(
                record["checked_counts"],
                {
                    "artifacts": 2,
                    "address_map": 1,
                    "timing": 1,
                    "utilization": 1,
                    "drc": 1,
                    "route_status": 1,
                    "clock_utilization": 1,
                },
            )
            self.assertEqual(
                record["evidence_categories"],
                {
                    "required_categories": [
                        "artifacts",
                        "address_map",
                        "timing",
                        "utilization",
                        "drc",
                        "route_status",
                        "clock_utilization",
                    ],
                    "required_category_count": 7,
                    "present": {
                        "artifacts": True,
                        "address_map": True,
                        "timing": True,
                        "utilization": True,
                        "drc": True,
                        "route_status": True,
                        "clock_utilization": True,
                    },
                    "present_category_count": 7,
                    "missing_category_count": 0,
                    "passing_counts": {
                        "artifacts": 2,
                        "address_map": 1,
                        "timing": 1,
                        "utilization": 1,
                        "drc": 1,
                        "route_status": 1,
                        "clock_utilization": 1,
                    },
                    "failing_counts": {
                        "artifacts": 0,
                        "address_map": 0,
                        "timing": 0,
                        "utilization": 0,
                        "drc": 0,
                        "route_status": 0,
                        "clock_utilization": 0,
                    },
                    "present_required_categories": [
                        "artifacts",
                        "address_map",
                        "timing",
                        "utilization",
                        "drc",
                        "route_status",
                        "clock_utilization",
                    ],
                    "failing_categories": [],
                    "missing_required_categories": [],
                    "all_required_present": True,
                },
            )
            self.assertEqual(
                record["artifact_suffixes"],
                {
                    "required_suffixes": [".bit", ".xsa"],
                    "required_suffix_count": 2,
                    "suffix_counts": {".bit": 1, ".xsa": 1},
                    "passing_suffix_counts": {".bit": 1, ".xsa": 1},
                    "failing_suffix_counts": {},
                    "required_suffixes_present": {".bit": True, ".xsa": True},
                    "present_suffix_count": 2,
                    "missing_suffix_count": 0,
                    "present_required_suffixes": [".bit", ".xsa"],
                    "failing_required_suffixes": [],
                    "missing_required_suffixes": [],
                    "all_required_suffixes_present": True,
                },
            )
            self.assertEqual(
                record["arguments"],
                {
                    "artifacts": [str(artifact), str(xsa)],
                    "address_map": [str(address_map)],
                    "timing": [str(timing)],
                    "hold_timing": [str(timing)],
                    "utilization": [str(utilization)],
                    "drc": [str(drc)],
                    "route_status": [str(route_status)],
                    "clock_utilization": [str(clock_utilization)],
                    "min_wns": 0.0,
                    "min_whs": 0.0,
                    "max_utilization": 90.0,
                    "clock_period_ns": 8.0,
                    "require_complete_evidence": False,
                },
            )
            self.assertTrue(record["complete_vivado_flow_evidence"])
            self.assertFalse(record["complete_vivado_flow_evidence_required"])
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_categories"], []
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_suffixes"], []
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_categories"], []
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_suffixes"], []
            )
            self.assertEqual(record["clock_period_ns"], 8.0)
            self.assertEqual(record["clock_frequency_mhz"], 125.0)
            self.assertEqual(record["artifacts"][0]["path"], str(artifact))
            self.assertEqual(record["artifacts"][0]["byte_length"], len(b"bitstream"))
            self.assertEqual(
                record["artifacts"][0]["sha256"],
                hashlib.sha256(b"bitstream").hexdigest(),
            )
            self.assertTrue(record["artifacts"][0]["exists"])
            self.assertTrue(record["artifacts"][0]["passed"])
            self.assertEqual(record["artifacts"][1]["path"], str(xsa))
            self.assertEqual(record["artifacts"][1]["byte_length"], len(b"xsa"))
            self.assertEqual(
                record["artifacts"][1]["sha256"],
                hashlib.sha256(b"xsa").hexdigest(),
            )
            self.assertTrue(record["artifacts"][1]["exists"])
            self.assertTrue(record["artifacts"][1]["passed"])
            self.assertEqual(record["address_map"][0]["path"], str(address_map))
            self.assertEqual(
                record["address_map"][0]["sha256"],
                hashlib.sha256(address_map.read_bytes()).hexdigest(),
            )
            self.assertTrue(record["address_map"][0]["exists"])
            self.assertTrue(record["address_map"][0]["passed"])
            self.assertEqual(record["timing"][0]["path"], str(timing))
            self.assertTrue(record["timing"][0]["exists"])
            self.assertEqual(record["timing"][0]["wns_ns"], 0.125)
            self.assertEqual(record["timing"][0]["whs_ns"], 0.05)
            self.assertEqual(record["timing"][0]["min_whs_ns"], 0.0)
            self.assertTrue(record["timing"][0]["check_whs"])
            timing_bytes = timing.read_bytes()
            self.assertEqual(record["timing"][0]["byte_length"], len(timing_bytes))
            self.assertEqual(
                record["timing"][0]["sha256"],
                hashlib.sha256(timing_bytes).hexdigest(),
            )
            self.assertEqual(record["utilization"][0]["path"], str(utilization))
            self.assertTrue(record["utilization"][0]["exists"])
            self.assertEqual(record["utilization"][0]["rows"][0]["name"], "LUT as Logic")
            self.assertEqual(record["utilization"][0]["rows"][0]["prohibited"], 0)
            self.assertEqual(record["utilization"][0]["rows"][0]["available"], 117120)
            self.assertEqual(record["utilization"][0]["rows"][0]["percent"], 22.45)
            self.assertTrue(record["utilization"][0]["rows"][0]["checked"])
            self.assertTrue(record["utilization"][0]["rows"][0]["passed"])
            self.assertFalse(record["utilization"][0]["rows"][3]["checked"])
            self.assertTrue(record["utilization"][0]["rows"][3]["passed"])
            self.assertEqual(record["drc"][0]["path"], str(drc))
            self.assertTrue(record["drc"][0]["exists"])
            self.assertTrue(record["drc"][0]["saw_zero_summary"])
            self.assertEqual(record["drc"][0]["violations"], [])
            self.assertTrue(record["drc"][0]["passed"])
            self.assertEqual(record["route_status"][0]["path"], str(route_status))
            self.assertEqual(
                record["route_status"][0]["counts"],
                {
                    "number_of_unrouted_nets": 0,
                    "number_of_nets_with_routing_errors": 0,
                },
            )
            self.assertTrue(record["route_status"][0]["passed"])
            self.assertEqual(record["clock_utilization"][0]["path"], str(clock_utilization))
            self.assertEqual(
                record["clock_utilization"][0]["sha256"],
                hashlib.sha256(clock_utilization.read_bytes()).hexdigest(),
            )
            self.assertTrue(record["clock_utilization"][0]["exists"])
            self.assertTrue(record["clock_utilization"][0]["passed"])

    def test_cli_can_require_complete_vivado_flow_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timing = root / "timing.rpt"
            timing.write_text(TIMING_TABLE)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--timing",
                            str(timing),
                            "--require-complete-evidence",
                            "--json",
                        ]
                    ),
                    1,
                )

            record = json.loads(stdout.getvalue())
            self.assertFalse(record["passed"])
            self.assertFalse(record["complete_vivado_flow_evidence"])
            self.assertTrue(record["complete_vivado_flow_evidence_required"])
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_categories"],
                [
                    "artifacts",
                    "address_map",
                    "utilization",
                    "drc",
                    "route_status",
                    "clock_utilization",
                ],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_suffixes"],
                [".bit", ".xsa"],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_categories"], []
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_suffixes"], []
            )
            self.assertTrue(record["evidence_categories"]["present"]["timing"])
            self.assertFalse(record["evidence_categories"]["present"]["artifacts"])
            self.assertFalse(record["artifact_suffixes"]["all_required_suffixes_present"])
            self.assertTrue(
                any("missing required categories" in failure for failure in record["failures"])
            )
            self.assertTrue(
                any("missing required artifact suffixes" in failure for failure in record["failures"])
            )

    def test_evidence_categories_require_strict_passed_booleans(self) -> None:
        record = check_reports.evidence_category_record(
            {
                category: [{"passed": "true"}]
                for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
            }
        )

        self.assertEqual(
            record["missing_required_categories"],
            list(check_reports.REQUIRED_EVIDENCE_CATEGORIES),
        )
        self.assertEqual(record["present_required_categories"], [])
        self.assertEqual(
            record["failing_categories"],
            list(check_reports.REQUIRED_EVIDENCE_CATEGORIES),
        )
        self.assertFalse(record["all_required_present"])
        self.assertEqual(
            record["required_category_count"],
            len(check_reports.REQUIRED_EVIDENCE_CATEGORIES),
        )
        self.assertEqual(record["present_category_count"], 0)
        self.assertEqual(
            record["missing_category_count"],
            len(check_reports.REQUIRED_EVIDENCE_CATEGORIES),
        )
        self.assertEqual(
            record["passing_counts"],
            {
                category: 0
                for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
            },
        )
        self.assertEqual(
            record["failing_counts"],
            {
                category: 1
                for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
            },
        )
        self.assertTrue(
            all(not present for present in record["present"].values())
        )

    def test_artifact_suffixes_require_strict_passed_booleans(self) -> None:
        record = check_reports.artifact_suffix_record(
            [
                {"path": "hjpeg_kv260.bit", "passed": "true"},
                {"path": "hjpeg_kv260.xsa", "passed": "true"},
            ]
        )

        self.assertEqual(
            record["missing_required_suffixes"],
            list(check_reports.REQUIRED_ARTIFACT_SUFFIXES),
        )
        self.assertEqual(record["present_required_suffixes"], [])
        self.assertEqual(record["failing_required_suffixes"], [".bit", ".xsa"])
        self.assertFalse(record["all_required_suffixes_present"])
        self.assertEqual(
            record["required_suffix_count"],
            len(check_reports.REQUIRED_ARTIFACT_SUFFIXES),
        )
        self.assertEqual(record["present_suffix_count"], 0)
        self.assertEqual(
            record["missing_suffix_count"],
            len(check_reports.REQUIRED_ARTIFACT_SUFFIXES),
        )
        self.assertEqual(record["suffix_counts"], {".bit": 1, ".xsa": 1})
        self.assertEqual(record["passing_suffix_counts"], {})
        self.assertEqual(record["failing_suffix_counts"], {".bit": 1, ".xsa": 1})
        self.assertTrue(
            all(not present for present in record["required_suffixes_present"].values())
        )

    def test_cli_json_records_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timing = root / "timing.rpt"
            missing = root / "missing.xsa"
            timing.write_text("WNS(ns): -0.010\nWHS(ns): -0.020\n")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--artifact",
                            str(missing),
                            "--timing",
                            str(timing),
                            "--hold-timing",
                            str(timing),
                            "--json",
                        ]
                    ),
                    1,
                )

            record = json.loads(stdout.getvalue())
            self.assertFalse(record["passed"])
            self.assertEqual(record["checked_count"], 2)
            self.assertEqual(record["passed_count"], 0)
            self.assertEqual(record["failed_count"], 2)
            self.assertEqual(record["failure_count"], len(record["failures"]))
            self.assertEqual(record["failure_count"], 3)
            self.assertEqual(record["checked_paths"], [str(missing), str(timing)])
            self.assertEqual(record["passed_paths"], [])
            self.assertEqual(record["failed_paths"], [str(missing), str(timing)])
            self.assertFalse(record["complete_vivado_flow_evidence"])
            self.assertFalse(record["complete_vivado_flow_evidence_required"])
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_categories"],
                [
                    "artifacts",
                    "address_map",
                    "timing",
                    "utilization",
                    "drc",
                    "route_status",
                    "clock_utilization",
                ],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_suffixes"],
                [".bit", ".xsa"],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_categories"],
                ["artifacts", "timing"],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_suffixes"],
                [".xsa"],
            )
            self.assertEqual(
                record["checked_counts"],
                {
                    "artifacts": 1,
                    "address_map": 0,
                    "timing": 1,
                    "utilization": 0,
                    "drc": 0,
                    "route_status": 0,
                    "clock_utilization": 0,
                },
            )
            self.assertEqual(
                record["evidence_categories"]["missing_required_categories"],
                [
                    "artifacts",
                    "address_map",
                    "timing",
                    "utilization",
                    "drc",
                    "route_status",
                    "clock_utilization",
                ],
            )
            self.assertFalse(record["evidence_categories"]["present"]["artifacts"])
            self.assertFalse(record["evidence_categories"]["present"]["timing"])
            self.assertFalse(record["evidence_categories"]["all_required_present"])
            self.assertEqual(
                record["artifact_suffixes"],
                {
                    "required_suffixes": [".bit", ".xsa"],
                    "required_suffix_count": 2,
                    "suffix_counts": {".xsa": 1},
                    "passing_suffix_counts": {},
                    "failing_suffix_counts": {".xsa": 1},
                    "required_suffixes_present": {".bit": False, ".xsa": False},
                    "present_suffix_count": 0,
                    "missing_suffix_count": 2,
                    "present_required_suffixes": [],
                    "failing_required_suffixes": [".xsa"],
                    "missing_required_suffixes": [".bit", ".xsa"],
                    "all_required_suffixes_present": False,
                },
            )
            self.assertFalse(record["artifacts"][0]["exists"])
            self.assertFalse(record["artifacts"][0]["passed"])
            self.assertEqual(record["timing"][0]["wns_ns"], -0.01)
            self.assertEqual(record["timing"][0]["whs_ns"], -0.02)
            self.assertFalse(record["timing"][0]["passed"])
            self.assertTrue(any("artifact not found" in failure for failure in record["failures"]))
            self.assertTrue(any("below required" in failure for failure in record["failures"]))
            self.assertTrue(any("WHS" in failure for failure in record["failures"]))

    def test_cli_json_rejects_empty_artifacts_and_evidence_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "hjpeg_kv260.bit"
            clock_utilization = root / "post_impl_clock_utilization.rpt"
            artifact.write_bytes(b"")
            clock_utilization.write_bytes(b"")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--artifact",
                            str(artifact),
                            "--clock-utilization",
                            str(clock_utilization),
                            "--json",
                        ]
                    ),
                    1,
                )

            record = json.loads(stdout.getvalue())
            self.assertFalse(record["passed"])
            self.assertEqual(
                record["artifact_suffixes"],
                {
                    "required_suffixes": [".bit", ".xsa"],
                    "required_suffix_count": 2,
                    "suffix_counts": {".bit": 1},
                    "passing_suffix_counts": {},
                    "failing_suffix_counts": {".bit": 1},
                    "required_suffixes_present": {".bit": False, ".xsa": False},
                    "present_suffix_count": 0,
                    "missing_suffix_count": 2,
                    "present_required_suffixes": [],
                    "failing_required_suffixes": [".bit"],
                    "missing_required_suffixes": [".bit", ".xsa"],
                    "all_required_suffixes_present": False,
                },
            )
            self.assertEqual(record["artifacts"][0]["byte_length"], 0)
            self.assertFalse(record["artifacts"][0]["passed"])
            self.assertEqual(record["clock_utilization"][0]["byte_length"], 0)
            self.assertFalse(record["clock_utilization"][0]["passed"])
            self.assertTrue(any("artifact is empty" in failure for failure in record["failures"]))
            self.assertTrue(
                any("clock utilization report is empty" in failure for failure in record["failures"])
            )

    def test_cli_json_records_drc_and_route_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drc = root / "post_impl_drc.rpt"
            route_status = root / "post_impl_route_status.rpt"
            drc.write_text(DRC_VIOLATION_TABLE)
            route_status.write_text(ROUTE_STATUS_BAD_REPORT)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--drc",
                            str(drc),
                            "--route-status",
                            str(route_status),
                            "--json",
                        ]
                    ),
                    1,
                )

            record = json.loads(stdout.getvalue())
            self.assertFalse(record["passed"])
            self.assertEqual(record["drc"][0]["path"], str(drc))
            self.assertFalse(record["drc"][0]["passed"])
            self.assertEqual(record["drc"][0]["violations"][0]["rule"], "UCIO-1")
            self.assertTrue(record["drc"][0]["violations"][0]["blocking"])
            self.assertFalse(record["drc"][0]["violations"][1]["blocking"])
            self.assertEqual(
                record["route_status"][0]["counts"],
                {
                    "number_of_unrouted_nets": 2,
                    "number_of_nets_with_routing_errors": 1,
                },
            )
            self.assertFalse(record["route_status"][0]["passed"])
            self.assertTrue(any("DRC critical warning" in failure for failure in record["failures"]))
            self.assertTrue(any("number_of_unrouted_nets" in failure for failure in record["failures"]))
            self.assertTrue(
                any("number_of_nets_with_routing_errors" in failure for failure in record["failures"])
            )

    def test_cli_json_records_missing_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_timing = root / "missing_timing.rpt"
            missing_utilization = root / "missing_utilization.rpt"
            missing_clock_utilization = root / "missing_clock_utilization.rpt"

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--timing",
                            str(missing_timing),
                            "--utilization",
                            str(missing_utilization),
                            "--clock-utilization",
                            str(missing_clock_utilization),
                            "--json",
                        ]
                    ),
                    1,
                )

            record = json.loads(stdout.getvalue())
            self.assertFalse(record["passed"])
            self.assertFalse(record["timing"][0]["exists"])
            self.assertFalse(record["timing"][0]["passed"])
            self.assertEqual(record["timing"][0]["min_wns_ns"], 0.0)
            self.assertFalse(record["timing"][0]["check_whs"])
            self.assertFalse(record["utilization"][0]["exists"])
            self.assertFalse(record["utilization"][0]["passed"])
            self.assertEqual(record["utilization"][0]["rows"], [])
            self.assertFalse(record["clock_utilization"][0]["exists"])
            self.assertFalse(record["clock_utilization"][0]["passed"])
            self.assertTrue(any("timing report not found" in failure for failure in record["failures"]))
            self.assertTrue(
                any("utilization report not found" in failure for failure in record["failures"])
            )
            self.assertTrue(
                any("clock utilization report not found" in failure for failure in record["failures"])
            )

    def test_cli_json_records_unparseable_timing_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timing = root / "bad_timing.rpt"
            timing.write_text("Timing summary omitted\n")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(["--timing", str(timing), "--json"]),
                    1,
                )

            record = json.loads(stdout.getvalue())
            self.assertFalse(record["passed"])
            self.assertTrue(record["timing"][0]["exists"])
            self.assertFalse(record["timing"][0]["passed"])
            self.assertNotIn("wns_ns", record["timing"][0])
            self.assertTrue(any("could not find WNS" in failure for failure in record["failures"]))

    def test_cli_json_records_unparseable_utilization_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            utilization = root / "bad_utilization.rpt"
            utilization.write_text("Utilization summary omitted\n")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(["--utilization", str(utilization), "--json"]),
                    1,
                )

            record = json.loads(stdout.getvalue())
            self.assertFalse(record["passed"])
            self.assertTrue(record["utilization"][0]["exists"])
            self.assertFalse(record["utilization"][0]["passed"])
            self.assertEqual(record["utilization"][0]["rows"], [])
            self.assertTrue(any("no utilization rows found" in failure for failure in record["failures"]))

    def test_cli_rejects_nonpositive_clock_period(self) -> None:
        with self.assertRaises(SystemExit):
            check_reports.main(["--clock-period-ns", "0", "--json"])

    def test_cli_rejects_nonfinite_clock_period(self) -> None:
        for value in ["nan", "inf", "-inf"]:
            with self.subTest(value=value):
                with self.assertRaises(SystemExit):
                    check_reports.main([f"--clock-period-ns={value}", "--json"])

    def test_cli_rejects_nonfinite_timing_thresholds(self) -> None:
        for option in ["--min-wns", "--min-whs"]:
            for value in ["nan", "inf", "-inf"]:
                with self.subTest(option=option, value=value):
                    with self.assertRaises(SystemExit):
                        check_reports.main([f"{option}={value}", "--json"])

    def test_cli_rejects_invalid_utilization_thresholds(self) -> None:
        for value in ["-0.001", "nan", "inf", "-inf"]:
            with self.subTest(value=value):
                with self.assertRaises(SystemExit):
                    check_reports.main([f"--max-utilization={value}", "--json"])


if __name__ == "__main__":
    unittest.main()
