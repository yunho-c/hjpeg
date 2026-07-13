#!/usr/bin/env python3

import argparse
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

ROUTE_STATUS_VIVADO_TABLE_REPORT = """
Design Route Status
                                               :      # nets :
   ------------------------------------------- : ----------- :
   # of logical nets.......................... :      119470 :
       # of nets not needing routing.......... :       44355 :
           # of internally routed nets........ :       36293 :
           # of nets with no loads............ :        8062 :
       # of routable nets..................... :       75115 :
           # of fully routed nets............. :       75115 :
       # of nets with routing errors.......... :           0 :
   ------------------------------------------- : ----------- :
"""

ROUTE_STATUS_VIVADO_TABLE_UNROUTED_REPORT = """
Design Route Status
                                               :      # nets :
   ------------------------------------------- : ----------- :
   # of logical nets.......................... :      119470 :
       # of nets not needing routing.......... :       44355 :
       # of routable nets..................... :       75115 :
           # of fully routed nets............. :       75113 :
       # of nets with routing errors.......... :           1 :
   ------------------------------------------- : ----------- :
"""

ROUTE_STATUS_MISSING_ROUTING_ERRORS_REPORT = """
Design Route Status
-------------------

Number of Unrouted Nets: 0
"""

ROUTE_STATUS_MISSING_UNROUTED_REPORT = """
Design Route Status
-------------------

Number of Nets with Routing Errors: 0
"""

ROUTE_STATUS_BAD_REPORT = """
Design Route Status
-------------------

Number of Unrouted Nets: 2
Number of Nets with Routing Errors: 1
"""

ADDRESS_MAP_REPORT = """
Address Map
-----------

| Master | Slave | Base Address | High Address |
| ps/M_AXI_HPM0_FPD | hjpeg_0/s_axi_lite/Reg | 0xA000_0000 | 0xA000_FFFF |
| ps/M_AXI_HPM0_FPD | axi_dma_0/S_AXI_LITE/Reg | 0xA001_0000 | 0xA001_FFFF |
"""

FLOORPLAN_REPORT = """
Floorplan Summary
Part: xck26-sfvc784-2LV-c
Pblock Count: 0
Placed Cell Count: 12345
Pblocks:
"""


