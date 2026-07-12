#!/usr/bin/env python3

import contextlib
import csv
import io
import json
import tempfile
import unittest
from dataclasses import replace
from unittest import mock
from pathlib import Path

import generate_performance_trace as performance


class GeneratePerformanceTraceTest(unittest.TestCase):
    def write_fixture(self, directory: Path) -> None:
        with (directory / "scenarios.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                (
                    "scenario",
                    "profile",
                    "frames",
                    "width",
                    "height",
                    "sampling",
                    "content",
                    "quality",
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
            writer.writerow(
                ("444", "quick", 1, 2, 1, "4:4:4", "deterministic-gradient", 50,
                 "always", 100_000_000, 0, 5, 6, 2, 2, 2, 2)
            )

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
        with (directory / "phases.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("scenario", "cycle", "raster_phase", "encoder_phase"))
            raster = (0, 0, 1, 2, 3, 0)
            encoder = (0, 1, 1, 3, 4, 10)
            for cycle in range(6):
                writer.writerow(("444", cycle, raster[cycle], encoder[cycle]))

    def test_reads_and_validates_capture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_fixture(directory)
            scenarios, samples, phases = performance.read_capture(directory)

            self.assertEqual(scenarios[0].frame_cycles, 6)
            self.assertEqual(scenarios[0].frames, 1)
            self.assertEqual(len(samples), 6 * len(performance.BOUNDARY_ORDER))
            self.assertEqual(samples[0].boundary, "rgb_input")
            self.assertEqual(len(phases), 6)
            self.assertEqual(phases[2].raster_phase, 1)

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
            scenarios, samples, phases = performance.read_capture(directory)
            metrics = performance.calculate_metrics(scenarios, samples, phases)
            scenario = metrics["scenarios"][0]

            self.assertEqual(scenario["frame_cycles"], 6)
            self.assertEqual(scenario["average_frame_cycles"], 6)
            self.assertAlmostEqual(scenario["frames_per_second"], 100_000_000 / 6)
            transform = next(stage for stage in scenario["stages"] if stage["name"] == "block_transform")
            self.assertEqual(transform["latency_cycles"]["p50"], 2)
            self.assertEqual(transform["initiation_interval_cycles"]["maximum"], 2)
            target = next(item for item in scenario["targets"] if item["name"] == "input_cycles_per_pixel")
            self.assertEqual(target["status"], "within")
            self.assertEqual(scenario["phase_metrics"]["raster_startup_to_first_mcu_cycles"], 2)
            self.assertEqual(scenario["phase_metrics"]["encoder_startup_to_first_entropy_block_cycles"], 2)
            self.assertEqual(scenario["phase_metrics"]["raster_phase_cycles"]["idle"], 3)

    def test_separates_frame_transitions_from_steady_mcu_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.write_fixture(directory)
            scenarios, samples, phases = performance.read_capture(directory)
            two_frame = replace(scenarios[0], frames=2, width=1, pixels=2)

            metrics = performance.calculate_metrics([two_frame], samples, phases)
            phase = metrics["scenarios"][0]["phase_metrics"]
            target = next(
                item
                for item in metrics["scenarios"][0]["targets"]
                if item["name"] == "steady_state_mcu_mean_ii"
            )

            self.assertEqual(phase["frame_transition_mcu_interval_cycles"]["maximum"], 3)
            self.assertEqual(phase["steady_state_mcu_interval_cycles"]["count"], 0)
            self.assertEqual(metrics["scenarios"][0]["average_frame_cycles"], 3)
            self.assertEqual(target["status"], "unknown")

    def test_renders_deterministic_perfetto_and_graph_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            capture = root / "capture"
            first = root / "first"
            second = root / "second"
            capture.mkdir()
            self.write_fixture(capture)
            scenarios, samples, phases = performance.read_capture(capture)

            performance.generate_artifacts(first, scenarios, samples, phases, dot_command=None)
            performance.generate_artifacts(second, scenarios, samples, phases, dot_command=None)

            round_trip_scenarios, round_trip_samples, round_trip_phases = performance.read_capture(first)

            self.assertEqual((first / "trace.json").read_bytes(), (second / "trace.json").read_bytes())
            self.assertEqual((first / "metrics.json").read_bytes(), (second / "metrics.json").read_bytes())
            self.assertEqual(round_trip_scenarios, scenarios)
            self.assertEqual(round_trip_samples, samples)
            self.assertEqual(round_trip_phases, phases)
            trace = json.loads((first / "trace.json").read_text(encoding="utf-8"))
            self.assertTrue(any(event.get("cat") == "transaction_latency" for event in trace["traceEvents"]))
            self.assertTrue(any(event.get("cat") == "fsm_phase" for event in trace["traceEvents"]))
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

    def test_steady_state_profile_covers_content_quality_and_sampling_matrix(self) -> None:
        self.assertEqual(len(performance.STEADY_STATE_SCENARIOS), 24)
        self.assertIn("steady-444-flat-q10", performance.STEADY_STATE_SCENARIOS)
        self.assertIn("steady-420-seeded-random-q90", performance.STEADY_STATE_SCENARIOS)
        args = performance.build_parser().parse_args(("--profile", "steady-state"))
        self.assertEqual(args.profile, "steady-state")

    def test_large_sustained_scenario_is_explicitly_selectable(self) -> None:
        name = "large-444-two-frame-seeded-random-q90"
        self.assertIn(name, performance.LARGE_SCENARIOS)
        args = performance.build_parser().parse_args(("--scenario", name))
        self.assertEqual(args.scenario, [name])

    def test_windows_simulation_uses_docker_launcher_and_container_capture_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            capture = repo_root / "build" / "capture"
            capture.mkdir(parents=True)
            completed = mock.Mock(returncode=0)
            with (
                mock.patch.object(performance.os, "name", "nt"),
                mock.patch.object(performance.shutil, "which", return_value="powershell.exe"),
                mock.patch.object(performance.subprocess, "run", return_value=completed) as run,
            ):
                performance._run_simulation(repo_root, capture, ("444",))

            command = run.call_args.args[0]
            environment = run.call_args.kwargs["env"]
            self.assertIn("test.ps1", " ".join(str(part) for part in command))
            self.assertEqual(environment["HJPEG_PERFORMANCE_CAPTURE_DIR"], "/workspace/build/capture")
            self.assertEqual(environment["HJPEG_PERFORMANCE_SCENARIOS"], "444")


if __name__ == "__main__":
    unittest.main()
