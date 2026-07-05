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

    def test_cli_passes_valid_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timing = root / "timing.rpt"
            utilization = root / "util.rpt"
            drc = root / "post_impl_drc.rpt"
            route_status = root / "post_impl_route_status.rpt"
            timing.write_text(TIMING_TABLE)
            utilization.write_text(UTILIZATION_TABLE)
            drc.write_text(DRC_CLEAN_REPORT)
            route_status.write_text(ROUTE_STATUS_CLEAN_REPORT)

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
            timing = root / "timing.rpt"
            utilization = root / "util.rpt"
            drc = root / "post_impl_drc.rpt"
            route_status = root / "post_impl_route_status.rpt"
            artifact.write_bytes(b"bitstream")
            timing.write_text(TIMING_TABLE)
            utilization.write_text(VIVADO_UTILIZATION_TABLE)
            drc.write_text(DRC_CLEAN_REPORT)
            route_status.write_text(ROUTE_STATUS_CLEAN_REPORT)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--artifact",
                            str(artifact),
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
            self.assertFalse(record["artifacts"][0]["exists"])
            self.assertFalse(record["artifacts"][0]["passed"])
            self.assertEqual(record["timing"][0]["wns_ns"], -0.01)
            self.assertEqual(record["timing"][0]["whs_ns"], -0.02)
            self.assertFalse(record["timing"][0]["passed"])
            self.assertTrue(any("artifact not found" in failure for failure in record["failures"]))
            self.assertTrue(any("below required" in failure for failure in record["failures"]))
            self.assertTrue(any("WHS" in failure for failure in record["failures"]))

    def test_cli_json_records_missing_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_timing = root / "missing_timing.rpt"
            missing_utilization = root / "missing_utilization.rpt"

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--timing",
                            str(missing_timing),
                            "--utilization",
                            str(missing_utilization),
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
            self.assertTrue(any("timing report not found" in failure for failure in record["failures"]))
            self.assertTrue(
                any("utilization report not found" in failure for failure in record["failures"])
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


if __name__ == "__main__":
    unittest.main()
