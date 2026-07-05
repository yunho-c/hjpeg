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

    def test_cli_passes_valid_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timing = root / "timing.rpt"
            utilization = root / "util.rpt"
            timing.write_text(TIMING_TABLE)
            utilization.write_text(UTILIZATION_TABLE)

            self.assertEqual(
                check_reports.main(
                    [
                        "--timing",
                        str(timing),
                        "--utilization",
                        str(utilization),
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
            artifact.write_bytes(b"bitstream")
            timing.write_text(TIMING_TABLE)
            utilization.write_text(VIVADO_UTILIZATION_TABLE)

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
                            "--json",
                        ]
                    ),
                    0,
                )

            record = json.loads(stdout.getvalue())
            self.assertTrue(record["passed"])
            self.assertEqual(record["failures"], [])
            self.assertEqual(record["artifacts"][0]["path"], str(artifact))
            self.assertEqual(record["artifacts"][0]["byte_length"], len(b"bitstream"))
            self.assertEqual(
                record["artifacts"][0]["sha256"],
                hashlib.sha256(b"bitstream").hexdigest(),
            )
            self.assertTrue(record["artifacts"][0]["exists"])
            self.assertTrue(record["artifacts"][0]["passed"])
            self.assertEqual(record["timing"][0]["path"], str(timing))
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
            self.assertEqual(record["utilization"][0]["rows"][0]["name"], "LUT as Logic")
            self.assertEqual(record["utilization"][0]["rows"][0]["prohibited"], 0)
            self.assertEqual(record["utilization"][0]["rows"][0]["available"], 117120)
            self.assertEqual(record["utilization"][0]["rows"][0]["percent"], 22.45)
            self.assertTrue(record["utilization"][0]["rows"][0]["checked"])
            self.assertTrue(record["utilization"][0]["rows"][0]["passed"])
            self.assertFalse(record["utilization"][0]["rows"][3]["checked"])
            self.assertTrue(record["utilization"][0]["rows"][3]["passed"])

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


if __name__ == "__main__":
    unittest.main()
