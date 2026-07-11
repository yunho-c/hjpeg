#!/usr/bin/env python3

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

import generate_performance_trace as performance


class GeneratePerformanceTraceTest(unittest.TestCase):
    def write_fixture(self, directory: Path) -> None:
        with (directory / "scenarios.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                (
                    "scenario",
                    "width",
                    "height",
                    "sampling",
                    "ready_pattern",
                    "clock_hz",
                    "first_input_cycle",
                    "last_output_cycle",
                    "frame_cycles",
                    "pixels",
                    "bytes",
                    "mcus",
                    "blocks",
                )
            )
            writer.writerow(("444", 2, 1, "4:4:4", "always", 100_000_000, 0, 5, 6, 2, 2, 2, 2))

        transfers = {
            "rgb_input": {0, 1},
            "transform_input": {1, 3},
            "dct_input": {1, 3},
            "dct_output": {3, 5},
            "quantize_input": {1, 3},
            "quantize_output": {2, 4},
            "zigzag_input": {1, 3},
            "zigzag_output": {1, 3},
            "transform_output": {3, 5},
            "mcu_output": {2, 5},
            "entropy_block_input": {2, 4},
            "entropy_run_output": {3, 5},
            "packer_output": {4, 5},
            "jpeg_output": {4, 5},
        }
        with (directory / "samples.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("scenario", "cycle", "boundary", "valid", "ready"))
            for cycle in range(6):
                for boundary in performance.BOUNDARY_ORDER:
                    writer.writerow(("444", cycle, boundary, int(cycle in transfers[boundary]), 1))

    def test_reads_and_validates_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_fixture(directory)
            scenarios, samples = performance.read_capture(directory)

            self.assertEqual(scenarios[0].frame_cycles, 6)
            self.assertEqual(len(samples), 6 * len(performance.BOUNDARY_ORDER))
            self.assertEqual(samples[0].boundary, "rgb_input")

    def test_rejects_inconsistent_scenario_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_fixture(directory)
            text = (directory / "scenarios.csv").read_text(encoding="utf-8").replace(",0,5,6,", ",0,5,7,")
            (directory / "scenarios.csv").write_text(text, encoding="utf-8")

            with self.assertRaisesRegex(performance.PerformanceTraceError, "inconsistent frame cycle"):
                performance.read_capture(directory)

    def test_coalesces_ready_valid_states(self) -> None:
        samples = [
            performance.Sample("444", 0, "rgb_input", True, True),
            performance.Sample("444", 1, "rgb_input", True, True),
            performance.Sample("444", 2, "rgb_input", True, False),
            performance.Sample("444", 3, "rgb_input", False, True),
        ]

        self.assertEqual(
            performance.coalesce_states(samples),
            [(0, 2, "transfer"), (2, 1, "blocked"), (3, 1, "starved")],
        )

    def test_calculates_latency_and_budget_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_fixture(directory)
            scenarios, samples = performance.read_capture(directory)
            metrics = performance.calculate_metrics(scenarios, samples)
            scenario = metrics["scenarios"][0]

            self.assertEqual(scenario["frame_cycles"], 6)
            self.assertAlmostEqual(scenario["frames_per_second"], 100_000_000 / 6)
            transform = next(stage for stage in scenario["stages"] if stage["name"] == "block_transform")
            self.assertEqual(transform["latency_cycles"]["p50"], 2)
            self.assertEqual(transform["initiation_interval_cycles"]["maximum"], 2)
            target = next(item for item in scenario["targets"] if item["name"] == "input_cycles_per_pixel")
            self.assertEqual(target["status"], "within")

    def test_renders_deterministic_perfetto_and_graph_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "capture"
            first = root / "first"
            second = root / "second"
            capture.mkdir()
            self.write_fixture(capture)
            scenarios, samples = performance.read_capture(capture)

            performance.generate_artifacts(first, scenarios, samples, dot_command=None)
            performance.generate_artifacts(second, scenarios, samples, dot_command=None)

            self.assertEqual((first / "trace.json").read_bytes(), (second / "trace.json").read_bytes())
            self.assertEqual((first / "metrics.json").read_bytes(), (second / "metrics.json").read_bytes())
            trace = json.loads((first / "trace.json").read_text(encoding="utf-8"))
            self.assertTrue(any(event.get("cat") == "transaction_latency" for event in trace["traceEvents"]))
            mermaid = (first / "pipeline-444.mmd").read_text(encoding="utf-8")
            self.assertIn("Block transform", mermaid)
            self.assertIn("cycles/pixel", mermaid)

    def test_removes_only_manifest_owned_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            stale = directory / "stale.json"
            unrelated = directory / "notes.txt"
            stale.write_text("old", encoding="utf-8")
            unrelated.write_text("keep", encoding="utf-8")
            (directory / "manifest.json").write_text(
                json.dumps({"files": [str(stale.resolve()), str((directory.parent / "outside.txt").resolve())]}),
                encoding="utf-8",
            )

            performance.clean_previous_artifacts(directory)

            self.assertFalse(stale.exists())
            self.assertTrue(unrelated.exists())
            self.assertFalse((directory / "manifest.json").exists())

    def test_cli_renders_existing_capture_without_graphviz(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "capture"
            output = root / "output"
            capture.mkdir()
            self.write_fixture(capture)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = performance.main(
                    [
                        "--capture-dir",
                        str(capture),
                        "--output-dir",
                        str(output),
                        "--scenario",
                        "444",
                        "--dot",
                        "definitely-missing-dot",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["scenarios"], ["444"])
            self.assertIsNone(report["graphs"]["444"]["svg"])
            self.assertIn("Graphviz executable", stderr.getvalue())
            self.assertTrue((output / "index.md").is_file())


if __name__ == "__main__":
    unittest.main()
