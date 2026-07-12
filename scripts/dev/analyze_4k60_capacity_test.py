#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import math
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("analyze_4k60_capacity.py")
SPEC = importlib.util.spec_from_file_location("analyze_4k60_capacity", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
capacity = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = capacity
SPEC.loader.exec_module(capacity)


class Analyze4k60CapacityTest(unittest.TestCase):
    def test_target_ingress_and_dma_budget(self) -> None:
        report = capacity.analyze()
        target = report["target"]

        self.assertEqual(target["pixels_per_frame"], 8_294_400)
        self.assertEqual(target["pixels_per_second"], 497_664_000)
        self.assertTrue(math.isclose(target["required_input_pixels_per_cycle"], 4.97664))
        self.assertEqual(target["packed_rgb_bytes_per_frame"], 33_177_600)
        self.assertEqual(target["packed_rgb_bytes_per_second"], 1_990_656_000)
        self.assertTrue(target["fits_26_bit_dma_length"])

    def test_mode_geometry_and_parallel_transform_floor(self) -> None:
        report = capacity.analyze()
        modes = {mode["sampling"]: mode for mode in report["modes"]}

        self.assertEqual(modes["4:2:0"]["mcus_per_frame"], 32_400)
        self.assertEqual(modes["4:2:0"]["blocks_per_frame"], 194_400)
        self.assertEqual(modes["4:2:0"]["minimum_transform_copies_at_current_ii"], 2)
        self.assertTrue(
            math.isclose(modes["4:2:0"]["required_speedup_over_scaled_current"], 5.306124)
        )

        self.assertEqual(modes["4:4:4"]["mcus_per_frame"], 129_600)
        self.assertEqual(modes["4:4:4"]["blocks_per_frame"], 388_800)
        self.assertEqual(modes["4:4:4"]["minimum_transform_copies_at_current_ii"], 4)
        self.assertTrue(
            math.isclose(modes["4:4:4"]["required_speedup_over_scaled_current"], 7.7389368)
        )

    def test_unified_raster_topology_recovers_bram_headroom(self) -> None:
        report = capacity.analyze()
        modes = {mode["sampling"]: mode for mode in report["modes"]}
        architecture = report["current_architecture"]

        self.assertEqual(modes["4:2:0"]["estimated_raster_ramb36"], 96)
        self.assertEqual(modes["4:4:4"]["estimated_raster_ramb36"], 48)
        self.assertEqual(architecture["legacy_dual_mode_raster_ramb36"], 144)
        self.assertEqual(architecture["estimated_unified_raster_ramb36"], 96)
        self.assertEqual(architecture["raster_ramb36_headroom_before_other_memories"], 48)

    def test_clock_changes_cycle_capacity_without_changing_frame_work(self) -> None:
        report = capacity.analyze(clock_hz=200_000_000)
        modes = {mode["sampling"]: mode for mode in report["modes"]}

        self.assertEqual(report["target"]["frame_cycle_budget"], 200_000_000 / 60)
        self.assertTrue(math.isclose(report["target"]["required_input_pixels_per_cycle"], 2.48832))
        self.assertEqual(modes["4:2:0"]["minimum_transform_copies_at_current_ii"], 1)
        self.assertEqual(modes["4:4:4"]["minimum_transform_copies_at_current_ii"], 2)

    def test_invalid_clock_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            capacity.analyze(clock_hz=0)


if __name__ == "__main__":
    unittest.main()
