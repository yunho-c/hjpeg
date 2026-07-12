#!/usr/bin/env python3
"""Calculate reproducible architecture budgets for the KV260 4K60 target."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass


TARGET_WIDTH = 3840
TARGET_HEIGHT = 2160
TARGET_FPS = 60
PACKED_RGB_BYTES_PER_PIXEL = 4
TRANSFORM_INITIATION_CYCLES = 16
K26_RAMB36_TILES = 144
RAMB36_9BIT_DEPTH = 4096


@dataclass(frozen=True)
class PhysicalBaseline:
    sampling: str
    frame_cycles: int
    jpeg_bytes: int
    mcu_width: int
    mcu_height: int
    blocks_per_mcu: int


BASELINE_WIDTH = 1920
BASELINE_HEIGHT = 1080
BASELINES = (
    PhysicalBaseline("4:2:0", 2_210_885, 151_020, 16, 16, 6),
    PhysicalBaseline("4:4:4", 3_224_557, 168_562, 8, 8, 3),
)


def ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def estimate_raster_ramb36(width: int, sampling: str) -> int:
    """Estimate inferred 9-bit RAMB36s from the current bank/depth topology."""
    if sampling == "4:4:4":
        bank_columns = ceil_div(width, 8)
        depth_per_bank = 2 * 8 * bank_columns
    elif sampling == "4:2:0":
        bank_columns = ceil_div(width, 4)
        depth_per_bank = 2 * 8 * bank_columns
    else:
        raise ValueError(f"unsupported sampling mode: {sampling}")

    memories = 8 * 3
    return memories * ceil_div(depth_per_bank, RAMB36_9BIT_DEPTH)


def analyze(clock_hz: int = 100_000_000) -> dict[str, object]:
    if clock_hz <= 0:
        raise ValueError("clock_hz must be positive")

    target_pixels = TARGET_WIDTH * TARGET_HEIGHT
    baseline_pixels = BASELINE_WIDTH * BASELINE_HEIGHT
    pixel_scale = target_pixels / baseline_pixels
    target_pixel_rate = target_pixels * TARGET_FPS
    frame_cycle_budget = clock_hz / TARGET_FPS
    transform_capacity = clock_hz / TRANSFORM_INITIATION_CYCLES

    modes: list[dict[str, object]] = []
    for baseline in BASELINES:
        mcu_columns = ceil_div(TARGET_WIDTH, baseline.mcu_width)
        mcu_rows = ceil_div(TARGET_HEIGHT, baseline.mcu_height)
        mcus = mcu_columns * mcu_rows
        blocks = mcus * baseline.blocks_per_mcu
        block_rate = blocks * TARGET_FPS
        scaled_cycles = baseline.frame_cycles * pixel_scale
        projected_jpeg_bytes = baseline.jpeg_bytes * pixel_scale
        raster_ramb36 = estimate_raster_ramb36(TARGET_WIDTH, baseline.sampling)

        modes.append(
            {
                "sampling": baseline.sampling,
                "mcu_columns": mcu_columns,
                "mcu_rows": mcu_rows,
                "mcus_per_frame": mcus,
                "blocks_per_frame": blocks,
                "blocks_per_second": block_rate,
                "frame_cycle_budget": frame_cycle_budget,
                "scaled_current_frame_cycles": scaled_cycles,
                "scaled_current_fps": clock_hz / scaled_cycles,
                "required_speedup_over_scaled_current": scaled_cycles / frame_cycle_budget,
                "minimum_transform_copies_at_current_ii": math.ceil(block_rate / transform_capacity),
                "projected_q85_jpeg_bytes": projected_jpeg_bytes,
                "projected_q85_output_bytes_per_second": projected_jpeg_bytes * TARGET_FPS,
                "estimated_raster_ramb36": raster_ramb36,
            }
        )

    legacy_dual_mode_raster_ramb36 = sum(int(mode["estimated_raster_ramb36"]) for mode in modes)
    unified_raster_ramb36 = max(int(mode["estimated_raster_ramb36"]) for mode in modes)
    input_bytes_per_frame = target_pixels * PACKED_RGB_BYTES_PER_PIXEL

    return {
        "target": {
            "width": TARGET_WIDTH,
            "height": TARGET_HEIGHT,
            "frames_per_second": TARGET_FPS,
            "clock_hz": clock_hz,
            "pixels_per_frame": target_pixels,
            "pixels_per_second": target_pixel_rate,
            "required_input_pixels_per_cycle": target_pixel_rate / clock_hz,
            "packed_rgb_bytes_per_frame": input_bytes_per_frame,
            "packed_rgb_bytes_per_second": input_bytes_per_frame * TARGET_FPS,
            "frame_cycle_budget": frame_cycle_budget,
            "fits_26_bit_dma_length": input_bytes_per_frame <= (1 << 26) - 1,
        },
        "current_architecture": {
            "transform_initiation_cycles": TRANSFORM_INITIATION_CYCLES,
            "transform_blocks_per_second": transform_capacity,
            "jpeg_output_bytes_per_second": clock_hz,
            "legacy_dual_mode_raster_ramb36": legacy_dual_mode_raster_ramb36,
            "estimated_unified_raster_ramb36": unified_raster_ramb36,
            "k26_ramb36_tiles": K26_RAMB36_TILES,
            "raster_ramb36_headroom_before_other_memories": K26_RAMB36_TILES - unified_raster_ramb36,
        },
        "modes": modes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clock-mhz", type=float, default=100.0)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not math.isfinite(args.clock_mhz) or args.clock_mhz <= 0:
        raise ValueError("clock MHz must be finite and positive")
    report = analyze(round(args.clock_mhz * 1_000_000))

    if args.json:
        print(json.dumps(report, allow_nan=False, indent=2, sort_keys=True))
        return 0

    target = report["target"]
    architecture = report["current_architecture"]
    print(
        f"{target['width']}x{target['height']}@{target['frames_per_second']} requires "
        f"{target['pixels_per_second'] / 1_000_000:.3f} Mpixel/s and "
        f"{target['required_input_pixels_per_cycle']:.3f} pixels/cycle."
    )
    for mode in report["modes"]:
        print(
            f"{mode['sampling']}: {mode['blocks_per_second'] / 1_000_000:.3f} Mblock/s, "
            f"{mode['minimum_transform_copies_at_current_ii']} transform copies, "
            f"{mode['required_speedup_over_scaled_current']:.3f}x current scaled throughput, "
            f"{mode['estimated_raster_ramb36']} raster RAMB36s."
        )
    print(
        "Separate raster modes require "
        f"{architecture['legacy_dual_mode_raster_ramb36']}/{architecture['k26_ramb36_tiles']} RAMB36s; "
        f"the unified store requires {architecture['estimated_unified_raster_ramb36']}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