class CheckReportsTest(unittest.TestCase):
    def test_clock_target_record_requires_positive_finite_period(self) -> None:
        record = check_reports.clock_target_record(8.0)
        self.assertEqual(record["clock_period_ns"], 8.0)
        self.assertEqual(record["clock_frequency_mhz"], 125.0)
        self.assertTrue(record["clock_period_finite"])
        self.assertTrue(record["clock_period_positive"])
        self.assertTrue(record["clock_frequency_finite"])
        self.assertTrue(record["clock_frequency_positive"])
        self.assertTrue(record["period_frequency_match"])
        self.assertTrue(record["valid"])

        for period in [0.0, -1.0, float("nan"), float("inf")]:
            with self.subTest(period=period):
                invalid = check_reports.clock_target_record(period)
                self.assertFalse(invalid["valid"])

    def test_diagnostic_summary_requires_consistent_complete_evidence(self) -> None:
        checked_records = [
            {"path": f"{category}.rpt", "passed": True}
            for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
        ]
        checked_counts = {
            category: 1
            for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
        }
        evidence_categories = check_reports.evidence_category_record(
            {
                category: [{"passed": True}]
                for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
            }
        )

        record = check_reports.diagnostic_summary_record(
            checked_records,
            checked_counts,
            evidence_categories,
            [],
        )

        self.assertTrue(record["valid"])
        self.assertEqual(record["checked_count"], len(checked_records))
        self.assertEqual(record["passed_count"], len(checked_records))
        self.assertEqual(record["failed_count"], 0)
        self.assertEqual(record["failure_count"], 0)
        self.assertEqual(record["checked_counts_sum"], len(checked_records))
        self.assertTrue(record["checked_counts_sum_matches"])
        self.assertTrue(record["checked_counts_categories_match"])
        self.assertTrue(record["checked_counts_strict_numbers"])
        self.assertTrue(record["checked_counts_positive"])
        self.assertTrue(record["checked_counts_match_categories"])
        self.assertTrue(record["count_balance_valid"])
        self.assertTrue(record["path_counts_valid"])
        self.assertTrue(record["checked_paths_match_passed_paths"])
        self.assertTrue(record["no_failed_paths"])
        self.assertTrue(record["no_failures"])

        checked_counts["clock_utilization"] = 0
        inconsistent = check_reports.diagnostic_summary_record(
            checked_records,
            checked_counts,
            evidence_categories,
            ["diagnostic failure"],
        )

        self.assertFalse(inconsistent["valid"])
        self.assertFalse(inconsistent["checked_counts_sum_matches"])
        self.assertTrue(inconsistent["checked_counts_categories_match"])
        self.assertTrue(inconsistent["checked_counts_strict_numbers"])
        self.assertFalse(inconsistent["checked_counts_positive"])
        self.assertFalse(inconsistent["checked_counts_match_categories"])
        self.assertFalse(inconsistent["no_failures"])

    def test_diagnostic_summary_rejects_boolean_checked_counts(self) -> None:
        checked_records = [
            {"path": f"{category}.rpt", "passed": True}
            for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
        ]
        checked_counts = {
            category: 1
            for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
        }
        checked_counts["artifacts"] = True
        evidence_categories = check_reports.evidence_category_record(
            {
                category: [{"passed": True}]
                for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
            }
        )

        record = check_reports.diagnostic_summary_record(
            checked_records,
            checked_counts,
            evidence_categories,
            [],
        )

        self.assertFalse(record["valid"])
        self.assertFalse(record["checked_counts_strict_numbers"])
        self.assertFalse(record["checked_counts_positive"])
        self.assertFalse(record["checked_counts_match_categories"])

    def test_diagnostic_summary_rejects_boolean_category_counts(self) -> None:
        checked_records = [
            {"path": f"{category}.rpt", "passed": True}
            for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
        ]
        checked_counts = {
            category: 1
            for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
        }
        evidence_categories = check_reports.evidence_category_record(
            {
                category: [{"passed": True}]
                for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
            }
        )
        evidence_categories["passing_counts"]["artifacts"] = True
        evidence_categories["failing_counts"]["artifacts"] = False

        record = check_reports.diagnostic_summary_record(
            checked_records,
            checked_counts,
            evidence_categories,
            [],
        )

        self.assertFalse(record["valid"])
        self.assertTrue(record["checked_counts_strict_numbers"])
        self.assertTrue(record["checked_counts_positive"])
        self.assertFalse(record["checked_counts_match_categories"])

    def test_diagnostic_summary_rejects_extra_checked_count_categories(self) -> None:
        checked_records = [
            {"path": f"{category}.rpt", "passed": True}
            for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
        ]
        checked_counts = {
            category: 1
            for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
        }
        checked_counts["unexpected"] = 1
        evidence_categories = check_reports.evidence_category_record(
            {
                category: [{"passed": True}]
                for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES
            }
        )

        record = check_reports.diagnostic_summary_record(
            checked_records,
            checked_counts,
            evidence_categories,
            [],
        )

        self.assertFalse(record["valid"])
        self.assertFalse(record["checked_counts_categories_match"])
        self.assertTrue(record["checked_counts_strict_numbers"])
        self.assertTrue(record["checked_counts_positive"])
        self.assertTrue(record["checked_counts_match_categories"])

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

    def test_parse_route_status_counts_from_vivado_table(self) -> None:
        self.assertEqual(
            check_reports.parse_route_status_counts(ROUTE_STATUS_VIVADO_TABLE_REPORT),
            {
                "number_of_nets_with_routing_errors": 0,
                "number_of_unrouted_nets": 0,
            },
        )

    def test_parse_route_status_counts_derives_unrouted_vivado_table_nets(self) -> None:
        self.assertEqual(
            check_reports.parse_route_status_counts(ROUTE_STATUS_VIVADO_TABLE_UNROUTED_REPORT),
            {
                "number_of_nets_with_routing_errors": 1,
                "number_of_unrouted_nets": 2,
            },
        )

    def test_check_utilization_keeps_non_budget_rows_informational(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "util.rpt"
            report.write_text(
                "| Site Type | Used | Fixed | Prohibited | Available | Util% |\n"
                "| CLB | 95 | 0 | 0 | 100 | 95.00 |\n"
                "| PS8 | 1 | 0 | 0 | 1 | 100.00 |\n"
            )
            self.assertEqual(check_reports.check_utilization(report, max_percent=90.0), [])

            record, failures = check_reports.utilization_record(report, max_percent=90.0)
            self.assertEqual(failures, [])
            self.assertEqual(
                {row["name"]: row["checked"] for row in record["rows"]},
                {"CLB": False, "PS8": False},
            )

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

    def test_check_route_status_accepts_vivado_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_route_status.rpt"
            report.write_text(ROUTE_STATUS_VIVADO_TABLE_REPORT)

            self.assertEqual(check_reports.check_route_status(report), [])

    def test_check_route_status_reports_vivado_table_unrouted_nets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_route_status.rpt"
            report.write_text(ROUTE_STATUS_VIVADO_TABLE_UNROUTED_REPORT)

            self.assertEqual(
                check_reports.check_route_status(report),
                [
                    f"{report}: route status number_of_nets_with_routing_errors is 1, expected 0",
                    f"{report}: route status number_of_unrouted_nets is 2, expected 0",
                ],
            )

    def test_check_route_status_requires_routing_error_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_route_status.rpt"
            report.write_text(ROUTE_STATUS_MISSING_ROUTING_ERRORS_REPORT)

            self.assertEqual(
                check_reports.check_route_status(report),
                [f"{report}: route status missing number_of_nets_with_routing_errors count"],
            )

    def test_check_route_status_requires_unrouted_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_route_status.rpt"
            report.write_text(ROUTE_STATUS_MISSING_UNROUTED_REPORT)

            self.assertEqual(
                check_reports.check_route_status(report),
                [f"{report}: route status missing number_of_unrouted_nets count"],
            )

    def test_route_status_record_reports_missing_required_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_route_status.rpt"
            report.write_text(ROUTE_STATUS_MISSING_UNROUTED_REPORT)

            record, failures = check_reports.route_status_record(report)

            self.assertEqual(
                record["required_counts"],
                [
                    "number_of_unrouted_nets",
                    "number_of_nets_with_routing_errors",
                ],
            )
            self.assertEqual(record["counts"], {"number_of_nets_with_routing_errors": 0})
            self.assertEqual(record["missing_counts"], ["number_of_unrouted_nets"])
            self.assertFalse(record["passed"])
            self.assertEqual(
                failures,
                [f"{report}: route status missing number_of_unrouted_nets count"],
            )

    def test_floorplan_record_reports_placed_cell_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_floorplan.rpt"
            report.write_text(FLOORPLAN_REPORT)

            record, failures = check_reports.floorplan_record(report)

            self.assertEqual(failures, [])
            self.assertTrue(record["passed"])
            self.assertEqual(record["pblock_count"], 0)
            self.assertEqual(record["placed_cell_count"], 12345)
            self.assertEqual(
                record["counts"],
                {"pblock_count": 0, "placed_cell_count": 12345},
            )

    def test_floorplan_record_requires_positive_placed_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "post_impl_floorplan.rpt"
            report.write_text(
                "Floorplan Summary\nPblock Count: 0\nPlaced Cell Count: 0\n"
            )

            record, failures = check_reports.floorplan_record(report)

            self.assertFalse(record["passed"])
            self.assertEqual(record["placed_cell_count"], 0)
            self.assertTrue(any("expected positive" in failure for failure in failures))

    def test_parse_address_map_entries(self) -> None:
        entries = check_reports.parse_address_map_entries(ADDRESS_MAP_REPORT)

        self.assertEqual(
            [entry.interface for entry in entries],
            ["hjpeg_0/s_axi_lite", "axi_dma_0/s_axi_lite"],
        )
        self.assertEqual(entries[0].base_address, 0xA0000000)
        self.assertEqual(entries[0].high_address, 0xA000FFFF)
        self.assertEqual(entries[1].base_address, 0xA0010000)
        self.assertEqual(entries[1].high_address, 0xA001FFFF)

    def test_check_address_map_requires_control_apertures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "hjpeg_kv260_address_map.rpt"
            report.write_text(
                "Address Map\n"
                "| ps/M_AXI_HPM0_FPD | hjpeg_0/s_axi_lite/Reg | 0xA0000000 | 0xA000FFFF |\n"
            )

            self.assertEqual(
                check_reports.check_address_map(report),
                [f"{report}: address map missing axi_dma_0/s_axi_lite base address"],
            )

    def test_cli_passes_valid_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            post_synth_timing = root / "post_synth_timing_summary.rpt"
            post_impl_timing = root / "post_impl_timing_summary.rpt"
            post_synth_utilization = root / "post_synth_utilization.rpt"
            post_impl_utilization = root / "post_impl_utilization.rpt"
            drc = root / "post_impl_drc.rpt"
            route_status = root / "post_impl_route_status.rpt"
            clock_utilization = root / "post_impl_clock_utilization.rpt"
            floorplan = root / "post_impl_floorplan.rpt"
            post_synth_timing.write_text(TIMING_TABLE)
            post_synth_utilization.write_text(UTILIZATION_TABLE)
            drc.write_text(DRC_CLEAN_REPORT)
            route_status.write_text(ROUTE_STATUS_CLEAN_REPORT)
            clock_utilization.write_text("Clock Utilization\n")
            floorplan.write_text(FLOORPLAN_REPORT)

            self.assertEqual(
                check_reports.main(
                    [
                        "--timing",
                        str(post_synth_timing),
                        "--utilization",
                        str(post_synth_utilization),
                        "--drc",
                        str(drc),
                        "--route-status",
                        str(route_status),
                        "--clock-utilization",
                        str(clock_utilization),
                        "--floorplan",
                        str(floorplan),
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
            dcp = root / "post_impl.dcp"
            address_map = root / "hjpeg_kv260_address_map.rpt"
            post_synth_timing = root / "post_synth_timing_summary.rpt"
            post_impl_timing = root / "post_impl_timing_summary.rpt"
            post_synth_utilization = root / "post_synth_utilization.rpt"
            post_impl_utilization = root / "post_impl_utilization.rpt"
            drc = root / "post_impl_drc.rpt"
            route_status = root / "post_impl_route_status.rpt"
            clock_utilization = root / "post_impl_clock_utilization.rpt"
            floorplan = root / "post_impl_floorplan.rpt"
            artifact.write_bytes(b"bitstream")
            xsa.write_bytes(b"xsa")
            dcp.write_bytes(b"checkpoint")
            address_map.write_text(ADDRESS_MAP_REPORT)
            post_synth_timing.write_text(TIMING_TABLE)
            post_impl_timing.write_text(TIMING_TABLE)
            post_synth_utilization.write_text(VIVADO_UTILIZATION_TABLE)
            post_impl_utilization.write_text(VIVADO_UTILIZATION_TABLE)
            drc.write_text(DRC_CLEAN_REPORT)
            route_status.write_text(ROUTE_STATUS_CLEAN_REPORT)
            clock_utilization.write_text("Clock Utilization\n")
            floorplan.write_text(FLOORPLAN_REPORT)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--artifact",
                            str(artifact),
                            "--artifact",
                            str(xsa),
                            "--artifact",
                            str(dcp),
                            "--address-map",
                            str(address_map),
                            "--timing",
                            str(post_synth_timing),
                            "--timing",
                            str(post_impl_timing),
                            "--hold-timing",
                            str(post_impl_timing),
                            "--utilization",
                            str(post_synth_utilization),
                            "--utilization",
                            str(post_impl_utilization),
                            "--drc",
                            str(drc),
                            "--route-status",
                            str(route_status),
                            "--clock-utilization",
                            str(clock_utilization),
                            "--floorplan",
                            str(floorplan),
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
            self.assertEqual(record["checked_count"], 12)
            self.assertEqual(record["passed_count"], 12)
            self.assertEqual(record["failed_count"], 0)
            self.assertEqual(
                record["checked_paths"],
                [
                    str(artifact),
                    str(xsa),
                    str(dcp),
                    str(address_map),
                    str(post_synth_timing),
                    str(post_impl_timing),
                    str(post_synth_utilization),
                    str(post_impl_utilization),
                    str(drc),
                    str(route_status),
                    str(clock_utilization),
                    str(floorplan),
                ],
            )
            self.assertEqual(
                record["passed_paths"],
                [
                    str(artifact),
                    str(xsa),
                    str(dcp),
                    str(address_map),
                    str(post_synth_timing),
                    str(post_impl_timing),
                    str(post_synth_utilization),
                    str(post_impl_utilization),
                    str(drc),
                    str(route_status),
                    str(clock_utilization),
                    str(floorplan),
                ],
            )
            self.assertEqual(record["failed_paths"], [])
            self.assertEqual(
                record["checked_counts"],
                {
                    "artifacts": 3,
                    "address_map": 1,
                    "timing": 2,
                    "utilization": 2,
                    "drc": 1,
                    "route_status": 1,
                    "clock_utilization": 1,
                    "floorplan": 1,
                },
            )
            self.assertEqual(
                record["diagnostic_summary"],
                {
                    "checked_count": 12,
                    "passed_count": 12,
                    "failed_count": 0,
                    "failure_count": 0,
                    "checked_counts_sum": 12,
                    "checked_counts_sum_matches": True,
                    "checked_counts_categories_match": True,
                    "checked_counts_strict_numbers": True,
                    "checked_counts_positive": True,
                    "checked_counts_match_categories": True,
                    "count_balance_valid": True,
                    "path_counts_valid": True,
                    "checked_paths_match_passed_paths": True,
                    "no_failed_paths": True,
                    "no_failures": True,
                    "valid": True,
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
                        "floorplan",
                    ],
                    "required_category_count": 8,
                    "present": {
                        "artifacts": True,
                        "address_map": True,
                        "timing": True,
                        "utilization": True,
                        "drc": True,
                        "route_status": True,
                        "clock_utilization": True,
                        "floorplan": True,
                    },
                    "present_category_count": 8,
                    "missing_category_count": 0,
                    "passing_counts": {
                        "artifacts": 3,
                        "address_map": 1,
                        "timing": 2,
                        "utilization": 2,
                        "drc": 1,
                        "route_status": 1,
                        "clock_utilization": 1,
                        "floorplan": 1,
                    },
                    "failing_counts": {
                        "artifacts": 0,
                        "address_map": 0,
                        "timing": 0,
                        "utilization": 0,
                        "drc": 0,
                        "route_status": 0,
                        "clock_utilization": 0,
                        "floorplan": 0,
                    },
                    "present_required_categories": [
                        "artifacts",
                        "address_map",
                        "timing",
                        "utilization",
                        "drc",
                        "route_status",
                        "clock_utilization",
                        "floorplan",
                    ],
                    "failing_categories": [],
                    "missing_required_categories": [],
                    "all_required_present": True,
                },
            )
            self.assertEqual(
                record["artifact_suffixes"],
                {
                    "required_suffixes": [".bit", ".xsa", ".dcp"],
                    "required_suffix_count": 3,
                    "suffix_counts": {".bit": 1, ".xsa": 1, ".dcp": 1},
                    "passing_suffix_counts": {".bit": 1, ".xsa": 1, ".dcp": 1},
                    "failing_suffix_counts": {},
                    "required_suffixes_present": {
                        ".bit": True,
                        ".xsa": True,
                        ".dcp": True,
                    },
                    "present_suffix_count": 3,
                    "missing_suffix_count": 0,
                    "present_required_suffixes": [".bit", ".xsa", ".dcp"],
                    "failing_required_suffixes": [],
                    "missing_required_suffixes": [],
                    "all_required_suffixes_present": True,
                },
            )
            self.assertEqual(
                record["artifact_filenames"],
                {
                    "required_filenames": [
                        "hjpeg_kv260.bit",
                        "hjpeg_kv260.xsa",
                        "post_impl.dcp",
                    ],
                    "required_filename_count": 3,
                    "filename_counts": {
                        "hjpeg_kv260.bit": 1,
                        "hjpeg_kv260.xsa": 1,
                        "post_impl.dcp": 1,
                    },
                    "passing_filename_counts": {
                        "hjpeg_kv260.bit": 1,
                        "hjpeg_kv260.xsa": 1,
                        "post_impl.dcp": 1,
                    },
                    "failing_filename_counts": {},
                    "required_filenames_present": {
                        "hjpeg_kv260.bit": True,
                        "hjpeg_kv260.xsa": True,
                        "post_impl.dcp": True,
                    },
                    "present_filename_count": 3,
                    "missing_filename_count": 0,
                    "present_required_filenames": [
                        "hjpeg_kv260.bit",
                        "hjpeg_kv260.xsa",
                        "post_impl.dcp",
                    ],
                    "failing_required_filenames": [],
                    "missing_required_filenames": [],
                    "all_required_filenames_present": True,
                },
            )
            self.assertEqual(
                record["address_map_filenames"],
                {
                    "label": "address_map",
                    "required_filenames": ["hjpeg_kv260_address_map.rpt"],
                    "required_filename_count": 1,
                    "filename_counts": {"hjpeg_kv260_address_map.rpt": 1},
                    "passing_filename_counts": {"hjpeg_kv260_address_map.rpt": 1},
                    "failing_filename_counts": {},
                    "required_filenames_present": {
                        "hjpeg_kv260_address_map.rpt": True,
                    },
                    "present_filename_count": 1,
                    "missing_filename_count": 0,
                    "present_required_filenames": ["hjpeg_kv260_address_map.rpt"],
                    "failing_required_filenames": [],
                    "missing_required_filenames": [],
                    "all_required_filenames_present": True,
                },
            )
            self.assertEqual(
                record["report_filenames"],
                {
                    "timing": {
                        "label": "timing",
                        "required_filenames": [
                            "post_synth_timing_summary.rpt",
                            "post_impl_timing_summary.rpt",
                        ],
                        "required_filename_count": 2,
                        "filename_counts": {
                            "post_synth_timing_summary.rpt": 1,
                            "post_impl_timing_summary.rpt": 1,
                        },
                        "passing_filename_counts": {
                            "post_synth_timing_summary.rpt": 1,
                            "post_impl_timing_summary.rpt": 1,
                        },
                        "failing_filename_counts": {},
                        "required_filenames_present": {
                            "post_synth_timing_summary.rpt": True,
                            "post_impl_timing_summary.rpt": True,
                        },
                        "present_filename_count": 2,
                        "missing_filename_count": 0,
                        "present_required_filenames": [
                            "post_synth_timing_summary.rpt",
                            "post_impl_timing_summary.rpt",
                        ],
                        "failing_required_filenames": [],
                        "missing_required_filenames": [],
                        "all_required_filenames_present": True,
                    },
                    "utilization": {
                        "label": "utilization",
                        "required_filenames": [
                            "post_synth_utilization.rpt",
                            "post_impl_utilization.rpt",
                        ],
                        "required_filename_count": 2,
                        "filename_counts": {
                            "post_synth_utilization.rpt": 1,
                            "post_impl_utilization.rpt": 1,
                        },
                        "passing_filename_counts": {
                            "post_synth_utilization.rpt": 1,
                            "post_impl_utilization.rpt": 1,
                        },
                        "failing_filename_counts": {},
                        "required_filenames_present": {
                            "post_synth_utilization.rpt": True,
                            "post_impl_utilization.rpt": True,
                        },
                        "present_filename_count": 2,
                        "missing_filename_count": 0,
                        "present_required_filenames": [
                            "post_synth_utilization.rpt",
                            "post_impl_utilization.rpt",
                        ],
                        "failing_required_filenames": [],
                        "missing_required_filenames": [],
                        "all_required_filenames_present": True,
                    },
                    "drc": {
                        "label": "drc",
                        "required_filenames": ["post_impl_drc.rpt"],
                        "required_filename_count": 1,
                        "filename_counts": {"post_impl_drc.rpt": 1},
                        "passing_filename_counts": {"post_impl_drc.rpt": 1},
                        "failing_filename_counts": {},
                        "required_filenames_present": {"post_impl_drc.rpt": True},
                        "present_filename_count": 1,
                        "missing_filename_count": 0,
                        "present_required_filenames": ["post_impl_drc.rpt"],
                        "failing_required_filenames": [],
                        "missing_required_filenames": [],
                        "all_required_filenames_present": True,
                    },
                    "route_status": {
                        "label": "route_status",
                        "required_filenames": ["post_impl_route_status.rpt"],
                        "required_filename_count": 1,
                        "filename_counts": {"post_impl_route_status.rpt": 1},
                        "passing_filename_counts": {"post_impl_route_status.rpt": 1},
                        "failing_filename_counts": {},
                        "required_filenames_present": {
                            "post_impl_route_status.rpt": True,
                        },
                        "present_filename_count": 1,
                        "missing_filename_count": 0,
                        "present_required_filenames": ["post_impl_route_status.rpt"],
                        "failing_required_filenames": [],
                        "missing_required_filenames": [],
                        "all_required_filenames_present": True,
                    },
                    "clock_utilization": {
                        "label": "clock_utilization",
                        "required_filenames": ["post_impl_clock_utilization.rpt"],
                        "required_filename_count": 1,
                        "filename_counts": {"post_impl_clock_utilization.rpt": 1},
                        "passing_filename_counts": {
                            "post_impl_clock_utilization.rpt": 1,
                        },
                        "failing_filename_counts": {},
                        "required_filenames_present": {
                            "post_impl_clock_utilization.rpt": True,
                        },
                        "present_filename_count": 1,
                        "missing_filename_count": 0,
                        "present_required_filenames": [
                            "post_impl_clock_utilization.rpt",
                        ],
                        "failing_required_filenames": [],
                        "missing_required_filenames": [],
                        "all_required_filenames_present": True,
                    },
                    "floorplan": {
                        "label": "floorplan",
                        "required_filenames": ["post_impl_floorplan.rpt"],
                        "required_filename_count": 1,
                        "filename_counts": {"post_impl_floorplan.rpt": 1},
                        "passing_filename_counts": {
                            "post_impl_floorplan.rpt": 1,
                        },
                        "failing_filename_counts": {},
                        "required_filenames_present": {
                            "post_impl_floorplan.rpt": True,
                        },
                        "present_filename_count": 1,
                        "missing_filename_count": 0,
                        "present_required_filenames": [
                            "post_impl_floorplan.rpt",
                        ],
                        "failing_required_filenames": [],
                        "missing_required_filenames": [],
                        "all_required_filenames_present": True,
                    },
                },
            )
            self.assertEqual(
                record["hold_timing_filenames"],
                {
                    "label": "hold_timing",
                    "required_filenames": ["post_impl_timing_summary.rpt"],
                    "required_filename_count": 1,
                    "filename_counts": {"post_impl_timing_summary.rpt": 1},
                    "passing_filename_counts": {
                        "post_impl_timing_summary.rpt": 1,
                    },
                    "failing_filename_counts": {},
                    "required_filenames_present": {
                        "post_impl_timing_summary.rpt": True,
                    },
                    "present_filename_count": 1,
                    "missing_filename_count": 0,
                    "present_required_filenames": [
                        "post_impl_timing_summary.rpt",
                    ],
                    "failing_required_filenames": [],
                    "missing_required_filenames": [],
                    "all_required_filenames_present": True,
                },
            )
            self.assertEqual(
                record["arguments"],
                {
                    "artifacts": [str(artifact), str(xsa), str(dcp)],
                    "address_map": [str(address_map)],
                    "timing": [str(post_synth_timing), str(post_impl_timing)],
                    "hold_timing": [str(post_impl_timing)],
                    "utilization": [
                        str(post_synth_utilization),
                        str(post_impl_utilization),
                    ],
                    "drc": [str(drc)],
                    "route_status": [str(route_status)],
                    "clock_utilization": [str(clock_utilization)],
                    "floorplan": [str(floorplan)],
                    "min_wns": 0.0,
                    "min_whs": 0.0,
                    "max_utilization": 90.0,
                    "clock_period_ns": 8.0,
                    "require_complete_evidence": False,
                },
            )
            self.assertTrue(record["complete_vivado_flow_evidence"])
            self.assertFalse(record["complete_vivado_flow_evidence_required"])
            self.assertTrue(record["route_status_counts_present"])
            self.assertTrue(record["floorplan_evidence_present"])
            self.assertTrue(record["address_map_hex_fields_consistent"])
            self.assertTrue(record["record_hashes_present"])
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
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_hold_timing_filenames"],
                [],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_hold_timing_filenames"],
                [],
            )
            self.assertEqual(record["clock_period_ns"], 8.0)
            self.assertEqual(record["clock_frequency_mhz"], 125.0)
            self.assertEqual(
                record["clock_target"],
                {
                    "clock_period_ns": 8.0,
                    "clock_frequency_mhz": 125.0,
                    "clock_period_finite": True,
                    "clock_period_positive": True,
                    "clock_frequency_finite": True,
                    "clock_frequency_positive": True,
                    "period_frequency_match": True,
                    "valid": True,
                },
            )
            self.assertTrue(record["clock_target_valid"])
            for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES:
                self.assertGreaterEqual(len(record[category]), 1)
                for item in record[category]:
                    self.assertIsInstance(item.get("path"), str)
                    self.assertIsInstance(item.get("path_resolved"), str)
                    self.assertEqual(
                        item["path_resolved"],
                        str(Path(item["path"]).resolve(strict=False)),
                    )
                    if item["passed"]:
                        sha256 = item.get("sha256")
                        self.assertIsInstance(sha256, str)
                        self.assertEqual(len(sha256), 64)
                        self.assertTrue(
                            all(
                                char in "0123456789abcdef"
                                for char in sha256
                            )
                        )
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
            self.assertEqual(record["artifacts"][2]["path"], str(dcp))
            self.assertEqual(record["artifacts"][2]["byte_length"], len(b"checkpoint"))
            self.assertEqual(
                record["artifacts"][2]["sha256"],
                hashlib.sha256(b"checkpoint").hexdigest(),
            )
            self.assertTrue(record["artifacts"][2]["exists"])
            self.assertTrue(record["artifacts"][2]["passed"])
            self.assertEqual(record["address_map"][0]["path"], str(address_map))
            self.assertEqual(
                record["address_map"][0]["sha256"],
                hashlib.sha256(address_map.read_bytes()).hexdigest(),
            )
            self.assertTrue(record["address_map"][0]["exists"])
            self.assertTrue(record["address_map"][0]["passed"])
            self.assertEqual(
                record["address_map"][0]["required_interfaces"],
                ["hjpeg_0/s_axi_lite", "axi_dma_0/s_axi_lite"],
            )
            self.assertEqual(
                record["address_map"][0]["present_interfaces"],
                ["axi_dma_0/s_axi_lite", "hjpeg_0/s_axi_lite"],
            )
            self.assertEqual(record["address_map"][0]["missing_interfaces"], [])
            self.assertEqual(record["address_map"][0]["duplicate_interfaces"], [])
            self.assertEqual(record["address_map"][0]["invalid_range_interfaces"], [])
            self.assertEqual(record["address_map"][0]["range_overlaps"], [])
            self.assertEqual(
                record["address_map"][0]["entries"][0],
                {
                    "interface": "hjpeg_0/s_axi_lite",
                    "base_address": 0xA0000000,
                    "base_address_hex": "0xa0000000",
                    "high_address": 0xA000FFFF,
                    "high_address_hex": "0xa000ffff",
                    "high_address_valid": True,
                    "aperture_bytes": 0x10000,
                },
            )
            self.assertEqual(record["timing"][0]["path"], str(post_synth_timing))
            self.assertTrue(record["timing"][0]["exists"])
            self.assertEqual(record["timing"][0]["wns_ns"], 0.125)
            self.assertEqual(record["timing"][0]["whs_ns"], 0.05)
            self.assertEqual(record["timing"][0]["min_whs_ns"], 0.0)
            self.assertFalse(record["timing"][0]["check_whs"])
            timing_bytes = post_synth_timing.read_bytes()
            self.assertEqual(record["timing"][0]["byte_length"], len(timing_bytes))
            self.assertEqual(
                record["timing"][0]["sha256"],
                hashlib.sha256(timing_bytes).hexdigest(),
            )
            self.assertEqual(record["timing"][1]["path"], str(post_impl_timing))
            self.assertTrue(record["timing"][1]["check_whs"])
            self.assertEqual(record["utilization"][0]["path"], str(post_synth_utilization))
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
                record["route_status"][0]["required_counts"],
                [
                    "number_of_unrouted_nets",
                    "number_of_nets_with_routing_errors",
                ],
            )
            self.assertEqual(
                record["route_status"][0]["counts"],
                {
                    "number_of_unrouted_nets": 0,
                    "number_of_nets_with_routing_errors": 0,
                },
            )
            self.assertEqual(record["route_status"][0]["missing_counts"], [])
            self.assertTrue(record["route_status"][0]["passed"])
            self.assertEqual(record["clock_utilization"][0]["path"], str(clock_utilization))
            self.assertEqual(
                record["clock_utilization"][0]["sha256"],
                hashlib.sha256(clock_utilization.read_bytes()).hexdigest(),
            )
            self.assertTrue(record["clock_utilization"][0]["exists"])
            self.assertTrue(record["clock_utilization"][0]["passed"])
            self.assertEqual(record["floorplan"][0]["path"], str(floorplan))
            self.assertEqual(
                record["floorplan"][0]["sha256"],
                hashlib.sha256(floorplan.read_bytes()).hexdigest(),
            )
            self.assertEqual(record["floorplan"][0]["pblock_count"], 0)
            self.assertEqual(record["floorplan"][0]["placed_cell_count"], 12345)
            self.assertTrue(record["floorplan"][0]["exists"])
            self.assertTrue(record["floorplan"][0]["passed"])

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
                    "floorplan",
                ],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_suffixes"],
                [".bit", ".xsa", ".dcp"],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_filenames"],
                ["hjpeg_kv260.bit", "hjpeg_kv260.xsa", "post_impl.dcp"],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_address_map_filenames"],
                ["hjpeg_kv260_address_map.rpt"],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_report_filenames"],
                {
                    "timing": [
                        "post_synth_timing_summary.rpt",
                        "post_impl_timing_summary.rpt",
                    ],
                    "utilization": [
                        "post_synth_utilization.rpt",
                        "post_impl_utilization.rpt",
                    ],
                    "drc": ["post_impl_drc.rpt"],
                    "route_status": ["post_impl_route_status.rpt"],
                    "clock_utilization": ["post_impl_clock_utilization.rpt"],
                    "floorplan": ["post_impl_floorplan.rpt"],
                },
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_hold_timing_filenames"],
                ["post_impl_timing_summary.rpt"],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_categories"], []
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_suffixes"], []
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_filenames"], []
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_address_map_filenames"],
                [],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_report_filenames"],
                {},
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_hold_timing_filenames"],
                [],
            )
            self.assertTrue(record["evidence_categories"]["present"]["timing"])
            self.assertFalse(record["evidence_categories"]["present"]["artifacts"])
            self.assertFalse(record["artifact_suffixes"]["all_required_suffixes_present"])
            self.assertFalse(record["route_status_counts_present"])
            self.assertFalse(record["floorplan_evidence_present"])
            self.assertFalse(record["address_map_hex_fields_consistent"])
            self.assertFalse(record["record_hashes_present"])
            self.assertEqual(
                record["diagnostic_summary"],
                {
                    "checked_count": 1,
                    "passed_count": 1,
                    "failed_count": 0,
                    "failure_count": 10,
                    "checked_counts_sum": 1,
                    "checked_counts_sum_matches": True,
                    "checked_counts_categories_match": True,
                    "checked_counts_strict_numbers": True,
                    "checked_counts_positive": False,
                    "checked_counts_match_categories": True,
                    "count_balance_valid": True,
                    "path_counts_valid": True,
                    "checked_paths_match_passed_paths": True,
                    "no_failed_paths": True,
                    "no_failures": False,
                    "valid": False,
                },
            )
            self.assertTrue(
                any("missing required categories" in failure for failure in record["failures"])
            )
            self.assertTrue(
                any("missing required artifact suffixes" in failure for failure in record["failures"])
            )
            self.assertTrue(
                any("route-status counts" in failure for failure in record["failures"])
            )
            self.assertTrue(
                any("floorplan placed-cell" in failure for failure in record["failures"])
            )
            self.assertTrue(
                any("address-map hex fields" in failure for failure in record["failures"])
            )
            self.assertTrue(
                any("file metadata" in failure for failure in record["failures"])
            )

    def test_complete_vivado_flow_evidence_requires_post_impl_hold_timing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "hjpeg_kv260.bit"
            xsa = root / "hjpeg_kv260.xsa"
            dcp = root / "post_impl.dcp"
            address_map = root / "hjpeg_kv260_address_map.rpt"
            post_synth_timing = root / "post_synth_timing_summary.rpt"
            post_impl_timing = root / "post_impl_timing_summary.rpt"
            post_synth_utilization = root / "post_synth_utilization.rpt"
            post_impl_utilization = root / "post_impl_utilization.rpt"
            drc = root / "post_impl_drc.rpt"
            route_status = root / "post_impl_route_status.rpt"
            clock_utilization = root / "post_impl_clock_utilization.rpt"
            floorplan = root / "post_impl_floorplan.rpt"
            artifact.write_bytes(b"bitstream")
            xsa.write_bytes(b"xsa")
            dcp.write_bytes(b"checkpoint")
            address_map.write_text(ADDRESS_MAP_REPORT)
            post_synth_timing.write_text(TIMING_TABLE)
            post_impl_timing.write_text(TIMING_TABLE)
            post_synth_utilization.write_text(VIVADO_UTILIZATION_TABLE)
            post_impl_utilization.write_text(VIVADO_UTILIZATION_TABLE)
            drc.write_text(DRC_CLEAN_REPORT)
            route_status.write_text(ROUTE_STATUS_CLEAN_REPORT)
            clock_utilization.write_text("Clock Utilization\n")
            floorplan.write_text(FLOORPLAN_REPORT)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--artifact",
                            str(artifact),
                            "--artifact",
                            str(xsa),
                            "--artifact",
                            str(dcp),
                            "--address-map",
                            str(address_map),
                            "--timing",
                            str(post_synth_timing),
                            "--timing",
                            str(post_impl_timing),
                            "--utilization",
                            str(post_synth_utilization),
                            "--utilization",
                            str(post_impl_utilization),
                            "--drc",
                            str(drc),
                            "--route-status",
                            str(route_status),
                            "--clock-utilization",
                            str(clock_utilization),
                            "--floorplan",
                            str(floorplan),
                            "--require-complete-evidence",
                            "--json",
                        ]
                    ),
                    1,
                )

            record = json.loads(stdout.getvalue())
            self.assertFalse(record["passed"])
            self.assertFalse(record["complete_vivado_flow_evidence"])
            self.assertTrue(record["evidence_categories"]["all_required_present"])
            self.assertTrue(record["artifact_suffixes"]["all_required_suffixes_present"])
            self.assertTrue(record["artifact_filenames"]["all_required_filenames_present"])
            self.assertTrue(record["address_map_filenames"]["all_required_filenames_present"])
            self.assertTrue(
                all(
                    item["all_required_filenames_present"]
                    for item in record["report_filenames"].values()
                )
            )
            self.assertEqual(
                record["hold_timing_filenames"]["missing_required_filenames"],
                ["post_impl_timing_summary.rpt"],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_hold_timing_filenames"],
                ["post_impl_timing_summary.rpt"],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_hold_timing_filenames"],
                [],
            )
            self.assertTrue(
                any("missing required hold-timing filenames" in failure for failure in record["failures"])
            )

    def test_complete_vivado_flow_evidence_rejects_failing_supplied_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "hjpeg_kv260.bit"
            xsa = root / "hjpeg_kv260.xsa"
            dcp = root / "post_impl.dcp"
            empty_xsa = root / "empty.xsa"
            address_map = root / "hjpeg_kv260_address_map.rpt"
            post_synth_timing = root / "post_synth_timing_summary.rpt"
            post_impl_timing = root / "post_impl_timing_summary.rpt"
            bad_timing = root / "bad_timing.rpt"
            post_synth_utilization = root / "post_synth_utilization.rpt"
            post_impl_utilization = root / "post_impl_utilization.rpt"
            drc = root / "post_impl_drc.rpt"
            route_status = root / "post_impl_route_status.rpt"
            clock_utilization = root / "post_impl_clock_utilization.rpt"
            floorplan = root / "post_impl_floorplan.rpt"
            artifact.write_bytes(b"bitstream")
            xsa.write_bytes(b"xsa")
            dcp.write_bytes(b"checkpoint")
            empty_xsa.write_bytes(b"")
            address_map.write_text(ADDRESS_MAP_REPORT)
            post_synth_timing.write_text(TIMING_TABLE)
            post_impl_timing.write_text(TIMING_TABLE)
            bad_timing.write_text("WNS(ns): -0.010\nWHS(ns): 0.010\n")
            post_synth_utilization.write_text(VIVADO_UTILIZATION_TABLE)
            post_impl_utilization.write_text(VIVADO_UTILIZATION_TABLE)
            drc.write_text(DRC_CLEAN_REPORT)
            route_status.write_text(ROUTE_STATUS_CLEAN_REPORT)
            clock_utilization.write_text("Clock Utilization\n")
            floorplan.write_text(FLOORPLAN_REPORT)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--artifact",
                            str(artifact),
                            "--artifact",
                            str(xsa),
                            "--artifact",
                            str(dcp),
                            "--artifact",
                            str(empty_xsa),
                            "--address-map",
                            str(address_map),
                            "--timing",
                            str(post_synth_timing),
                            "--timing",
                            str(post_impl_timing),
                            "--timing",
                            str(bad_timing),
                            "--hold-timing",
                            str(post_impl_timing),
                            "--utilization",
                            str(post_synth_utilization),
                            "--utilization",
                            str(post_impl_utilization),
                            "--drc",
                            str(drc),
                            "--route-status",
                            str(route_status),
                            "--clock-utilization",
                            str(clock_utilization),
                            "--floorplan",
                            str(floorplan),
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
                record["complete_vivado_flow_evidence_missing_categories"], []
            )
            self.assertEqual(record["complete_vivado_flow_evidence_missing_suffixes"], [])
            self.assertEqual(record["complete_vivado_flow_evidence_missing_filenames"], [])
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_address_map_filenames"],
                [],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_report_filenames"],
                {},
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_hold_timing_filenames"],
                [],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_categories"],
                ["artifacts", "timing"],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_suffixes"], [".xsa"]
            )
            self.assertEqual(record["complete_vivado_flow_evidence_failing_filenames"], [])
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_address_map_filenames"],
                [],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_report_filenames"],
                {},
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_failing_hold_timing_filenames"],
                [],
            )
            self.assertTrue(record["evidence_categories"]["all_required_present"])
            self.assertTrue(record["artifact_suffixes"]["all_required_suffixes_present"])
            self.assertTrue(record["artifact_filenames"]["all_required_filenames_present"])
            self.assertTrue(record["address_map_filenames"]["all_required_filenames_present"])
            self.assertTrue(
                all(
                    item["all_required_filenames_present"]
                    for item in record["report_filenames"].values()
                )
            )
            self.assertEqual(record["artifact_suffixes"]["failing_suffix_counts"], {".xsa": 1})
            self.assertTrue(
                any("failing required categories" in failure for failure in record["failures"])
            )
            self.assertTrue(
                any("failing required artifact suffixes" in failure for failure in record["failures"])
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

    def test_route_status_counts_present_requires_strict_zero_counts(self) -> None:
        self.assertTrue(
            check_reports.route_status_counts_present(
                [
                    {
                        "passed": True,
                        "counts": {
                            "number_of_unrouted_nets": 0,
                            "number_of_nets_with_routing_errors": 0,
                        },
                        "missing_counts": [],
                        "required_counts": list(
                            check_reports.REQUIRED_ROUTE_STATUS_COUNTS
                        ),
                    }
                ]
            )
        )
        self.assertFalse(
            check_reports.route_status_counts_present(
                [
                    {
                        "passed": True,
                        "counts": {
                            "number_of_unrouted_nets": False,
                            "number_of_nets_with_routing_errors": 0,
                        },
                        "missing_counts": [],
                        "required_counts": list(
                            check_reports.REQUIRED_ROUTE_STATUS_COUNTS
                        ),
                    }
                ]
            )
        )

    def test_artifact_suffixes_require_strict_passed_booleans(self) -> None:
        record = check_reports.artifact_suffix_record(
            [
                {"path": "hjpeg_kv260.bit", "passed": "true"},
                {"path": "hjpeg_kv260.xsa", "passed": "true"},
                {"path": "post_impl.dcp", "passed": "true"},
            ]
        )

        self.assertEqual(
            record["missing_required_suffixes"],
            list(check_reports.REQUIRED_ARTIFACT_SUFFIXES),
        )
        self.assertEqual(record["present_required_suffixes"], [])
        self.assertEqual(record["failing_required_suffixes"], [".bit", ".xsa", ".dcp"])
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
        self.assertEqual(record["suffix_counts"], {".bit": 1, ".xsa": 1, ".dcp": 1})
        self.assertEqual(record["passing_suffix_counts"], {})
        self.assertEqual(
            record["failing_suffix_counts"],
            {".bit": 1, ".xsa": 1, ".dcp": 1},
        )
        self.assertTrue(
            all(not present for present in record["required_suffixes_present"].values())
        )

    def test_artifact_filenames_require_strict_passed_booleans(self) -> None:
        record = check_reports.artifact_filename_record(
            [
                {"path": "build/hjpeg_kv260.bit", "passed": "true"},
                {"path": "build/hjpeg_kv260.xsa", "passed": "true"},
                {"path": "build/post_impl.dcp", "passed": "true"},
            ]
        )

        self.assertEqual(
            record["missing_required_filenames"],
            list(check_reports.REQUIRED_ARTIFACT_FILENAMES),
        )
        self.assertEqual(record["present_required_filenames"], [])
        self.assertEqual(
            record["failing_required_filenames"],
            ["hjpeg_kv260.bit", "hjpeg_kv260.xsa", "post_impl.dcp"],
        )
        self.assertFalse(record["all_required_filenames_present"])
        self.assertEqual(
            record["required_filename_count"],
            len(check_reports.REQUIRED_ARTIFACT_FILENAMES),
        )
        self.assertEqual(record["present_filename_count"], 0)
        self.assertEqual(
            record["missing_filename_count"],
            len(check_reports.REQUIRED_ARTIFACT_FILENAMES),
        )
        self.assertEqual(
            record["filename_counts"],
            {"hjpeg_kv260.bit": 1, "hjpeg_kv260.xsa": 1, "post_impl.dcp": 1},
        )
        self.assertEqual(record["passing_filename_counts"], {})
        self.assertEqual(
            record["failing_filename_counts"],
            {"hjpeg_kv260.bit": 1, "hjpeg_kv260.xsa": 1, "post_impl.dcp": 1},
        )
        self.assertTrue(
            all(not present for present in record["required_filenames_present"].values())
        )

    def test_record_hashes_require_matching_resolved_paths(self) -> None:
        records = {}
        for category in check_reports.REQUIRED_EVIDENCE_CATEGORIES:
            path = Path(f"{category}.rpt")
            records[category] = [
                {
                    "path": str(path),
                    "path_resolved": str(path.resolve(strict=False)),
                    "exists": True,
                    "passed": True,
                    "byte_length": 1,
                    "sha256": "0" * 64,
                }
            ]
        self.assertTrue(check_reports.record_hashes_present(records))

        records["artifacts"][0]["path_resolved"] = str(
            Path("stale/hjpeg_kv260.bit").resolve(strict=False)
        )
        self.assertFalse(check_reports.record_hashes_present(records))

    def test_required_filenames_require_strict_passed_booleans(self) -> None:
        record = check_reports.required_filename_record(
            [
                {"path": "reports/post_route_timing_summary.rpt", "passed": "true"},
                {"path": "reports/post_route_timing_summary.rpt", "passed": 1},
                {"path": "reports/post_route_hold_timing_summary.rpt", "passed": True},
            ],
            (
                "post_route_timing_summary.rpt",
                "post_route_hold_timing_summary.rpt",
            ),
            "timing",
        )

        self.assertEqual(record["label"], "timing")
        self.assertEqual(
            record["filename_counts"],
            {
                "post_route_timing_summary.rpt": 2,
                "post_route_hold_timing_summary.rpt": 1,
            },
        )
        self.assertEqual(
            record["passing_filename_counts"],
            {"post_route_hold_timing_summary.rpt": 1},
        )
        self.assertEqual(
            record["failing_filename_counts"],
            {"post_route_timing_summary.rpt": 2},
        )
        self.assertEqual(
            record["required_filenames_present"],
            {
                "post_route_timing_summary.rpt": False,
                "post_route_hold_timing_summary.rpt": True,
            },
        )
        self.assertEqual(
            record["present_required_filenames"],
            ["post_route_hold_timing_summary.rpt"],
        )
        self.assertEqual(
            record["missing_required_filenames"],
            ["post_route_timing_summary.rpt"],
        )
        self.assertEqual(
            record["failing_required_filenames"],
            ["post_route_timing_summary.rpt"],
        )
        self.assertFalse(record["all_required_filenames_present"])

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
                    "floorplan",
                ],
            )
            self.assertEqual(
                record["complete_vivado_flow_evidence_missing_suffixes"],
                [".bit", ".xsa", ".dcp"],
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
                    "floorplan": 0,
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
                    "floorplan",
                ],
            )
            self.assertFalse(record["evidence_categories"]["present"]["artifacts"])
            self.assertFalse(record["evidence_categories"]["present"]["timing"])
            self.assertFalse(record["evidence_categories"]["all_required_present"])
            self.assertEqual(
                record["artifact_suffixes"],
                {
                    "required_suffixes": [".bit", ".xsa", ".dcp"],
                    "required_suffix_count": 3,
                    "suffix_counts": {".xsa": 1},
                    "passing_suffix_counts": {},
                    "failing_suffix_counts": {".xsa": 1},
                    "required_suffixes_present": {
                        ".bit": False,
                        ".xsa": False,
                        ".dcp": False,
                    },
                    "present_suffix_count": 0,
                    "missing_suffix_count": 3,
                    "present_required_suffixes": [],
                    "failing_required_suffixes": [".xsa"],
                    "missing_required_suffixes": [".bit", ".xsa", ".dcp"],
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

    def test_cli_json_deduplicates_timing_and_hold_timing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            timing = root / "post_impl_timing_summary.rpt"
            timing.write_text(TIMING_TABLE)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--timing",
                            str(timing),
                            "--hold-timing",
                            str(timing),
                            "--json",
                        ]
                    ),
                    0,
                )

            record = json.loads(stdout.getvalue())
            self.assertTrue(record["passed"])
            self.assertEqual(record["checked_count"], 1)
            self.assertEqual(record["passed_count"], 1)
            self.assertEqual(record["failed_count"], 0)
            self.assertEqual(record["failure_count"], 0)
            self.assertEqual(record["checked_paths"], [str(timing)])
            self.assertEqual(record["passed_paths"], [str(timing)])
            self.assertEqual(record["failed_paths"], [])
            self.assertEqual(record["checked_counts"]["timing"], 1)
            self.assertEqual(len(record["timing"]), 1)
            self.assertTrue(record["timing"][0]["check_whs"])
            self.assertTrue(record["timing"][0]["passed"])
            self.assertEqual(record["arguments"]["timing"], [str(timing)])
            self.assertEqual(record["arguments"]["hold_timing"], [str(timing)])
            self.assertEqual(
                record["hold_timing_filenames"]["present_required_filenames"],
                ["post_impl_timing_summary.rpt"],
            )

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
                    "required_suffixes": [".bit", ".xsa", ".dcp"],
                    "required_suffix_count": 3,
                    "suffix_counts": {".bit": 1},
                    "passing_suffix_counts": {},
                    "failing_suffix_counts": {".bit": 1},
                    "required_suffixes_present": {
                        ".bit": False,
                        ".xsa": False,
                        ".dcp": False,
                    },
                    "present_suffix_count": 0,
                    "missing_suffix_count": 3,
                    "present_required_suffixes": [],
                    "failing_required_suffixes": [".bit"],
                    "missing_required_suffixes": [".bit", ".xsa", ".dcp"],
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

    def test_cli_json_rejects_incomplete_address_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            address_map = root / "hjpeg_kv260_address_map.rpt"
            address_map.write_text("Address Map\nhjpeg_0/s_axi_lite\n")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--address-map",
                            str(address_map),
                            "--json",
                        ]
                    ),
                    1,
                )

            record = json.loads(stdout.getvalue())
            self.assertFalse(record["passed"])
            self.assertEqual(record["address_map"][0]["entries"], [])
            self.assertEqual(
                record["address_map"][0]["missing_interfaces"],
                ["hjpeg_0/s_axi_lite", "axi_dma_0/s_axi_lite"],
            )
            self.assertEqual(record["address_map"][0]["duplicate_interfaces"], [])
            self.assertEqual(record["address_map"][0]["invalid_range_interfaces"], [])
            self.assertEqual(record["address_map"][0]["range_overlaps"], [])
            self.assertFalse(record["address_map"][0]["passed"])
            self.assertTrue(
                any("address map missing hjpeg_0/s_axi_lite" in failure for failure in record["failures"])
            )
            self.assertTrue(
                any("address map missing axi_dma_0/s_axi_lite" in failure for failure in record["failures"])
            )

    def test_cli_json_rejects_ambiguous_address_map_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            address_map = root / "hjpeg_kv260_address_map.rpt"
            address_map.write_text(
                "Address Map\n"
                "| ps/M_AXI_HPM0_FPD | hjpeg_0/s_axi_lite/Reg | 0xA0000000 | 0xA000FFFF |\n"
                "| ps/M_AXI_HPM0_FPD | hjpeg_0/s_axi_lite/Reg | 0xA0020000 | 0xA002FFFF |\n"
                "| ps/M_AXI_HPM0_FPD | axi_dma_0/S_AXI_LITE/Reg | 0xA0008000 | 0xA0017FFF |\n"
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    check_reports.main(
                        [
                            "--address-map",
                            str(address_map),
                            "--json",
                        ]
                    ),
                    1,
                )

            record = json.loads(stdout.getvalue())
            self.assertFalse(record["passed"])
            self.assertEqual(
                record["address_map"][0]["duplicate_interfaces"],
                ["hjpeg_0/s_axi_lite"],
            )
            self.assertEqual(record["address_map"][0]["missing_interfaces"], [])
            self.assertEqual(len(record["address_map"][0]["range_overlaps"]), 1)
            self.assertEqual(
                record["address_map"][0]["range_overlaps"][0]["first_interface"],
                "hjpeg_0/s_axi_lite",
            )
            self.assertEqual(
                record["address_map"][0]["range_overlaps"][0]["second_interface"],
                "axi_dma_0/s_axi_lite",
            )
            self.assertTrue(
                any("duplicate hjpeg_0/s_axi_lite" in failure for failure in record["failures"])
            )
            self.assertTrue(
                any("overlaps axi_dma_0/s_axi_lite" in failure for failure in record["failures"])
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
            self.assertEqual(record["route_status"][0]["missing_counts"], [])
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
            self.assertEqual(
                record["timing"][0]["path_resolved"],
                str(missing_timing.resolve(strict=False)),
            )
            self.assertEqual(record["timing"][0]["min_wns_ns"], 0.0)
            self.assertFalse(record["timing"][0]["check_whs"])
            self.assertFalse(record["utilization"][0]["exists"])
            self.assertFalse(record["utilization"][0]["passed"])
            self.assertEqual(
                record["utilization"][0]["path_resolved"],
                str(missing_utilization.resolve(strict=False)),
            )
            self.assertEqual(record["utilization"][0]["rows"], [])
            self.assertFalse(record["clock_utilization"][0]["exists"])
            self.assertFalse(record["clock_utilization"][0]["passed"])
            self.assertEqual(
                record["clock_utilization"][0]["path_resolved"],
                str(missing_clock_utilization.resolve(strict=False)),
            )
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

    def test_cli_numeric_helpers_report_malformed_values(self) -> None:
        for helper in (
            check_reports.finite_float,
            check_reports.positive_float,
            check_reports.nonnegative_float,
        ):
            with self.subTest(helper=helper.__name__):
                with self.assertRaisesRegex(
                    argparse.ArgumentTypeError, "finite"
                ):
                    helper("not-a-number")

    def test_strict_json_dumps_rejects_nonfinite_numbers(self) -> None:
        with self.assertRaisesRegex(ValueError, "Out of range float values"):
            check_reports.strict_json_dumps({"wns": float("nan")})

    def test_cli_rejects_invalid_utilization_thresholds(self) -> None:
        for value in ["-0.001", "nan", "inf", "-inf"]:
            with self.subTest(value=value):
                with self.assertRaises(SystemExit):
                    check_reports.main([f"--max-utilization={value}", "--json"])


if __name__ == "__main__":
    unittest.main()
