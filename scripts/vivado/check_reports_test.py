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

  WNS(ns)      TNS(ns)  TNS Failing Endpoints
  -------      -------  ---------------------
    0.125        0.000                      0
"""

UTILIZATION_TABLE = """
1. CLB Logic
------------

| Site Type | Used | Fixed | Available | Util% |
| LUT as Logic | 1234 | 0 | 117120 | 1.05 |
| Register as Flip Flop | 5678 | 0 | 234240 | 2.42 |
| Block RAM Tile | 12 | 0 | 144 | 8.33 |
"""


class CheckReportsTest(unittest.TestCase):
    def test_parse_wns_from_timing_table(self) -> None:
        self.assertEqual(check_reports.parse_wns(TIMING_TABLE), 0.125)

    def test_parse_wns_from_key_value_summary(self) -> None:
        self.assertEqual(check_reports.parse_wns("WNS(ns): -0.250\n"), -0.25)

    def test_parse_utilization_rows(self) -> None:
        rows = check_reports.parse_utilization_rows(UTILIZATION_TABLE)
        self.assertEqual(
            [row.name for row in rows],
            ["LUT as Logic", "Register as Flip Flop", "Block RAM Tile"],
        )
        self.assertEqual(rows[0].used, 1234)
        self.assertEqual(rows[2].percent, 8.33)

    def test_check_timing_reports_negative_slack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "timing.rpt"
            report.write_text("WNS(ns): -0.010\n")
            self.assertEqual(
                check_reports.check_timing(report, min_wns=0.0),
                [f"{report}: WNS -0.010 ns is below required 0.000 ns"],
            )

    def test_check_utilization_reports_over_threshold_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "util.rpt"
            report.write_text(
                "| Site Type | Used | Fixed | Available | Util% |\n"
                "| LUT as Logic | 95 | 0 | 100 | 95.00 |\n"
                "| Register as Flip Flop | 10 | 0 | 100 | 10.00 |\n"
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
            utilization.write_text(UTILIZATION_TABLE)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--artifact",
                            str(artifact),
                            "--timing",
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
            timing_bytes = timing.read_bytes()
            self.assertEqual(record["timing"][0]["byte_length"], len(timing_bytes))
            self.assertEqual(
                record["timing"][0]["sha256"],
                hashlib.sha256(timing_bytes).hexdigest(),
            )
            self.assertEqual(record["utilization"][0]["path"], str(utilization))
            self.assertEqual(record["utilization"][0]["rows"][0]["name"], "LUT as Logic")
            self.assertTrue(record["utilization"][0]["rows"][0]["passed"])

    def test_cli_json_records_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timing = root / "timing.rpt"
            missing = root / "missing.xsa"
            timing.write_text("WNS(ns): -0.010\n")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--artifact",
                            str(missing),
                            "--timing",
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
            self.assertFalse(record["timing"][0]["passed"])
            self.assertTrue(any("artifact not found" in failure for failure in record["failures"]))
            self.assertTrue(any("below required" in failure for failure in record["failures"]))


if __name__ == "__main__":
    unittest.main()
