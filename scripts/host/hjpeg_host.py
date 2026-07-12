#!/usr/bin/env python3
"""Host-side helpers for the hjpeg KV260 AXI-Lite/DMA integration.

This utility keeps the software contract close to the RTL register map:

* P6 PPM input is packed as one 32-bit AXI-stream beat per RGB pixel, with byte
  order R, G, B, unused. The unused byte is ignored by the KV260 RTL wrapper.
* AXI-Lite register writes configure `HjpegKv260AxiLiteTop`.
* JPEG output validation checks SOI/EOI, SOF0 dimensions and baseline shape,
  DQT/DHT table markers, optional JFIF APP0 signature, SOS, and non-empty
  entropy-coded scan data after a hardware run.

DMA buffer allocation and transfer submission are intentionally board-image
specific, so this script prepares and validates the payloads around that layer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mmap
import os
import shlex
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Sequence


AXI_LITE_APERTURE_BYTES = 0x1000

REG_CONTROL = 0x00
REG_STATUS = 0x04
REG_XSIZE = 0x08
REG_YSIZE = 0x0C
REG_QUALITY = 0x10
REG_RESTART_INTERVAL = 0x14

CONTROL_CLEAR_PROTOCOL_ERROR = 1 << 0
CONTROL_ENABLE_CHROMA_SUBSAMPLE = 1 << 1
CONTROL_EMIT_JFIF = 1 << 2

STATUS_BUSY = 1 << 0
STATUS_PROTOCOL_ERROR = 1 << 1

DEFAULT_MAX_FRAME_WIDTH = 1920
DEFAULT_MAX_FRAME_HEIGHT = 1080
VIVADO_REQUIRED_REPORT_FILENAMES = {
    "timing": ("post_synth_timing_summary.rpt", "post_impl_timing_summary.rpt"),
    "utilization": ("post_synth_utilization.rpt", "post_impl_utilization.rpt"),
    "drc": ("post_impl_drc.rpt",),
    "route_status": ("post_impl_route_status.rpt",),
    "clock_utilization": ("post_impl_clock_utilization.rpt",),
    "floorplan": ("post_impl_floorplan.rpt",),
}
VIVADO_REQUIRED_HOLD_TIMING_FILENAMES = ("post_impl_timing_summary.rpt",)
VIVADO_REQUIRED_EVIDENCE_CATEGORIES = (
    "artifacts",
    "address_map",
    "timing",
    "utilization",
    "drc",
    "route_status",
    "clock_utilization",
    "floorplan",
)
VIVADO_REQUIRED_ROUTE_STATUS_COUNTS = (
    "number_of_unrouted_nets",
    "number_of_nets_with_routing_errors",
)
RUN_STATUS_CHECK_CONTEXTS = [
    "after configuration",
    "before transfer",
    "after validation",
]
DECODER_OUTPUT_CAPTURE_CHARS = 4096

JPEG_HEADER_MARKER_ORDER = {
    0xE0: (0, "APP0"),
    0xDB: (1, "DQT"),
    0xC0: (2, "SOF0"),
    0xC4: (3, "DHT"),
    0xDD: (4, "DRI"),
    0xDA: (5, "SOS"),
}

ZIG_ZAG_ORDER = (
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
)

STANDARD_LUMINANCE_QUANT = (
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
)

STANDARD_CHROMINANCE_QUANT = (
    17, 18, 24, 47, 99, 99, 99, 99,
    18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99,
    47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,
)

STANDARD_DC_LUMINANCE_BITS = (0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
STANDARD_DC_CHROMINANCE_BITS = (0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0)
STANDARD_AC_LUMINANCE_BITS = (0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 125)
STANDARD_AC_CHROMINANCE_BITS = (0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 119)
STANDARD_DC_SYMBOLS = tuple(range(12))
STANDARD_AC_LUMINANCE_SYMBOLS = (
    0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12,
    0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07,
    0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08,
    0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0,
    0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0A, 0x16,
    0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
    0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39,
    0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
    0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59,
    0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
    0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79,
    0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
    0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98,
    0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7,
    0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6,
    0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5,
    0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4,
    0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
    0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA,
    0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
    0xF9, 0xFA,
)
STANDARD_AC_CHROMINANCE_SYMBOLS = (
    0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21,
    0x31, 0x06, 0x12, 0x41, 0x51, 0x07, 0x61, 0x71,
    0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91,
    0xA1, 0xB1, 0xC1, 0x09, 0x23, 0x33, 0x52, 0xF0,
    0x15, 0x62, 0x72, 0xD1, 0x0A, 0x16, 0x24, 0x34,
    0xE1, 0x25, 0xF1, 0x17, 0x18, 0x19, 0x1A, 0x26,
    0x27, 0x28, 0x29, 0x2A, 0x35, 0x36, 0x37, 0x38,
    0x39, 0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48,
    0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58,
    0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68,
    0x69, 0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78,
    0x79, 0x7A, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
    0x88, 0x89, 0x8A, 0x92, 0x93, 0x94, 0x95, 0x96,
    0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5,
    0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4,
    0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3,
    0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2,
    0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA,
    0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9,
    0xEA, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
    0xF9, 0xFA,
)


def jpeg_marker_name(marker: int) -> str:
    if 0xD0 <= marker <= 0xD7:
        return f"RST{marker - 0xD0}"
    if marker == 0xD8:
        return "SOI"
    if marker == 0xD9:
        return "EOI"
    if 0xE0 <= marker <= 0xEF:
        return f"APP{marker - 0xE0}"
    if marker == 0xDB:
        return "DQT"
    if marker == 0xDD:
        return "DRI"
    if marker == 0xC0:
        return "SOF0"
    if marker == 0xC4:
        return "DHT"
    if marker == 0xDA:
        return "SOS"
    return f"0xFF{marker:02X}"


@dataclass(frozen=True)
class PpmImage:
    width: int
    height: int
    rgb: bytes


@dataclass(frozen=True)
class JpegComponent:
    component_id: int
    horizontal_sampling: int
    vertical_sampling: int
    quantization_table: int


@dataclass(frozen=True)
class JpegHuffmanTable:
    table_class: int
    table_id: int
    symbol_count: int
    payload_sha256: str


@dataclass(frozen=True)
class JpegQuantizationTable:
    table_id: int
    precision: int
    byte_length: int
    payload_sha256: str


@dataclass(frozen=True)
class JpegScanComponent:
    component_id: int
    dc_table: int
    ac_table: int


@dataclass(frozen=True)
class JfifApp0Info:
    version_major: int
    version_minor: int
    density_units: int
    x_density: int
    y_density: int
    thumbnail_width: int
    thumbnail_height: int


@dataclass(frozen=True)
class JpegInfo:
    width: int
    height: int
    mcu_count: int
    sample_precision: int
    components: tuple[JpegComponent, ...]
    scan_components: tuple[JpegScanComponent, ...]
    spectral_start: int
    spectral_end: int
    successive_approximation: int
    quantization_tables: tuple[int, ...]
    quantization_table_order: tuple[int, ...]
    quantization_table_details: tuple[JpegQuantizationTable, ...]
    huffman_tables: tuple[JpegHuffmanTable, ...]
    huffman_table_order: tuple[tuple[int, int], ...]
    scan_data_bytes: int
    scan_data_sha256: str
    stuffed_ff_bytes: int
    byte_length: int
    sha256: str
    app0_segments: int
    jfif_app0_segments: int
    jfif_app0: JfifApp0Info | None
    dqt_segments: int
    sof0_segments: int
    dht_segments: int
    sos_segments: int
    dri_segments: int
    restart_interval: int | None
    restart_markers: int
    restart_marker_sequence: tuple[int, ...]
    marker_sequence: tuple[str, ...]


@dataclass(frozen=True)
class FileInfo:
    path: str
    path_resolved: str
    byte_length: int
    sha256: str


@dataclass(frozen=True)
class DecoderCommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float
    stdout_chars: int
    stderr_chars: int
    output_capture_chars: int
    stdout_truncated: bool
    stderr_truncated: bool


def make_test_image(width: int, height: int, pattern: str = "gradient-checker") -> PpmImage:
    if width <= 0 or height <= 0:
        raise ValueError("PPM dimensions must be positive")
    if pattern not in ("gradient-checker", "seeded-random"):
        raise ValueError(f"unsupported test-image pattern: {pattern}")

    rgb = bytearray()
    denom_x = max(width - 1, 1)
    denom_y = max(height - 1, 1)
    for index in range(width * height):
        x = index % width
        y = index // width
        if pattern == "seeded-random":
            value = (index ^ 0x5EED1234) & 0xFFFFFFFF
            value ^= (value << 13) & 0xFFFFFFFF
            value ^= value >> 17
            value ^= (value << 5) & 0xFFFFFFFF
            value &= 0xFFFFFFFF
            rgb.extend([value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF])
        else:
            checker = 48 if ((x // 8) ^ (y // 8)) & 1 else 0
            r = (x * 255) // denom_x
            g = (y * 255) // denom_y
            b = (((x + y) * 127) // max(width + height - 2, 1)) + checker
            rgb.extend([r & 0xFF, g & 0xFF, min(b, 255)])
    return PpmImage(width=width, height=height, rgb=bytes(rgb))


def require_supported_dimensions(
    width: int,
    height: int,
    max_width: int = DEFAULT_MAX_FRAME_WIDTH,
    max_height: int = DEFAULT_MAX_FRAME_HEIGHT,
) -> None:
    if max_width <= 0 or max_height <= 0:
        raise ValueError("maximum frame dimensions must be positive")
    if not 1 <= width <= max_width:
        raise ValueError(f"width must be in 1..{max_width}")
    if not 1 <= height <= max_height:
        raise ValueError(f"height must be in 1..{max_height}")


def write_ppm(image: PpmImage, output: Path) -> None:
    output.write_bytes(
        f"P6\n{image.width} {image.height}\n255\n".encode("ascii") + image.rgb
    )


def _read_ppm_token(stream: BinaryIO) -> bytes:
    token = bytearray()
    while True:
        byte = stream.read(1)
        if byte == b"":
            raise ValueError("unexpected EOF while reading PPM header")
        if byte == b"#":
            stream.readline()
            continue
        if not byte.isspace():
            token.extend(byte)
            break

    while True:
        byte = stream.read(1)
        if byte == b"" or byte.isspace():
            break
        if byte == b"#":
            stream.readline()
            break
        token.extend(byte)
    return bytes(token)


def read_ppm(
    path: Path,
    max_width: int | None = None,
    max_height: int | None = None,
) -> PpmImage:
    with path.open("rb") as stream:
        magic = _read_ppm_token(stream)
        if magic != b"P6":
            raise ValueError(f"{path}: expected binary P6 PPM, got {magic!r}")

        width = int(_read_ppm_token(stream))
        height = int(_read_ppm_token(stream))
        max_value = int(_read_ppm_token(stream))
        if width <= 0 or height <= 0:
            raise ValueError("PPM dimensions must be positive")
        if max_value != 255:
            raise ValueError("only 8-bit P6 PPM files with max value 255 are supported")
        if max_width is not None or max_height is not None:
            require_supported_dimensions(
                width,
                height,
                max_width if max_width is not None else width,
                max_height if max_height is not None else height,
            )

        expected = width * height * 3
        rgb = stream.read(expected)
        if len(rgb) != expected:
            raise ValueError(f"{path}: expected {expected} RGB bytes, found {len(rgb)}")
        trailing = stream.read(1)
        if trailing:
            raise ValueError(f"{path}: trailing data after RGB payload")
        return PpmImage(width=width, height=height, rgb=rgb)


def rgb_stream_bytes(image: PpmImage) -> bytes:
    stream = bytearray()
    for offset in range(0, len(image.rgb), 3):
        stream.extend(image.rgb[offset : offset + 3])
        stream.append(0)
    return bytes(stream)


def write_rgb_stream(image: PpmImage, output: Path) -> None:
    stream = rgb_stream_bytes(image)
    output.write_bytes(stream)


def _read_be16(data: bytes, offset: int) -> int:
    return (data[offset] << 8) | data[offset + 1]


def require_valid_huffman_code_counts(
    count_bytes: bytes,
    table_class: int,
    table_id: int,
) -> None:
    class_name = "DC" if table_class == 0 else "AC"
    open_codes = 1
    for bit_length, count in enumerate(count_bytes, start=1):
        open_codes = (open_codes << 1) - count
        if open_codes < 0:
            raise ValueError(
                f"JPEG {class_name} DHT table {table_id} oversubscribes Huffman codes at length {bit_length}"
            )


def require_baseline_huffman_symbols(
    symbol_bytes: bytes,
    table_class: int,
    table_id: int,
) -> None:
    class_name = "DC" if table_class == 0 else "AC"
    if table_class == 0:
        for symbol in symbol_bytes:
            if symbol > 11:
                raise ValueError(
                    f"JPEG DC DHT table {table_id} contains invalid category {symbol}"
                )
        return

    for symbol in symbol_bytes:
        magnitude_size = symbol & 0x0F
        if magnitude_size > 10:
            raise ValueError(
                f"JPEG AC DHT table {table_id} contains invalid magnitude size {magnitude_size}"
            )
        if magnitude_size == 0 and symbol not in (0x00, 0xF0):
            raise ValueError(
                f"JPEG {class_name} DHT table {table_id} contains invalid zero-size run symbol 0x{symbol:02x}"
            )


def jpeg_info(data: bytes) -> JpegInfo:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("JPEG output does not start with SOI")

    offset = 2
    dimensions: tuple[int, int] | None = None
    sample_precision: int | None = None
    components: tuple[JpegComponent, ...] = ()
    scan_components: tuple[JpegScanComponent, ...] = ()
    spectral_start: int | None = None
    spectral_end: int | None = None
    successive_approximation: int | None = None
    quantization_tables: set[int] = set()
    quantization_table_order: list[int] = []
    quantization_table_details: dict[int, tuple[int, int, str]] = {}
    huffman_tables: dict[tuple[int, int], tuple[int, str]] = {}
    huffman_table_order: list[tuple[int, int]] = []
    scan_data = bytearray()
    scan_data_bytes = 0
    stuffed_ff_bytes = 0
    app0_segments = 0
    jfif_app0_segments = 0
    jfif_app0: JfifApp0Info | None = None
    dqt_segments = 0
    sof0_segments = 0
    dht_segments = 0
    sos_segments = 0
    dri_segments = 0
    restart_interval: int | None = None
    restart_marker_sequence: list[int] = []
    saw_sos = False
    saw_eoi = False
    eoi_offset: int | None = None
    marker_order_phase = -1
    marker_sequence = ["SOI"]
    while offset < len(data):
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break

        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            saw_eoi = True
            eoi_offset = offset
            marker_sequence.append("EOI")
            break
        if saw_sos:
            raise ValueError(
                f"JPEG scan data is followed by unexpected {jpeg_marker_name(marker)} marker"
            )
        if 0xD0 <= marker <= 0xD7:
            if not saw_sos:
                raise ValueError(
                    "JPEG restart marker appears before entropy-coded scan data"
                )
            restart_marker_sequence.append(marker - 0xD0)
            marker_sequence.append(jpeg_marker_name(marker))
            continue
        if offset + 2 > len(data):
            break

        marker_sequence.append(jpeg_marker_name(marker))
        marker_order = JPEG_HEADER_MARKER_ORDER.get(marker)
        if marker_order is not None:
            next_phase, marker_name = marker_order
            if next_phase < marker_order_phase:
                raise ValueError(
                    f"JPEG {marker_name} marker appears out of baseline header order"
                )
            marker_order_phase = next_phase
        else:
            raise ValueError(
                f"JPEG {jpeg_marker_name(marker)} marker is not supported in hjpeg baseline header"
            )

        segment_length = _read_be16(data, offset)
        if segment_length < 2 or offset + segment_length > len(data):
            raise ValueError(f"invalid JPEG segment length at byte {offset - 1}")

        if marker == 0xE0:
            app0_segments += 1
            if data[offset + 2 : offset + 7] == b"JFIF\x00":
                if segment_length < 16:
                    raise ValueError("JPEG JFIF APP0 segment is too short")
                jfif_fields = data[offset + 7 : offset + 16]
                if jfif_fields != b"\x01\x01\x00\x00\x01\x00\x01\x00\x00":
                    raise ValueError(
                        "JPEG JFIF APP0 fields do not match hjpeg baseline header"
                    )
                thumbnail_bytes = data[offset + 14] * data[offset + 15] * 3
                expected_segment_length = 16 + thumbnail_bytes
                if segment_length != expected_segment_length:
                    raise ValueError(
                        "JPEG JFIF APP0 segment length does not match thumbnail size"
                    )
                jfif_app0_segments += 1
                jfif_app0 = JfifApp0Info(
                    version_major=data[offset + 7],
                    version_minor=data[offset + 8],
                    density_units=data[offset + 9],
                    x_density=_read_be16(data, offset + 10),
                    y_density=_read_be16(data, offset + 12),
                    thumbnail_width=data[offset + 14],
                    thumbnail_height=data[offset + 15],
                )
        if marker == 0xDB:
            dqt_segments += 1
            table_offset = offset + 2
            segment_end = offset + segment_length
            while table_offset < segment_end:
                table_info = data[table_offset]
                precision = (table_info >> 4) & 0x0F
                table_id = table_info & 0x0F
                if precision not in (0, 1):
                    raise ValueError("DQT segment has invalid table precision")
                if precision != 0:
                    raise ValueError(
                        f"DQT table {table_id} has precision {precision}, expected 0"
                    )
                table_bytes = 64 * (2 if precision else 1)
                table_payload = data[table_offset + 1 : table_offset + 1 + table_bytes]
                table_offset += 1 + table_bytes
                if table_offset > segment_end:
                    raise ValueError("DQT segment table overruns segment length")
                if any(value == 0 for value in table_payload):
                    raise ValueError(
                        f"JPEG DQT table {table_id} contains zero quantization value"
                    )
                if table_id in quantization_table_details:
                    raise ValueError(f"JPEG DQT table {table_id} is defined more than once")
                quantization_tables.add(table_id)
                quantization_table_order.append(table_id)
                quantization_table_details[table_id] = (
                    precision,
                    table_bytes,
                    hashlib.sha256(table_payload).hexdigest(),
                )
        if marker == 0xDD:
            if segment_length != 4:
                raise ValueError("DRI segment has invalid length")
            dri_segments += 1
            restart_interval = _read_be16(data, offset + 2)
        if marker == 0xC0:
            sof0_segments += 1
            if segment_length < 8:
                raise ValueError("SOF0 segment is too short")
            sample_precision = data[offset + 2]
            height = _read_be16(data, offset + 3)
            width = _read_be16(data, offset + 5)
            if width == 0 or height == 0:
                raise ValueError(
                    f"JPEG SOF0 dimensions are {width}x{height}, expected nonzero dimensions"
                )
            component_count = data[offset + 7]
            if segment_length != 8 + component_count * 3:
                raise ValueError("SOF0 segment length does not match component count")
            parsed_components = []
            for component_index in range(component_count):
                component_offset = offset + 8 + component_index * 3
                sampling = data[component_offset + 1]
                horizontal_sampling = (sampling >> 4) & 0x0F
                vertical_sampling = sampling & 0x0F
                if horizontal_sampling == 0 or vertical_sampling == 0:
                    raise ValueError(
                        f"JPEG SOF0 component {data[component_offset]} has zero sampling factor"
                    )
                parsed_components.append(
                    JpegComponent(
                        component_id=data[component_offset],
                        horizontal_sampling=horizontal_sampling,
                        vertical_sampling=vertical_sampling,
                        quantization_table=data[component_offset + 2],
                    )
                )
            dimensions = (width, height)
            components = tuple(parsed_components)

        if marker == 0xC4:
            dht_segments += 1
            table_offset = offset + 2
            segment_end = offset + segment_length
            while table_offset < segment_end:
                table_info = data[table_offset]
                table_class = (table_info >> 4) & 0x0F
                table_id = table_info & 0x0F
                if table_class not in (0, 1):
                    raise ValueError("DHT segment has invalid table class")
                if table_offset + 17 > segment_end:
                    raise ValueError("DHT segment is too short for code counts")
                count_bytes = data[table_offset + 1 : table_offset + 17]
                value_count = sum(count_bytes)
                if value_count == 0:
                    class_name = "DC" if table_class == 0 else "AC"
                    raise ValueError(
                        f"JPEG {class_name} DHT table {table_id} has no symbols"
                    )
                if value_count > 256:
                    class_name = "DC" if table_class == 0 else "AC"
                    raise ValueError(
                        f"JPEG {class_name} DHT table {table_id} has {value_count} symbols, expected at most 256"
                    )
                require_valid_huffman_code_counts(count_bytes, table_class, table_id)
                symbol_bytes = data[table_offset + 17 : table_offset + 17 + value_count]
                require_baseline_huffman_symbols(symbol_bytes, table_class, table_id)
                table_offset += 17 + value_count
                if table_offset > segment_end:
                    raise ValueError("DHT segment table overruns segment length")
                if (table_class, table_id) in huffman_tables:
                    class_name = "DC" if table_class == 0 else "AC"
                    raise ValueError(
                        f"JPEG {class_name} DHT table {table_id} is defined more than once"
                    )
                huffman_table_order.append((table_class, table_id))
                huffman_tables[(table_class, table_id)] = (
                    value_count,
                    hashlib.sha256(count_bytes + symbol_bytes).hexdigest(),
                )

        if marker == 0xDA:
            saw_sos = True
            sos_segments += 1
            if segment_length < 6:
                raise ValueError("SOS segment is too short")
            scan_component_count = data[offset + 2]
            if segment_length != 6 + scan_component_count * 2:
                raise ValueError("SOS segment length does not match component count")
            parsed_scan_components = []
            for component_index in range(scan_component_count):
                component_offset = offset + 3 + component_index * 2
                table_selector = data[component_offset + 1]
                parsed_scan_components.append(
                    JpegScanComponent(
                        component_id=data[component_offset],
                        dc_table=(table_selector >> 4) & 0x0F,
                        ac_table=table_selector & 0x0F,
                    )
                )
            scan_components = tuple(parsed_scan_components)
            spectral_offset = offset + 3 + scan_component_count * 2
            spectral_start = data[spectral_offset]
            spectral_end = data[spectral_offset + 1]
            successive_approximation = data[spectral_offset + 2]
            offset += segment_length
            while offset + 1 < len(data):
                if data[offset] != 0xFF:
                    scan_data.append(data[offset])
                    scan_data_bytes += 1
                    offset += 1
                    continue

                following = data[offset + 1]
                if following == 0x00:
                    scan_data.append(0xFF)
                    scan_data_bytes += 1
                    stuffed_ff_bytes += 1
                    offset += 2
                elif following == 0xFF:
                    offset += 1
                elif 0xD0 <= following <= 0xD7:
                    restart_marker_sequence.append(following - 0xD0)
                    marker_sequence.append(jpeg_marker_name(following))
                    offset += 2
                else:
                    break
            continue

        offset += segment_length

    if not saw_eoi:
        raise ValueError("JPEG output does not contain EOI")
    if eoi_offset != len(data):
        raise ValueError("JPEG output contains trailing data after EOI")
    if dimensions is None:
        raise ValueError("JPEG output does not contain a baseline SOF0 segment")
    if sof0_segments != 1:
        raise ValueError(f"JPEG SOF0 segment count is {sof0_segments}, expected 1")
    if sample_precision != 8:
        raise ValueError(
            f"JPEG SOF0 sample precision is {sample_precision}, expected 8"
        )
    if len(components) != 3:
        raise ValueError(
            f"JPEG SOF0 component count is {len(components)}, expected 3"
        )
    component_id_order = [component.component_id for component in components]
    if component_id_order != [1, 2, 3]:
        raise ValueError(
            f"JPEG SOF0 component IDs are {component_id_order}, expected [1, 2, 3]"
        )
    component_sampling = tuple(
        (component.horizontal_sampling, component.vertical_sampling)
        for component in components
    )
    if component_sampling not in (
        ((1, 1), (1, 1), (1, 1)),
        ((2, 2), (1, 1), (1, 1)),
    ):
        raise ValueError(
            f"JPEG SOF0 sampling factors are {component_sampling}, "
            "expected 4:4:4 or 4:2:0"
        )
    if dqt_segments == 0:
        raise ValueError("JPEG output does not contain a DQT segment")
    if quantization_tables != {0, 1}:
        actual = sorted(quantization_tables)
        raise ValueError(f"JPEG DQT table set is {actual}, expected tables 0 and 1")
    if dqt_segments != 2:
        raise ValueError(f"JPEG DQT segment count is {dqt_segments}, expected 2")
    if tuple(quantization_table_order) != (0, 1):
        raise ValueError(
            f"JPEG DQT table order is {quantization_table_order}, expected [0, 1]"
        )
    if dht_segments == 0:
        raise ValueError("JPEG output does not contain a DHT segment")
    expected_huffman_tables = {(0, 0), (0, 1), (1, 0), (1, 1)}
    if set(huffman_tables) != expected_huffman_tables:
        actual = sorted(huffman_tables)
        raise ValueError(
            f"JPEG DHT table set is {actual}, expected DC/AC tables 0 and 1"
        )
    if dht_segments != 4:
        raise ValueError(f"JPEG DHT segment count is {dht_segments}, expected 4")
    if tuple(huffman_table_order) != ((0, 0), (0, 1), (1, 0), (1, 1)):
        raise ValueError(
            f"JPEG DHT table order is {huffman_table_order}, "
            "expected DC0, DC1, AC0, AC1"
        )
    if app0_segments > 1:
        raise ValueError(f"JPEG APP0 segment count is {app0_segments}, expected 0 or 1")
    if app0_segments != jfif_app0_segments:
        raise ValueError("JPEG APP0 segment is not a JFIF APP0 segment")
    if dri_segments > 1:
        raise ValueError(f"JPEG DRI segment count is {dri_segments}, expected 0 or 1")
    if restart_interval == 0:
        raise ValueError("JPEG DRI restart interval is 0, expected no DRI segment")
    if restart_marker_sequence and restart_interval is None:
        raise ValueError("JPEG restart marker appears without a DRI segment")
    for marker_index, marker_id in enumerate(restart_marker_sequence):
        expected_marker_id = marker_index % 8
        if marker_id != expected_marker_id:
            raise ValueError(
                "JPEG restart marker sequence is "
                f"{restart_marker_sequence}, expected RST markers to increment "
                f"modulo 8 starting at RST0"
            )
    if not saw_sos:
        raise ValueError("JPEG output does not contain an SOS segment")
    if sos_segments != 1:
        raise ValueError(f"JPEG SOS segment count is {sos_segments}, expected 1")
    if (spectral_start, spectral_end, successive_approximation) != (0, 63, 0):
        raise ValueError(
            "JPEG SOS spectral selection/successive approximation is "
            f"{spectral_start}/{spectral_end}/{successive_approximation}, expected 0/63/0"
        )
    if scan_data_bytes == 0:
        raise ValueError("JPEG output does not contain entropy-coded scan data")
    component_ids = {component.component_id for component in components}
    scan_component_ids = [component.component_id for component in scan_components]
    if len(scan_component_ids) != 3:
        raise ValueError(
            f"JPEG SOS component count is {len(scan_component_ids)}, expected 3"
        )
    if len(set(scan_component_ids)) != len(scan_component_ids):
        raise ValueError("JPEG SOS component IDs must be unique")
    if scan_component_ids != [1, 2, 3]:
        raise ValueError(
            f"JPEG SOS component IDs are {scan_component_ids}, expected [1, 2, 3]"
        )
    if set(scan_component_ids) != component_ids:
        raise ValueError("JPEG SOS components do not match SOF0 components")
    for component in components:
        if component.quantization_table not in quantization_tables:
            raise ValueError(
                f"JPEG SOF0 component {component.component_id} references missing DQT table "
                f"{component.quantization_table}"
            )
    component_quantization_tables = tuple(
        component.quantization_table for component in components
    )
    if component_quantization_tables != (0, 1, 1):
        raise ValueError(
            f"JPEG SOF0 quantization table selectors are {component_quantization_tables}, "
            "expected Y=0 and Cb/Cr=1"
        )
    for scan_component in scan_components:
        if scan_component.component_id not in component_ids:
            raise ValueError(
                f"JPEG SOS references unknown component {scan_component.component_id}"
            )
        if (0, scan_component.dc_table) not in huffman_tables:
            raise ValueError(
                f"JPEG SOS component {scan_component.component_id} references missing DC DHT table "
                f"{scan_component.dc_table}"
            )
        if (1, scan_component.ac_table) not in huffman_tables:
            raise ValueError(
                f"JPEG SOS component {scan_component.component_id} references missing AC DHT table "
                f"{scan_component.ac_table}"
            )
    scan_table_selectors = tuple(
        (scan_component.dc_table, scan_component.ac_table)
        for scan_component in scan_components
    )
    if scan_table_selectors != ((0, 0), (1, 1), (1, 1)):
        raise ValueError(
            f"JPEG SOS table selectors are {scan_table_selectors}, "
            "expected Y=0/0 and Cb/Cr=1/1"
        )
    return JpegInfo(
        width=dimensions[0],
        height=dimensions[1],
        mcu_count=jpeg_mcu_count_from_components(
            dimensions[0],
            dimensions[1],
            components,
        ),
        sample_precision=sample_precision,
        components=components,
        scan_components=scan_components,
        spectral_start=spectral_start,
        spectral_end=spectral_end,
        successive_approximation=successive_approximation,
        quantization_tables=tuple(sorted(quantization_tables)),
        quantization_table_order=tuple(quantization_table_order),
        quantization_table_details=tuple(
            JpegQuantizationTable(
                table_id=table_id,
                precision=precision,
                byte_length=byte_length,
                payload_sha256=payload_sha256,
            )
            for table_id, (precision, byte_length, payload_sha256) in sorted(
                quantization_table_details.items()
            )
        ),
        huffman_tables=tuple(
            JpegHuffmanTable(
                table_class=table_class,
                table_id=table_id,
                symbol_count=symbol_count,
                payload_sha256=payload_sha256,
            )
            for (table_class, table_id), (symbol_count, payload_sha256) in sorted(
                huffman_tables.items()
            )
        ),
        huffman_table_order=tuple(huffman_table_order),
        scan_data_bytes=scan_data_bytes,
        scan_data_sha256=hashlib.sha256(scan_data).hexdigest(),
        stuffed_ff_bytes=stuffed_ff_bytes,
        byte_length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        app0_segments=app0_segments,
        jfif_app0_segments=jfif_app0_segments,
        jfif_app0=jfif_app0,
        dqt_segments=dqt_segments,
        sof0_segments=sof0_segments,
        dht_segments=dht_segments,
        sos_segments=sos_segments,
        dri_segments=dri_segments,
        restart_interval=restart_interval,
        restart_markers=len(restart_marker_sequence),
        restart_marker_sequence=tuple(restart_marker_sequence),
        marker_sequence=tuple(marker_sequence),
    )


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    info = jpeg_info(data)
    return info.width, info.height


def jpeg_chroma_mode(info: JpegInfo) -> str:
    if len(info.components) != 3:
        return "unknown"

    y, cb, cr = info.components
    if (
        y.horizontal_sampling == 1
        and y.vertical_sampling == 1
        and cb.horizontal_sampling == 1
        and cb.vertical_sampling == 1
        and cr.horizontal_sampling == 1
        and cr.vertical_sampling == 1
    ):
        return "4:4:4"
    if (
        y.horizontal_sampling == 2
        and y.vertical_sampling == 2
        and cb.horizontal_sampling == 1
        and cb.vertical_sampling == 1
        and cr.horizontal_sampling == 1
        and cr.vertical_sampling == 1
    ):
        return "4:2:0"
    return "unknown"


def jpeg_mcu_count_from_components(
    width: int,
    height: int,
    components: tuple[JpegComponent, ...],
) -> int:
    max_horizontal_sampling = max(
        component.horizontal_sampling for component in components
    )
    max_vertical_sampling = max(
        component.vertical_sampling for component in components
    )
    mcu_width = max_horizontal_sampling * 8
    mcu_height = max_vertical_sampling * 8
    columns = (width + mcu_width - 1) // mcu_width
    rows = (height + mcu_height - 1) // mcu_height
    return columns * rows


def require_chroma_mode(info: JpegInfo, expected_chroma_subsample: bool) -> None:
    expected = "4:2:0" if expected_chroma_subsample else "4:4:4"
    actual = jpeg_chroma_mode(info)
    if actual != expected:
        raise ValueError(f"JPEG chroma mode is {actual}, expected {expected}")


def require_jfif(info: JpegInfo, expected_emit_jfif: bool) -> None:
    if expected_emit_jfif and info.jfif_app0_segments == 0:
        raise ValueError("JPEG does not contain APP0/JFIF, but JFIF emission was expected")
    if not expected_emit_jfif and info.jfif_app0_segments != 0:
        raise ValueError("JPEG contains APP0/JFIF, but JFIF emission was disabled")


def scaled_quantization_payload(base_table: tuple[int, ...], quality: int) -> bytes:
    if not 1 <= quality <= 100:
        raise ValueError("quality must be in 1..100")
    scale = 5000 // quality if quality < 50 else 200 - quality * 2
    values = []
    for index in ZIG_ZAG_ORDER:
        scaled = (base_table[index] * scale + 50) // 100
        values.append(max(1, min(255, scaled)))
    return bytes(values)


def expected_quantization_payload_hashes(quality: int) -> dict[int, str]:
    return {
        0: hashlib.sha256(scaled_quantization_payload(STANDARD_LUMINANCE_QUANT, quality)).hexdigest(),
        1: hashlib.sha256(scaled_quantization_payload(STANDARD_CHROMINANCE_QUANT, quality)).hexdigest(),
    }


def standard_huffman_payloads() -> dict[tuple[int, int], bytes]:
    return {
        (0, 0): bytes(STANDARD_DC_LUMINANCE_BITS + STANDARD_DC_SYMBOLS),
        (0, 1): bytes(STANDARD_DC_CHROMINANCE_BITS + STANDARD_DC_SYMBOLS),
        (1, 0): bytes(STANDARD_AC_LUMINANCE_BITS + STANDARD_AC_LUMINANCE_SYMBOLS),
        (1, 1): bytes(STANDARD_AC_CHROMINANCE_BITS + STANDARD_AC_CHROMINANCE_SYMBOLS),
    }


def expected_huffman_payload_hashes() -> dict[tuple[int, int], tuple[int, str]]:
    return {
        table: (len(payload) - 16, hashlib.sha256(payload).hexdigest())
        for table, payload in standard_huffman_payloads().items()
    }


def require_standard_table_payloads(
    info: JpegInfo,
    expected_quality: int | None = None,
    require_standard_huffman: bool = False,
) -> None:
    if expected_quality is not None:
        expected_dqt_hashes = expected_quantization_payload_hashes(expected_quality)
        actual_dqt_hashes = {
            table.table_id: table.payload_sha256 for table in info.quantization_table_details
        }
        for table_id, expected_hash in expected_dqt_hashes.items():
            actual_hash = actual_dqt_hashes.get(table_id)
            if actual_hash != expected_hash:
                raise ValueError(
                    f"JPEG DQT table {table_id} payload does not match "
                    f"standard quality {expected_quality}"
                )

    if require_standard_huffman:
        expected_dht_hashes = expected_huffman_payload_hashes()
        actual_dht = {
            (table.table_class, table.table_id): (table.symbol_count, table.payload_sha256)
            for table in info.huffman_tables
        }
        for table, expected in expected_dht_hashes.items():
            if actual_dht.get(table) != expected:
                table_class, table_id = table
                class_name = "DC" if table_class == 0 else "AC"
                raise ValueError(
                    f"JPEG {class_name} DHT table {table_id} payload does not match "
                    "the standard baseline table"
                )


def jpeg_mcu_count(info: JpegInfo) -> int:
    return info.mcu_count


def expected_restart_marker_count(
    info: JpegInfo,
    expected_restart_interval: int | None,
) -> int | None:
    if expected_restart_interval is None:
        return None
    if expected_restart_interval == 0:
        return 0
    return (jpeg_mcu_count(info) - 1) // expected_restart_interval


def expected_restart_marker_sequence(
    info: JpegInfo,
    expected_restart_interval: int | None,
) -> list[str] | None:
    marker_count = expected_restart_marker_count(info, expected_restart_interval)
    if marker_count is None:
        return None
    return [f"RST{index % 8}" for index in range(marker_count)]


def require_restart_interval(info: JpegInfo, expected_restart_interval: int) -> None:
    if expected_restart_interval < 0:
        raise ValueError("expected restart interval must be nonnegative")
    if expected_restart_interval == 0:
        if (
            info.dri_segments != 0
            or info.restart_interval is not None
            or info.restart_markers != 0
        ):
            raise ValueError(
                "JPEG contains restart markers or DRI, but restart interval 0 was expected"
            )
        return
    if info.dri_segments != 1:
        raise ValueError(
            f"JPEG DRI segment count is {info.dri_segments}, expected 1"
        )
    if info.restart_interval != expected_restart_interval:
        actual = "none" if info.restart_interval is None else str(info.restart_interval)
        raise ValueError(
            f"JPEG restart interval is {actual}, expected {expected_restart_interval}"
        )
    expected_restart_markers = expected_restart_marker_count(info, expected_restart_interval)
    if info.restart_markers != expected_restart_markers:
        raise ValueError(
            f"JPEG restart marker count is {info.restart_markers}, "
            f"expected {expected_restart_markers}"
        )


def validate_jpeg(
    path: Path,
    expected_width: int,
    expected_height: int,
    expected_restart_interval: int | None = None,
    expected_chroma_subsample: bool | None = None,
    expected_emit_jfif: bool | None = None,
    expected_quality: int | None = None,
    require_standard_huffman: bool = False,
) -> JpegInfo:
    if expected_width <= 0 or expected_height <= 0:
        raise ValueError("expected JPEG dimensions must be positive")
    if expected_restart_interval is not None and not 0 <= expected_restart_interval <= 0xFFFF:
        raise ValueError("expected restart interval must be in 0..65535")
    if expected_quality is not None and not 1 <= expected_quality <= 100:
        raise ValueError("expected quality must be in 1..100")
    data = path.read_bytes()
    if len(data) < 4:
        raise ValueError("JPEG output is too short")
    if data[:2] != b"\xff\xd8":
        raise ValueError("JPEG output does not start with SOI")
    if data[-2:] != b"\xff\xd9":
        raise ValueError("JPEG output does not end with EOI")

    info = jpeg_info(data)
    if info.width != expected_width or info.height != expected_height:
        raise ValueError(
            f"JPEG dimensions are {info.width}x{info.height}, expected {expected_width}x{expected_height}"
        )
    if expected_restart_interval is not None:
        require_restart_interval(info, expected_restart_interval)
    if expected_chroma_subsample is not None:
        require_chroma_mode(info, expected_chroma_subsample)
    if expected_emit_jfif is not None:
        require_jfif(info, expected_emit_jfif)
    require_standard_table_payloads(
        info,
        expected_quality=expected_quality,
        require_standard_huffman=require_standard_huffman,
    )
    return info


def validation_expectations_record(
    info: JpegInfo,
    width: int,
    height: int,
    restart_interval: int | None,
    check_chroma_mode: bool,
    chroma_subsample: bool | None,
    expect_jfif: str | None,
    quality: int | None,
    require_standard_huffman: bool,
) -> dict[str, object]:
    expected_app0_segments: int | None
    if expect_jfif == "present":
        expected_app0_segments = 1
        expected_jfif_app0: dict[str, int] | None = {
            "version_major": 1,
            "version_minor": 1,
            "density_units": 0,
            "x_density": 1,
            "y_density": 1,
            "thumbnail_width": 0,
            "thumbnail_height": 0,
        }
    elif expect_jfif == "absent":
        expected_app0_segments = 0
        expected_jfif_app0 = None
    else:
        expected_app0_segments = None
        expected_jfif_app0 = None
    expected_marker_counts: dict[str, int | None] = {
        "APP0": expected_app0_segments,
        "JFIF_APP0": expected_app0_segments,
        "DQT": 2,
        "SOF0": 1,
        "DHT": 4,
        "SOS": 1,
        "DRI": None if restart_interval is None else (0 if restart_interval == 0 else 1),
        "RST": expected_restart_marker_count(info, restart_interval),
    }
    expected_marker_sequence_through_sos = ["SOI"]
    if expect_jfif == "present":
        expected_marker_sequence_through_sos.append("APP0")
    expected_marker_sequence_through_sos.extend(
        ["DQT", "DQT", "SOF0", "DHT", "DHT", "DHT", "DHT"]
    )
    if restart_interval is not None and restart_interval != 0:
        expected_marker_sequence_through_sos.append("DRI")
    expected_marker_sequence_through_sos.append("SOS")
    expected_sof0_components = [
        {"component_id": 1, "quantization_table": 0},
        {"component_id": 2, "quantization_table": 1},
        {"component_id": 3, "quantization_table": 1},
    ]
    if check_chroma_mode and chroma_subsample is not None:
        expected_y_sampling = 2 if chroma_subsample else 1
        expected_sof0_components[0].update(
            {
                "horizontal_sampling": expected_y_sampling,
                "vertical_sampling": expected_y_sampling,
            }
        )
        for expected_component in expected_sof0_components[1:]:
            expected_component.update(
                {
                    "horizontal_sampling": 1,
                    "vertical_sampling": 1,
                }
            )
    expected_sos_components = [
        {"component_id": 1, "dc_table": 0, "ac_table": 0},
        {"component_id": 2, "dc_table": 1, "ac_table": 1},
        {"component_id": 3, "dc_table": 1, "ac_table": 1},
    ]
    record: dict[str, object] = {
        "width": width,
        "height": height,
        "expected_sample_precision": 8,
        "expected_component_count": 3,
        "restart_interval": restart_interval,
        "expected_restart_markers": expected_restart_marker_count(info, restart_interval),
        "expected_restart_marker_sequence": expected_restart_marker_sequence(
            info,
            restart_interval,
        ),
        "expected_scan_data_min_bytes": 1,
        "expected_marker_counts": expected_marker_counts,
        "expected_marker_order": {
            "through_sos": expected_marker_sequence_through_sos,
            "app0_policy": expect_jfif if expect_jfif is not None else "optional",
            "dri_policy": (
                "optional"
                if restart_interval is None
                else ("absent" if restart_interval == 0 else "present")
            ),
            "terminal_marker": "EOI",
        },
        "expected_quantization_tables": [0, 1],
        "expected_quantization_table_order": [0, 1],
        "expected_huffman_table_order": [
            {"table_class": 0, "table_id": 0},
            {"table_class": 0, "table_id": 1},
            {"table_class": 1, "table_id": 0},
            {"table_class": 1, "table_id": 1},
        ],
        "expected_sof0_components": expected_sof0_components,
        "expected_sos_components": expected_sos_components,
        "expected_sos_spectral": {
            "spectral_start": 0,
            "spectral_end": 63,
            "successive_approximation": 0,
        },
        "check_chroma_mode": check_chroma_mode,
        "chroma_subsample": chroma_subsample,
        "expected_chroma_mode": (
            ("4:2:0" if chroma_subsample else "4:4:4")
            if check_chroma_mode and chroma_subsample is not None
            else None
        ),
        "expect_jfif": expect_jfif,
        "expected_jfif_app0": expected_jfif_app0,
        "quality": quality,
        "require_standard_huffman": require_standard_huffman,
    }
    if quality is not None:
        record["expected_quantization_payload_sha256"] = {
            str(table_id): payload_sha256
            for table_id, payload_sha256 in expected_quantization_payload_hashes(quality).items()
        }
    if require_standard_huffman:
        record["expected_huffman_tables"] = [
            {
                "table_class": table_class,
                "table_id": table_id,
                "symbol_count": symbol_count,
                "payload_sha256": payload_sha256,
            }
            for (table_class, table_id), (symbol_count, payload_sha256) in sorted(
                expected_huffman_payload_hashes().items()
            )
        ]
    return record


def decoder_command_argv(jpeg: Path, command: str) -> list[str]:
    if not command:
        raise ValueError("decoder command must be non-empty")

    argv = shlex.split(command, posix=(os.name != "nt"))
    if os.name == "nt":
        argv = [
            arg[1:-1] if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in ("'", '"') else arg
            for arg in argv
        ]
    if not argv:
        raise ValueError("decoder command must be non-empty")
    jpeg_arg = str(jpeg)
    if any("{jpeg}" in arg for arg in argv):
        argv = [arg.replace("{jpeg}", jpeg_arg) for arg in argv]
    else:
        argv.append(jpeg_arg)
    return argv


def capture_decoder_text(text: str) -> tuple[str, bool]:
    if len(text) <= DECODER_OUTPUT_CAPTURE_CHARS:
        return text, False
    return text[:DECODER_OUTPUT_CAPTURE_CHARS], True


def run_decoder_command(
    jpeg: Path,
    command: str,
    timeout_seconds: float = 30.0,
) -> DecoderCommandResult:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("decoder timeout must be finite and positive")

    argv = decoder_command_argv(jpeg, command)

    try:
        start = time.perf_counter()
        completed = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
        elapsed_seconds = time.perf_counter() - start
    except FileNotFoundError as exc:
        raise RuntimeError(f"decoder command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"decoder command timed out after {timeout_seconds:g} seconds"
        ) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"decoder command failed with exit code {completed.returncode}{suffix}")
    stdout, stdout_truncated = capture_decoder_text(completed.stdout)
    stderr, stderr_truncated = capture_decoder_text(completed.stderr)
    return DecoderCommandResult(
        argv=tuple(argv),
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        elapsed_seconds=elapsed_seconds,
        stdout_chars=len(stdout),
        stderr_chars=len(stderr),
        output_capture_chars=DECODER_OUTPUT_CAPTURE_CHARS,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def jpeg_info_record(
    jpeg: Path,
    info: JpegInfo,
    decoder_passed: bool | None = None,
    decoder_command: str | None = None,
    decoder_timeout_seconds: float | None = None,
    decoder_result: DecoderCommandResult | None = None,
    validation_expectations: dict[str, object] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "jpeg": str(jpeg),
        "jpeg_resolved": str(jpeg.resolve(strict=False)),
        "jpeg_validation_passed": True,
        "width": info.width,
        "height": info.height,
        "mcu_count": info.mcu_count,
        "sample_precision": info.sample_precision,
        "component_count": len(info.components),
        "components": [
            {
                "component_id": component.component_id,
                "horizontal_sampling": component.horizontal_sampling,
                "vertical_sampling": component.vertical_sampling,
                "quantization_table": component.quantization_table,
            }
            for component in info.components
        ],
        "scan_components": [
            {
                "component_id": component.component_id,
                "dc_table": component.dc_table,
                "ac_table": component.ac_table,
            }
            for component in info.scan_components
        ],
        "spectral_start": info.spectral_start,
        "spectral_end": info.spectral_end,
        "successive_approximation": info.successive_approximation,
        "quantization_tables": list(info.quantization_tables),
        "quantization_table_order": list(info.quantization_table_order),
        "quantization_table_details": [
            {
                "table_id": table.table_id,
                "precision": table.precision,
                "byte_length": table.byte_length,
                "payload_sha256": table.payload_sha256,
            }
            for table in info.quantization_table_details
        ],
        "huffman_tables": [
            {
                "table_class": table.table_class,
                "table_id": table.table_id,
                "symbol_count": table.symbol_count,
                "payload_sha256": table.payload_sha256,
            }
            for table in info.huffman_tables
        ],
        "huffman_table_order": [
            {"table_class": table_class, "table_id": table_id}
            for table_class, table_id in info.huffman_table_order
        ],
        "chroma_mode": jpeg_chroma_mode(info),
        "scan_data_bytes": info.scan_data_bytes,
        "scan_data_sha256": info.scan_data_sha256,
        "stuffed_ff_bytes": info.stuffed_ff_bytes,
        "app0_segments": info.app0_segments,
        "jfif_app0_segments": info.jfif_app0_segments,
        "jfif_app0": None
        if info.jfif_app0 is None
        else {
            "version_major": info.jfif_app0.version_major,
            "version_minor": info.jfif_app0.version_minor,
            "density_units": info.jfif_app0.density_units,
            "x_density": info.jfif_app0.x_density,
            "y_density": info.jfif_app0.y_density,
            "thumbnail_width": info.jfif_app0.thumbnail_width,
            "thumbnail_height": info.jfif_app0.thumbnail_height,
        },
        "dqt_segments": info.dqt_segments,
        "sof0_segments": info.sof0_segments,
        "dht_segments": info.dht_segments,
        "sos_segments": info.sos_segments,
        "dri_segments": info.dri_segments,
        "restart_interval": info.restart_interval,
        "restart_markers": info.restart_markers,
        "marker_counts": {
            "APP0": info.app0_segments,
            "JFIF_APP0": info.jfif_app0_segments,
            "DQT": info.dqt_segments,
            "SOF0": info.sof0_segments,
            "DHT": info.dht_segments,
            "SOS": info.sos_segments,
            "DRI": info.dri_segments,
            "RST": info.restart_markers,
        },
        "restart_marker_sequence": list(info.restart_marker_sequence),
        "marker_sequence": list(info.marker_sequence),
        "byte_length": info.byte_length,
        "sha256": info.sha256,
    }
    if validation_expectations is not None:
        record["validation_expectations"] = validation_expectations
    if decoder_passed is not None:
        record["decoder_passed"] = decoder_passed
    if decoder_command is not None:
        record["decoder_command"] = decoder_command
    if decoder_timeout_seconds is not None:
        record["decoder_timeout_seconds"] = decoder_timeout_seconds
    if decoder_result is not None:
        record["decoder_argv"] = list(decoder_result.argv)
        record["decoder_returncode"] = decoder_result.returncode
        record["decoder_stdout"] = decoder_result.stdout
        record["decoder_stderr"] = decoder_result.stderr
        record["decoder_elapsed_seconds"] = decoder_result.elapsed_seconds
        record["decoder_stdout_chars"] = decoder_result.stdout_chars
        record["decoder_stderr_chars"] = decoder_result.stderr_chars
        record["decoder_output_capture_chars"] = decoder_result.output_capture_chars
        record["decoder_stdout_truncated"] = decoder_result.stdout_truncated
        record["decoder_stderr_truncated"] = decoder_result.stderr_truncated
    return record


def file_info(path: Path, data: bytes) -> FileInfo:
    return FileInfo(
        path=str(path),
        path_resolved=str(path.resolve(strict=False)),
        byte_length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def is_sha256_hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdefABCDEF" for char in value
    )


def is_strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_strict_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_finite_strict_number(value: object) -> bool:
    return is_strict_number(value) and math.isfinite(value)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def strict_json_loads(text: str) -> object:
    return json.loads(text, parse_constant=_reject_json_constant)


def strict_json_dumps(value: object, **kwargs: object) -> str:
    return json.dumps(value, allow_nan=False, **kwargs)


def file_info_record(info: FileInfo) -> dict[str, object]:
    return {
        "path": info.path,
        "path_resolved": info.path_resolved,
        "byte_length": info.byte_length,
        "sha256": info.sha256,
    }


def ppm_image_stats(image: PpmImage) -> dict[str, object]:
    if len(image.rgb) != image.width * image.height * 3:
        raise ValueError("PPM RGB payload length does not match dimensions")
    pixels = zip(image.rgb[0::3], image.rgb[1::3], image.rgb[2::3])
    first_pixel: tuple[int, int, int] | None = None
    min_r = min_g = min_b = 255
    max_r = max_g = max_b = 0
    non_flat = False
    has_color_pixels = False
    for pixel in pixels:
        r, g, b = pixel
        if first_pixel is None:
            first_pixel = pixel
        elif pixel != first_pixel:
            non_flat = True
        has_color_pixels = has_color_pixels or r != g or r != b
        min_r = min(min_r, r)
        min_g = min(min_g, g)
        min_b = min(min_b, b)
        max_r = max(max_r, r)
        max_g = max(max_g, g)
        max_b = max(max_b, b)

    return {
        "channel_min": {"r": min_r, "g": min_g, "b": min_b},
        "channel_max": {"r": max_r, "g": max_g, "b": max_b},
        "non_flat": non_flat,
        "has_color_pixels": has_color_pixels,
    }


def ppm_evidence_record(path: Path, image: PpmImage) -> dict[str, object]:
    record = file_info_record(file_info(path, path.read_bytes()))
    record.update(
        {
            "width": image.width,
            "height": image.height,
            "rgb_bytes": len(image.rgb),
            "image_stats": ppm_image_stats(image),
        }
    )
    return record


def run_input_ppm_record(
    path: Path,
    expected_width: int,
    expected_height: int,
    input_rgb: bytes,
    max_width: int,
    max_height: int,
) -> dict[str, object]:
    image = read_ppm(path, max_width, max_height)
    if image.width != expected_width or image.height != expected_height:
        raise ValueError(
            f"{path}: PPM dimensions are {image.width}x{image.height}, "
            f"expected {expected_width}x{expected_height}"
        )
    expected_rgb = rgb_stream_bytes(image)
    if expected_rgb != input_rgb:
        raise ValueError(f"{path}: packed PPM bytes do not match input RGB stream")

    record = ppm_evidence_record(path, image)
    record["packed_rgb_byte_length"] = len(expected_rgb)
    record["packed_rgb_sha256"] = hashlib.sha256(expected_rgb).hexdigest()
    record["packed_rgb_matches_input"] = True
    return record


def expected_component_records_match(
    actual_components: object,
    expected_components: object,
    required_keys: tuple[str, ...],
) -> bool:
    if not isinstance(actual_components, list) or not isinstance(
        expected_components, list
    ):
        return False
    if len(actual_components) != len(expected_components):
        return False
    for actual, expected in zip(actual_components, expected_components):
        if not isinstance(actual, dict) or not isinstance(expected, dict):
            return False
        for key in required_keys:
            if not is_strict_int(expected.get(key)):
                return False
        for key, expected_value in expected.items():
            if actual.get(key) != expected_value:
                return False
    return True


def expected_record_fields_match(
    actual_record: object,
    expected_record: object,
    required_keys: tuple[str, ...],
) -> bool:
    if not isinstance(actual_record, dict) or not isinstance(expected_record, dict):
        return False
    for key in required_keys:
        if not is_strict_int(expected_record.get(key)):
            return False
    return all(actual_record.get(key) == value for key, value in expected_record.items())


def hardware_run_summary_record(record: dict[str, object]) -> dict[str, object]:
    evidence_present = {
        "jpeg_output": False,
        "input_rgb": False,
        "stream_devices": False,
        "axi_lite": False,
        "encoder_config": False,
        "capture_config": False,
        "status_checks": False,
        "validation_expectations": False,
        "input_ppm": False,
        "transfer_timing": False,
        "decoder": False,
    }
    checks = {"jpeg_validation_passed": record.get("jpeg_validation_passed") is True}

    jpeg_byte_length = record.get("byte_length")
    scan_data_bytes = record.get("scan_data_bytes")
    jpeg_sha256 = record.get("sha256")
    scan_data_sha256 = record.get("scan_data_sha256")
    jpeg_path = record.get("jpeg")
    jpeg_path_resolved = record.get("jpeg_resolved")
    width = record.get("width")
    height = record.get("height")
    marker_sequence = record.get("marker_sequence")
    marker_counts = record.get("marker_counts")
    expected_marker_count_fields = {
        "APP0": "app0_segments",
        "JFIF_APP0": "jfif_app0_segments",
        "DQT": "dqt_segments",
        "SOF0": "sof0_segments",
        "DHT": "dht_segments",
        "SOS": "sos_segments",
        "DRI": "dri_segments",
        "RST": "restart_markers",
    }
    restart_markers = record.get("restart_markers")
    restart_marker_sequence = record.get("restart_marker_sequence")
    jpeg_byte_length_positive = (
        is_strict_int(jpeg_byte_length) and jpeg_byte_length > 0
    )
    scan_data_bytes_positive = (
        is_strict_int(scan_data_bytes) and scan_data_bytes > 0
    )
    jpeg_sha256_present = is_sha256_hex(jpeg_sha256)
    scan_data_sha256_present = is_sha256_hex(scan_data_sha256)
    jpeg_path_present = isinstance(jpeg_path, str) and len(jpeg_path) > 0
    jpeg_path_resolved_present = (
        isinstance(jpeg_path_resolved, str) and len(jpeg_path_resolved) > 0
    )
    jpeg_path_resolved_matches = (
        jpeg_path_present
        and jpeg_path_resolved_present
        and jpeg_path_resolved == str(Path(jpeg_path).resolve(strict=False))
    )
    jpeg_dimensions_positive = (
        is_strict_int(width) and is_strict_int(height) and width > 0 and height > 0
    )
    marker_sequence_values = (
        marker_sequence if isinstance(marker_sequence, (list, tuple)) else ()
    )
    jpeg_marker_sequence_starts_with_soi = (
        len(marker_sequence_values) > 0 and marker_sequence_values[0] == "SOI"
    )
    jpeg_marker_sequence_ends_with_eoi = (
        len(marker_sequence_values) > 0 and marker_sequence_values[-1] == "EOI"
    )
    restart_marker_sequence_values = (
        restart_marker_sequence
        if isinstance(restart_marker_sequence, (list, tuple))
        and all(is_strict_int(marker) for marker in restart_marker_sequence)
        else ()
    )
    restart_marker_sequence_length_matches_count = (
        is_strict_int(restart_markers)
        and len(restart_marker_sequence_values) == restart_markers
    )
    restart_marker_count_matches_marker_counts = (
        isinstance(marker_counts, dict)
        and is_strict_int(restart_markers)
        and marker_counts.get("RST") == restart_markers
    )
    marker_counts_match_segment_counts = (
        isinstance(marker_counts, dict)
        and all(
            is_strict_int(record.get(field_name))
            and marker_counts.get(marker_name) == record.get(field_name)
            for marker_name, field_name in expected_marker_count_fields.items()
        )
    )
    evidence_present["jpeg_output"] = (
        jpeg_path_present
        and jpeg_path_resolved_present
        and jpeg_path_resolved_matches
        and jpeg_byte_length_positive
        and scan_data_bytes_positive
        and jpeg_sha256_present
        and scan_data_sha256_present
        and jpeg_dimensions_positive
        and jpeg_marker_sequence_starts_with_soi
        and jpeg_marker_sequence_ends_with_eoi
        and restart_marker_sequence_length_matches_count
        and restart_marker_count_matches_marker_counts
        and marker_counts_match_segment_counts
    )
    checks["jpeg_path_present"] = jpeg_path_present
    checks["jpeg_path_resolved_present"] = jpeg_path_resolved_present
    checks["jpeg_path_resolved_matches"] = jpeg_path_resolved_matches
    checks["jpeg_byte_length_positive"] = jpeg_byte_length_positive
    checks["jpeg_scan_data_bytes_positive"] = scan_data_bytes_positive
    checks["jpeg_sha256_present"] = jpeg_sha256_present
    checks["jpeg_scan_data_sha256_present"] = scan_data_sha256_present
    checks["jpeg_dimensions_positive"] = jpeg_dimensions_positive
    checks["jpeg_marker_sequence_starts_with_soi"] = (
        jpeg_marker_sequence_starts_with_soi
    )
    checks["jpeg_marker_sequence_ends_with_eoi"] = jpeg_marker_sequence_ends_with_eoi
    checks["restart_marker_sequence_length_matches_count"] = (
        restart_marker_sequence_length_matches_count
    )
    checks["restart_marker_count_matches_marker_counts"] = (
        restart_marker_count_matches_marker_counts
    )
    checks["marker_counts_match_segment_counts"] = marker_counts_match_segment_counts

    encoder_config = record.get("encoder_config")
    validation_expectations = record.get("validation_expectations")
    input_rgb = record.get("input_rgb")
    input_ppm = record.get("input_ppm")
    if is_strict_int(width) and is_strict_int(height):
        if isinstance(encoder_config, dict):
            encoder_width = encoder_config.get("width")
            encoder_height = encoder_config.get("height")
            checks["encoder_config_matches_jpeg_dimensions"] = (
                is_strict_int(encoder_width)
                and is_strict_int(encoder_height)
                and encoder_width == width
                and encoder_height == height
            )
        if isinstance(validation_expectations, dict):
            validation_width = validation_expectations.get("width")
            validation_height = validation_expectations.get("height")
            checks["validation_expectations_match_jpeg_dimensions"] = (
                is_strict_int(validation_width)
                and is_strict_int(validation_height)
                and validation_width == width
                and validation_height == height
            )
        if isinstance(input_ppm, dict):
            input_ppm_width = input_ppm.get("width")
            input_ppm_height = input_ppm.get("height")
            checks["input_ppm_dimensions_match_jpeg"] = (
                is_strict_int(input_ppm_width)
                and is_strict_int(input_ppm_height)
                and input_ppm_width == width
                and input_ppm_height == height
            )
        if isinstance(input_rgb, dict):
            expected_byte_length = input_rgb.get("expected_byte_length")
            checks["input_rgb_expected_length_matches_dimensions"] = (
                expected_byte_length == width * height * 4
            )

    if isinstance(encoder_config, dict):
        encoder_width = encoder_config.get("width")
        encoder_height = encoder_config.get("height")
        encoder_max_width = encoder_config.get("max_width")
        encoder_max_height = encoder_config.get("max_height")
        encoder_quality = encoder_config.get("quality")
        encoder_restart_interval = encoder_config.get("restart_interval")
        encoder_chroma_subsample = encoder_config.get("chroma_subsample")
        encoder_emit_jfif = encoder_config.get("emit_jfif")
        encoder_clear_error = encoder_config.get("clear_error")
        encoder_control = encoder_config.get("control")
        encoder_control_hex = encoder_config.get("control_hex")
        encoder_dimensions_supported = (
            is_strict_int(encoder_width)
            and is_strict_int(encoder_height)
            and is_strict_int(encoder_max_width)
            and is_strict_int(encoder_max_height)
            and 0 < encoder_width <= encoder_max_width
            and 0 < encoder_height <= encoder_max_height
        )
        encoder_quality_valid = (
            is_strict_int(encoder_quality) and 1 <= encoder_quality <= 100
        )
        encoder_restart_interval_valid = (
            is_strict_int(encoder_restart_interval)
            and 0 <= encoder_restart_interval <= 0xFFFF
        )
        encoder_flags_valid = all(
            isinstance(flag, bool)
            for flag in (
                encoder_chroma_subsample,
                encoder_emit_jfif,
                encoder_clear_error,
            )
        )
        expected_control = control_value(
            bool(encoder_chroma_subsample),
            bool(encoder_emit_jfif),
            bool(encoder_clear_error),
        )
        encoder_control_matches_flags = (
            encoder_flags_valid
            and encoder_control == expected_control
            and encoder_control_hex == f"0x{expected_control:08x}"
        )
        evidence_present["encoder_config"] = (
            encoder_dimensions_supported
            and encoder_quality_valid
            and encoder_restart_interval_valid
            and encoder_flags_valid
            and encoder_control_matches_flags
        )
        checks["encoder_dimensions_supported"] = encoder_dimensions_supported
        checks["encoder_quality_valid"] = encoder_quality_valid
        checks["encoder_restart_interval_valid"] = encoder_restart_interval_valid
        checks["encoder_flags_valid"] = encoder_flags_valid
        checks["encoder_control_matches_flags"] = encoder_control_matches_flags

    if isinstance(validation_expectations, dict):
        expected_marker_order = validation_expectations.get("expected_marker_order")
        expected_sos_spectral = validation_expectations.get("expected_sos_spectral")
        expected_through_sos = (
            expected_marker_order.get("through_sos")
            if isinstance(expected_marker_order, dict)
            else None
        )
        validation_baseline_shape = (
            validation_expectations.get("expected_sample_precision") == 8
            and validation_expectations.get("expected_component_count") == 3
            and is_strict_int(
                validation_expectations.get("expected_scan_data_min_bytes")
            )
            and validation_expectations.get("expected_scan_data_min_bytes") == 1
        )
        validation_scan_data_length_matches = (
            is_strict_int(scan_data_bytes)
            and is_strict_int(
                validation_expectations.get("expected_scan_data_min_bytes")
            )
            and scan_data_bytes
            >= validation_expectations.get("expected_scan_data_min_bytes")
        )
        validation_marker_order_present = (
            isinstance(expected_marker_order, dict)
            and isinstance(expected_through_sos, list)
            and len(expected_through_sos) > 0
            and expected_through_sos[0] == "SOI"
            and expected_through_sos[-1] == "SOS"
            and expected_marker_order.get("terminal_marker") == "EOI"
        )
        actual_through_sos = None
        if "SOS" in marker_sequence_values:
            sos_index = marker_sequence_values.index("SOS")
            actual_through_sos = list(marker_sequence_values[: sos_index + 1])
        validation_marker_order_matches = (
            validation_marker_order_present
            and actual_through_sos == expected_through_sos
            and jpeg_marker_sequence_ends_with_eoi
        )
        validation_table_order_present = (
            validation_expectations.get("expected_quantization_tables") == [0, 1]
            and validation_expectations.get("expected_quantization_table_order")
            == [0, 1]
            and validation_expectations.get("expected_huffman_table_order")
            == [
                {"table_class": 0, "table_id": 0},
                {"table_class": 0, "table_id": 1},
                {"table_class": 1, "table_id": 0},
                {"table_class": 1, "table_id": 1},
            ]
        )
        validation_table_order_matches = (
            validation_table_order_present
            and record.get("quantization_tables")
            == validation_expectations.get("expected_quantization_tables")
            and record.get("quantization_table_order")
            == validation_expectations.get("expected_quantization_table_order")
            and record.get("huffman_table_order")
            == validation_expectations.get("expected_huffman_table_order")
        )
        validation_sos_spectral_baseline = (
            isinstance(expected_sos_spectral, dict)
            and expected_sos_spectral.get("spectral_start") == 0
            and expected_sos_spectral.get("spectral_end") == 63
            and expected_sos_spectral.get("successive_approximation") == 0
        )
        validation_sos_spectral_matches = (
            validation_sos_spectral_baseline
            and record.get("spectral_start")
            == expected_sos_spectral.get("spectral_start")
            and record.get("spectral_end")
            == expected_sos_spectral.get("spectral_end")
            and record.get("successive_approximation")
            == expected_sos_spectral.get("successive_approximation")
        )
        validation_requires_standard_huffman = (
            validation_expectations.get("require_standard_huffman") is True
        )
        expected_quality = validation_expectations.get("quality")
        validation_quality_valid = expected_quality is None or (
            is_strict_int(expected_quality) and 1 <= expected_quality <= 100
        )
        expected_restart_interval = validation_expectations.get("restart_interval")
        validation_restart_interval_valid = expected_restart_interval is None or (
            is_strict_int(expected_restart_interval)
            and 0 <= expected_restart_interval <= 0xFFFF
        )
        expected_restart_markers = validation_expectations.get(
            "expected_restart_markers"
        )
        expected_restart_marker_sequence = validation_expectations.get(
            "expected_restart_marker_sequence"
        )
        expected_marker_counts = validation_expectations.get("expected_marker_counts")
        expected_sof0_components = validation_expectations.get(
            "expected_sof0_components"
        )
        expected_sos_components = validation_expectations.get(
            "expected_sos_components"
        )
        expected_jfif = validation_expectations.get("expect_jfif")
        expected_jfif_app0 = validation_expectations.get("expected_jfif_app0")
        expected_chroma_mode = validation_expectations.get("expected_chroma_mode")
        expected_dqt_payload_hashes = validation_expectations.get(
            "expected_quantization_payload_sha256"
        )
        expected_huffman_tables = validation_expectations.get(
            "expected_huffman_tables"
        )
        validation_restart_marker_count_matches = (
            expected_restart_markers is None
            or "restart_markers" not in record
            or (
                is_strict_int(restart_markers)
                and expected_restart_markers == restart_markers
            )
        )
        validation_restart_marker_sequence_matches = (
            expected_restart_marker_sequence is None
            or "restart_marker_sequence" not in record
            or (
                isinstance(expected_restart_marker_sequence, list)
                and expected_restart_marker_sequence
                == [f"RST{marker}" for marker in restart_marker_sequence_values]
            )
        )
        validation_marker_counts_match = (
            expected_marker_counts is None
            or "marker_counts" not in record
            or (
                isinstance(expected_marker_counts, dict)
                and isinstance(marker_counts, dict)
                and all(
                    expected_count is None
                    or (
                        is_strict_int(expected_count)
                        and marker_counts.get(marker_name) == expected_count
                    )
                    for marker_name, expected_count in expected_marker_counts.items()
                )
            )
        )
        validation_sof0_components_match = expected_component_records_match(
            record.get("components"),
            expected_sof0_components,
            ("component_id", "quantization_table"),
        )
        validation_sos_components_match = expected_component_records_match(
            record.get("scan_components"),
            expected_sos_components,
            ("component_id", "dc_table", "ac_table"),
        )
        validation_jfif_policy_matches = (
            expected_jfif is None
            or (
                expected_jfif == "present"
                and record.get("jfif_app0_segments") == 1
                and isinstance(record.get("jfif_app0"), dict)
            )
            or (
                expected_jfif == "absent"
                and record.get("jfif_app0_segments") == 0
                and record.get("jfif_app0") is None
            )
        )
        validation_jfif_app0_fields_match = (
            expected_jfif_app0 is None
            or expected_record_fields_match(
                record.get("jfif_app0"),
                expected_jfif_app0,
                (
                    "version_major",
                    "version_minor",
                    "density_units",
                    "x_density",
                    "y_density",
                    "thumbnail_width",
                    "thumbnail_height",
                ),
            )
        )
        validation_chroma_mode_matches = (
            expected_chroma_mode is None
            or "chroma_mode" not in record
            or (
                isinstance(expected_chroma_mode, str)
                and record.get("chroma_mode") == expected_chroma_mode
            )
        )
        quantization_table_details = record.get("quantization_table_details")
        actual_dqt_payload_hashes = {}
        if isinstance(quantization_table_details, list):
            for table in quantization_table_details:
                if not isinstance(table, dict):
                    continue
                table_id = table.get("table_id")
                payload_sha256 = table.get("payload_sha256")
                if is_strict_int(table_id) and is_sha256_hex(payload_sha256):
                    actual_dqt_payload_hashes[str(table_id)] = payload_sha256
        dqt_hashes_required = validation_quality_valid and is_strict_int(expected_quality)
        expected_dqt_payload_hashes_valid = (
            isinstance(expected_dqt_payload_hashes, dict)
            and set(expected_dqt_payload_hashes.keys()) == {"0", "1"}
            and all(is_sha256_hex(payload) for payload in expected_dqt_payload_hashes.values())
        )
        validation_dqt_payload_hashes_match = (
            (expected_dqt_payload_hashes is None and not dqt_hashes_required)
            or "quantization_table_details" not in record
            or (
                expected_dqt_payload_hashes_valid
                and all(
                    actual_dqt_payload_hashes.get(table_id) == expected_hash
                    for table_id, expected_hash in expected_dqt_payload_hashes.items()
                )
            )
        )
        huffman_tables = record.get("huffman_tables")
        actual_huffman_tables = {}
        if isinstance(huffman_tables, list):
            for table in huffman_tables:
                if not isinstance(table, dict):
                    continue
                table_class = table.get("table_class")
                table_id = table.get("table_id")
                symbol_count = table.get("symbol_count")
                payload_sha256 = table.get("payload_sha256")
                if (
                    is_strict_int(table_class)
                    and is_strict_int(table_id)
                    and is_strict_int(symbol_count)
                    and is_sha256_hex(payload_sha256)
                ):
                    actual_huffman_tables[(table_class, table_id)] = (
                        symbol_count,
                        payload_sha256,
                    )
        expected_huffman_table_keys = {(0, 0), (0, 1), (1, 0), (1, 1)}
        expected_huffman_table_hashes = {}
        expected_huffman_tables_valid = isinstance(expected_huffman_tables, list)
        if expected_huffman_tables_valid:
            for expected_table in expected_huffman_tables:
                if not isinstance(expected_table, dict):
                    expected_huffman_tables_valid = False
                    break
                table_class = expected_table.get("table_class")
                table_id = expected_table.get("table_id")
                symbol_count = expected_table.get("symbol_count")
                payload_sha256 = expected_table.get("payload_sha256")
                if (
                    not is_strict_int(table_class)
                    or not is_strict_int(table_id)
                    or not is_strict_int(symbol_count)
                    or not is_sha256_hex(payload_sha256)
                ):
                    expected_huffman_tables_valid = False
                    break
                expected_huffman_table_hashes[(table_class, table_id)] = (
                    symbol_count,
                    payload_sha256,
                )
        if (
            expected_huffman_tables_valid
            and validation_requires_standard_huffman
            and set(expected_huffman_table_hashes.keys()) != expected_huffman_table_keys
        ):
            expected_huffman_tables_valid = False
        validation_huffman_tables_match = (
            (expected_huffman_tables is None and not validation_requires_standard_huffman)
            or "huffman_tables" not in record
            or (
                expected_huffman_tables_valid
                and all(
                    actual_huffman_tables.get(table) == expected
                    for table, expected in expected_huffman_table_hashes.items()
                )
            )
        )
        evidence_present["validation_expectations"] = (
            validation_baseline_shape
            and validation_scan_data_length_matches
            and validation_marker_order_present
            and validation_marker_order_matches
            and validation_table_order_present
            and validation_table_order_matches
            and validation_sos_spectral_baseline
            and validation_sos_spectral_matches
            and validation_requires_standard_huffman
            and validation_quality_valid
            and validation_restart_interval_valid
            and validation_restart_marker_count_matches
            and validation_restart_marker_sequence_matches
            and validation_marker_counts_match
            and validation_sof0_components_match
            and validation_sos_components_match
            and validation_jfif_policy_matches
            and validation_jfif_app0_fields_match
            and validation_chroma_mode_matches
            and validation_dqt_payload_hashes_match
            and validation_huffman_tables_match
        )
        checks["validation_baseline_shape"] = validation_baseline_shape
        checks["validation_scan_data_length_matches"] = (
            validation_scan_data_length_matches
        )
        checks["validation_marker_order_present"] = validation_marker_order_present
        checks["validation_marker_order_matches"] = validation_marker_order_matches
        checks["validation_table_order_present"] = validation_table_order_present
        checks["validation_table_order_matches"] = validation_table_order_matches
        checks["validation_sos_spectral_baseline"] = validation_sos_spectral_baseline
        checks["validation_sos_spectral_matches"] = validation_sos_spectral_matches
        checks["validation_requires_standard_huffman"] = (
            validation_requires_standard_huffman
        )
        checks["validation_quality_valid"] = validation_quality_valid
        checks["validation_restart_interval_valid"] = (
            validation_restart_interval_valid
        )
        checks["validation_restart_marker_count_matches"] = (
            validation_restart_marker_count_matches
        )
        checks["validation_restart_marker_sequence_matches"] = (
            validation_restart_marker_sequence_matches
        )
        checks["validation_marker_counts_match"] = validation_marker_counts_match
        checks["validation_sof0_components_match"] = (
            validation_sof0_components_match
        )
        checks["validation_sos_components_match"] = validation_sos_components_match
        checks["validation_jfif_policy_matches"] = validation_jfif_policy_matches
        checks["validation_jfif_app0_fields_match"] = (
            validation_jfif_app0_fields_match
        )
        checks["validation_chroma_mode_matches"] = validation_chroma_mode_matches
        checks["validation_dqt_payload_hashes_match"] = (
            validation_dqt_payload_hashes_match
        )
        checks["validation_huffman_tables_match"] = validation_huffman_tables_match

    input_rgb = record.get("input_rgb")
    if isinstance(input_rgb, dict):
        input_rgb_path = input_rgb.get("path")
        input_rgb_path_resolved = input_rgb.get("path_resolved")
        input_rgb_byte_length = input_rgb.get("byte_length")
        input_rgb_sha256 = input_rgb.get("sha256")
        input_rgb_expected_byte_length = input_rgb.get("expected_byte_length")
        input_rgb_path_present = (
            isinstance(input_rgb_path, str) and len(input_rgb_path) > 0
        )
        input_rgb_path_resolved_present = (
            isinstance(input_rgb_path_resolved, str)
            and len(input_rgb_path_resolved) > 0
        )
        input_rgb_path_resolved_matches = (
            input_rgb_path_present
            and input_rgb_path_resolved_present
            and input_rgb_path_resolved
            == str(Path(input_rgb_path).resolve(strict=False))
        )
        input_rgb_byte_length_positive = (
            is_strict_int(input_rgb_byte_length) and input_rgb_byte_length > 0
        )
        input_rgb_sha256_present = is_sha256_hex(input_rgb_sha256)
        input_rgb_expected_byte_length_positive = (
            is_strict_int(input_rgb_expected_byte_length)
            and input_rgb_expected_byte_length > 0
        )
        input_rgb_length_matches_expected = (
            is_strict_int(input_rgb_byte_length)
            and is_strict_int(input_rgb_expected_byte_length)
            and input_rgb_byte_length == input_rgb_expected_byte_length
        )
        input_rgb_length_match_flag = input_rgb.get("byte_length_matches_expected")
        input_rgb_length_match_flag_present = isinstance(
            input_rgb_length_match_flag, bool
        )
        input_rgb_length_match_flag_matches = (
            input_rgb_length_match_flag_present
            and input_rgb_length_match_flag == input_rgb_length_matches_expected
        )
        evidence_present["input_rgb"] = (
            input_rgb_path_present
            and input_rgb_path_resolved_present
            and input_rgb_path_resolved_matches
            and input_rgb_byte_length_positive
            and input_rgb_sha256_present
            and input_rgb_expected_byte_length_positive
            and input_rgb_length_matches_expected
            and input_rgb_length_match_flag_present
            and input_rgb_length_match_flag_matches
        )
        checks["input_rgb_path_present"] = input_rgb_path_present
        checks["input_rgb_path_resolved_present"] = input_rgb_path_resolved_present
        checks["input_rgb_path_resolved_matches"] = input_rgb_path_resolved_matches
        checks["input_rgb_byte_length_positive"] = input_rgb_byte_length_positive
        checks["input_rgb_sha256_present"] = input_rgb_sha256_present
        checks["input_rgb_expected_byte_length_positive"] = (
            input_rgb_expected_byte_length_positive
        )
        checks["input_rgb_length_matches_expected"] = input_rgb_length_matches_expected
        checks["input_rgb_length_match_flag_present"] = (
            input_rgb_length_match_flag_present
        )
        checks["input_rgb_length_match_flag_matches"] = (
            input_rgb_length_match_flag_matches
        )

    stream_devices = record.get("stream_devices")
    if isinstance(stream_devices, dict):
        tx_device = stream_devices.get("tx_device")
        rx_device = stream_devices.get("rx_device")
        tx_device_resolved = stream_devices.get("tx_device_resolved")
        rx_device_resolved = stream_devices.get("rx_device_resolved")
        tx_device_present = isinstance(tx_device, str) and bool(tx_device)
        rx_device_present = isinstance(rx_device, str) and bool(rx_device)
        tx_device_resolved_present = (
            isinstance(tx_device_resolved, str) and bool(tx_device_resolved)
        )
        rx_device_resolved_present = (
            isinstance(rx_device_resolved, str) and bool(rx_device_resolved)
        )
        stream_devices_distinct = (
            tx_device_present and rx_device_present and tx_device != rx_device
        )
        tx_device_resolved_matches = (
            tx_device_present
            and tx_device_resolved_present
            and tx_device_resolved == str(Path(tx_device).resolve(strict=False))
        )
        rx_device_resolved_matches = (
            rx_device_present
            and rx_device_resolved_present
            and rx_device_resolved == str(Path(rx_device).resolve(strict=False))
        )
        stream_devices_resolved_distinct = (
            tx_device_resolved_present
            and rx_device_resolved_present
            and tx_device_resolved_matches
            and rx_device_resolved_matches
            and tx_device_resolved != rx_device_resolved
        )
        evidence_present["stream_devices"] = (
            tx_device_present
            and rx_device_present
            and tx_device_resolved_present
            and rx_device_resolved_present
            and stream_devices_distinct
            and tx_device_resolved_matches
            and rx_device_resolved_matches
            and stream_devices_resolved_distinct
        )
        checks["stream_tx_device_present"] = tx_device_present
        checks["stream_rx_device_present"] = rx_device_present
        checks["stream_devices_distinct"] = stream_devices_distinct
        checks["stream_tx_device_resolved_present"] = tx_device_resolved_present
        checks["stream_rx_device_resolved_present"] = rx_device_resolved_present
        checks["stream_tx_device_resolved_matches"] = tx_device_resolved_matches
        checks["stream_rx_device_resolved_matches"] = rx_device_resolved_matches
        checks["stream_devices_resolved_distinct"] = (
            stream_devices_resolved_distinct
        )

    capture_config = record.get("capture_config")
    if isinstance(capture_config, dict):
        max_output_bytes = capture_config.get("max_output_bytes")
        timeout_seconds = capture_config.get("timeout_seconds")
        capture_max_output_bytes_positive = (
            is_strict_int(max_output_bytes) and max_output_bytes > 0
        )
        capture_timeout_valid = timeout_seconds is None or (
            is_strict_number(timeout_seconds)
            and math.isfinite(timeout_seconds)
            and timeout_seconds > 0
        )
        evidence_present["capture_config"] = (
            capture_max_output_bytes_positive and capture_timeout_valid
        )
        checks["capture_max_output_bytes_positive"] = (
            capture_max_output_bytes_positive
        )
        checks["capture_timeout_valid"] = capture_timeout_valid

    axi_lite = record.get("axi_lite")
    if isinstance(axi_lite, dict):
        axi_lite_device = axi_lite.get("device")
        axi_lite_device_present = isinstance(axi_lite_device, str) and bool(
            axi_lite_device
        )
        axi_lite_base_addr = axi_lite.get("base_addr")
        axi_lite_base_addr_nonnegative = (
            is_strict_int(axi_lite_base_addr) and axi_lite_base_addr >= 0
        )
        axi_lite_base_addr_hex_matches = (
            axi_lite_base_addr_nonnegative
            and axi_lite.get("base_addr_hex") == f"0x{axi_lite_base_addr:x}"
        )
        evidence_present["axi_lite"] = (
            axi_lite_device_present
            and axi_lite_base_addr_nonnegative
            and axi_lite_base_addr_hex_matches
        )
        checks["axi_lite_device_present"] = axi_lite_device_present
        checks["axi_lite_base_addr_nonnegative"] = axi_lite_base_addr_nonnegative
        checks["axi_lite_base_addr_hex_matches"] = axi_lite_base_addr_hex_matches

    input_ppm = record.get("input_ppm")
    if isinstance(input_ppm, dict) and "packed_rgb_matches_input" in input_ppm:
        input_ppm_path = input_ppm.get("path")
        input_ppm_path_resolved = input_ppm.get("path_resolved")
        input_ppm_byte_length = input_ppm.get("byte_length")
        input_ppm_sha256 = input_ppm.get("sha256")
        input_ppm_width = input_ppm.get("width")
        input_ppm_height = input_ppm.get("height")
        input_ppm_rgb_bytes = input_ppm.get("rgb_bytes")
        input_ppm_packed_rgb_byte_length = input_ppm.get("packed_rgb_byte_length")
        input_ppm_packed_rgb_sha256 = input_ppm.get("packed_rgb_sha256")
        input_ppm_matches_flag = input_ppm.get("packed_rgb_matches_input")
        input_ppm_matches_input_flag_present = isinstance(
            input_ppm_matches_flag, bool
        )
        input_ppm_matches = input_ppm_matches_flag is True
        input_ppm_path_present = (
            isinstance(input_ppm_path, str) and len(input_ppm_path) > 0
        )
        input_ppm_path_resolved_present = (
            isinstance(input_ppm_path_resolved, str)
            and len(input_ppm_path_resolved) > 0
        )
        input_ppm_path_resolved_matches = (
            input_ppm_path_present
            and input_ppm_path_resolved_present
            and input_ppm_path_resolved
            == str(Path(input_ppm_path).resolve(strict=False))
        )
        input_ppm_byte_length_positive = (
            is_strict_int(input_ppm_byte_length) and input_ppm_byte_length > 0
        )
        input_ppm_sha256_present = is_sha256_hex(input_ppm_sha256)
        input_ppm_dimensions_positive = (
            is_strict_int(input_ppm_width)
            and is_strict_int(input_ppm_height)
            and input_ppm_width > 0
            and input_ppm_height > 0
        )
        input_ppm_rgb_byte_length_positive = (
            is_strict_int(input_ppm_rgb_bytes) and input_ppm_rgb_bytes > 0
        )
        input_ppm_packed_rgb_byte_length_positive = (
            is_strict_int(input_ppm_packed_rgb_byte_length)
            and input_ppm_packed_rgb_byte_length > 0
        )
        input_ppm_rgb_byte_length_matches_dimensions = (
            input_ppm_dimensions_positive
            and input_ppm_rgb_byte_length_positive
            and input_ppm_rgb_bytes == input_ppm_width * input_ppm_height * 3
        )
        input_ppm_packed_rgb_length_matches_dimensions = (
            input_ppm_dimensions_positive
            and input_ppm_packed_rgb_byte_length_positive
            and input_ppm_packed_rgb_byte_length == input_ppm_width * input_ppm_height * 4
        )
        input_ppm_packed_rgb_sha256_present = is_sha256_hex(
            input_ppm_packed_rgb_sha256
        )
        input_ppm_matches_input_flag_matches = False
        input_rgb = record.get("input_rgb")
        if isinstance(input_rgb, dict):
            input_rgb_byte_length = input_rgb.get("byte_length")
            input_rgb_sha256 = input_rgb.get("sha256")
            input_ppm_matches_input_derived = (
                input_ppm_packed_rgb_length_matches_dimensions
                and input_ppm_packed_rgb_byte_length == input_rgb_byte_length
                and input_ppm_packed_rgb_sha256_present
                and input_ppm_packed_rgb_sha256 == input_rgb_sha256
            )
            input_ppm_matches_input_flag_matches = (
                input_ppm_matches == input_ppm_matches_input_derived
            )
        checks["input_ppm_matches_input"] = input_ppm_matches
        checks["input_ppm_matches_input_flag_present"] = (
            input_ppm_matches_input_flag_present
        )
        checks["input_ppm_matches_input_flag_matches"] = (
            input_ppm_matches_input_flag_matches
        )
        checks["input_ppm_path_present"] = input_ppm_path_present
        checks["input_ppm_path_resolved_present"] = input_ppm_path_resolved_present
        checks["input_ppm_path_resolved_matches"] = input_ppm_path_resolved_matches
        checks["input_ppm_byte_length_positive"] = input_ppm_byte_length_positive
        checks["input_ppm_sha256_present"] = input_ppm_sha256_present
        checks["input_ppm_dimensions_positive"] = input_ppm_dimensions_positive
        checks["input_ppm_rgb_byte_length_positive"] = (
            input_ppm_rgb_byte_length_positive
        )
        checks["input_ppm_rgb_byte_length_matches_dimensions"] = (
            input_ppm_rgb_byte_length_matches_dimensions
        )
        checks["input_ppm_packed_rgb_byte_length_positive"] = (
            input_ppm_packed_rgb_byte_length_positive
        )
        checks["input_ppm_packed_rgb_length_matches_dimensions"] = (
            input_ppm_packed_rgb_length_matches_dimensions
        )
        checks["input_ppm_packed_rgb_sha256_present"] = (
            input_ppm_packed_rgb_sha256_present
        )
        input_ppm_non_flat = False
        input_ppm_has_color_pixels = False
        image_stats = input_ppm.get("image_stats")
        if isinstance(image_stats, dict):
            input_ppm_non_flat = image_stats.get("non_flat") is True
            input_ppm_has_color_pixels = image_stats.get("has_color_pixels") is True
        checks["input_ppm_non_flat"] = input_ppm_non_flat
        checks["input_ppm_has_color_pixels"] = input_ppm_has_color_pixels
        evidence_present["input_ppm"] = (
            input_ppm_matches
            and input_ppm_path_present
            and input_ppm_path_resolved_present
            and input_ppm_path_resolved_matches
            and input_ppm_byte_length_positive
            and input_ppm_sha256_present
            and input_ppm_dimensions_positive
            and input_ppm_rgb_byte_length_positive
            and input_ppm_rgb_byte_length_matches_dimensions
            and input_ppm_packed_rgb_byte_length_positive
            and input_ppm_packed_rgb_length_matches_dimensions
            and input_ppm_packed_rgb_sha256_present
            and input_ppm_matches_input_flag_present
            and input_ppm_matches_input_flag_matches
            and input_ppm_non_flat
            and input_ppm_has_color_pixels
        )

    if "status_checks" in record:
        status_checks = record.get("status_checks")
        status_checks_list_present = isinstance(status_checks, list)
        status_check_count_matches = (
            status_checks_list_present
            and record.get("status_check_count") == len(status_checks)
        )
        status_check_count_expected = (
            status_checks_list_present
            and len(status_checks) == len(RUN_STATUS_CHECK_CONTEXTS)
        )
        expected_status_contexts_present = (
            record.get("expected_status_check_contexts") == RUN_STATUS_CHECK_CONTEXTS
        )
        status_check_contexts = record.get("status_check_contexts")
        derived_status_check_contexts = (
            [
                str(status_check.get("context", ""))
                if isinstance(status_check, dict)
                else ""
                for status_check in status_checks
            ]
            if status_checks_list_present
            else []
        )
        status_check_contexts_match_list = (
            status_checks_list_present
            and status_check_contexts == derived_status_check_contexts
        )
        status_check_contexts_match_expected = (
            status_checks_list_present
            and derived_status_check_contexts == RUN_STATUS_CHECK_CONTEXTS
        )
        status_check_contexts_expected_flag = record.get(
            "status_check_contexts_match_expected"
        )
        status_check_contexts_expected_flag_present = isinstance(
            status_check_contexts_expected_flag, bool
        )
        status_check_contexts_expected_flag_matches = (
            status_check_contexts_expected_flag_present
            and status_check_contexts_expected_flag
            == status_check_contexts_match_expected
        )
        status_checks_have_status_words = status_checks_list_present and all(
            isinstance(status_check, dict)
            and is_strict_int(status_check.get("status"))
            for status_check in status_checks
        )
        status_checks_status_hex_matches = status_checks_list_present and all(
            isinstance(status_check, dict)
            and is_strict_int(status_check.get("status"))
            and status_check.get("status_hex")
            == f"0x{status_check.get('status') & 0xFFFFFFFF:08x}"
            for status_check in status_checks
        )
        status_checks_text_matches = status_checks_list_present and all(
            isinstance(status_check, dict)
            and is_strict_int(status_check.get("status"))
            and status_check.get("text") == status_text(status_check.get("status"))
            for status_check in status_checks
        )
        status_checks_busy_flag_matches = status_checks_list_present and all(
            isinstance(status_check, dict)
            and is_strict_int(status_check.get("status"))
            and (
                status_check.get("busy")
                is bool(status_check.get("status") & STATUS_BUSY)
            )
            for status_check in status_checks
        )
        status_checks_protocol_error_flag_matches = (
            status_checks_list_present
            and all(
                isinstance(status_check, dict)
                and is_strict_int(status_check.get("status"))
                and (
                    status_check.get("protocol_error")
                    is bool(status_check.get("status") & STATUS_PROTOCOL_ERROR)
                )
                for status_check in status_checks
            )
        )
        run_axi_lite = record.get("axi_lite")
        status_checks_have_axi_lite_targets = status_checks_list_present and all(
            isinstance(status_check, dict)
            and isinstance(status_check.get("axi_lite"), dict)
            for status_check in status_checks
        )
        status_checks_axi_lite_targets_match = (
            isinstance(run_axi_lite, dict)
            and status_checks_have_axi_lite_targets
            and all(
                status_check.get("axi_lite") == run_axi_lite
                for status_check in status_checks
                if isinstance(status_check, dict)
            )
        )
        status_checks_each_idle = status_checks_list_present and all(
            isinstance(status_check, dict)
            and is_strict_int(status_check.get("status"))
            and status_check.get("status") == 0
            and status_check.get("text") == "idle"
            and status_check.get("busy") is False
            and status_check.get("protocol_error") is False
            for status_check in status_checks
        )
        status_checks_all_idle = status_checks_each_idle
        status_checks_all_idle_flag = record.get("status_checks_all_idle")
        status_checks_all_idle_flag_present = isinstance(
            status_checks_all_idle_flag, bool
        )
        status_checks_all_idle_flag_matches = (
            status_checks_all_idle_flag_present
            and status_checks_all_idle_flag == status_checks_all_idle
        )
        derived_status_checks_any_protocol_error = (
            status_checks_list_present
            and any(
                isinstance(status_check, dict)
                and status_check.get("protocol_error") is True
                for status_check in status_checks
            )
        )
        status_checks_any_protocol_error_flag = record.get(
            "status_checks_any_protocol_error"
        )
        status_checks_any_protocol_error_flag_present = isinstance(
            status_checks_any_protocol_error_flag, bool
        )
        status_checks_any_protocol_error_flag_matches = (
            status_checks_any_protocol_error_flag_present
            and status_checks_any_protocol_error_flag
            == derived_status_checks_any_protocol_error
        )
        status_checks_no_protocol_error = (
            status_checks_list_present
            and not derived_status_checks_any_protocol_error
        )
        derived_status_checks_any_busy = (
            status_checks_list_present
            and any(
                isinstance(status_check, dict)
                and status_check.get("busy") is True
                for status_check in status_checks
            )
        )
        status_checks_any_busy_flag = record.get("status_checks_any_busy")
        status_checks_any_busy_flag_present = isinstance(
            status_checks_any_busy_flag, bool
        )
        status_checks_any_busy_flag_matches = (
            status_checks_any_busy_flag_present
            and status_checks_any_busy_flag == derived_status_checks_any_busy
        )
        status_checks_no_busy = (
            status_checks_list_present and not derived_status_checks_any_busy
        )
        evidence_present["status_checks"] = (
            status_checks_list_present
            and status_check_count_matches
            and status_check_count_expected
            and expected_status_contexts_present
            and status_check_contexts_match_list
            and status_check_contexts_match_expected
            and status_check_contexts_expected_flag_present
            and status_check_contexts_expected_flag_matches
            and status_checks_have_status_words
            and status_checks_status_hex_matches
            and status_checks_text_matches
            and status_checks_busy_flag_matches
            and status_checks_protocol_error_flag_matches
            and status_checks_have_axi_lite_targets
            and status_checks_axi_lite_targets_match
            and status_checks_each_idle
            and status_checks_all_idle
            and status_checks_all_idle_flag_present
            and status_checks_all_idle_flag_matches
            and status_checks_no_protocol_error
            and status_checks_any_protocol_error_flag_present
            and status_checks_any_protocol_error_flag_matches
            and status_checks_no_busy
            and status_checks_any_busy_flag_present
            and status_checks_any_busy_flag_matches
        )
        checks["status_checks_list_present"] = status_checks_list_present
        checks["status_check_count_matches"] = status_check_count_matches
        checks["status_check_count_expected"] = status_check_count_expected
        checks["expected_status_contexts_present"] = expected_status_contexts_present
        checks["status_check_contexts_match_list"] = status_check_contexts_match_list
        checks["status_check_contexts_match_expected"] = (
            status_check_contexts_match_expected
        )
        checks["status_check_contexts_expected_flag_present"] = (
            status_check_contexts_expected_flag_present
        )
        checks["status_check_contexts_expected_flag_matches"] = (
            status_check_contexts_expected_flag_matches
        )
        checks["status_checks_have_status_words"] = status_checks_have_status_words
        checks["status_checks_status_hex_matches"] = status_checks_status_hex_matches
        checks["status_checks_text_matches"] = status_checks_text_matches
        checks["status_checks_busy_flag_matches"] = status_checks_busy_flag_matches
        checks["status_checks_protocol_error_flag_matches"] = (
            status_checks_protocol_error_flag_matches
        )
        checks["status_checks_have_axi_lite_targets"] = (
            status_checks_have_axi_lite_targets
        )
        checks["status_checks_axi_lite_targets_match"] = (
            status_checks_axi_lite_targets_match
        )
        checks["status_checks_each_idle"] = status_checks_each_idle
        checks["status_checks_all_idle"] = status_checks_all_idle
        checks["status_checks_all_idle_flag_present"] = (
            status_checks_all_idle_flag_present
        )
        checks["status_checks_all_idle_flag_matches"] = (
            status_checks_all_idle_flag_matches
        )
        checks["status_checks_no_protocol_error"] = status_checks_no_protocol_error
        checks["status_checks_any_protocol_error_flag_present"] = (
            status_checks_any_protocol_error_flag_present
        )
        checks["status_checks_any_protocol_error_flag_matches"] = (
            status_checks_any_protocol_error_flag_matches
        )
        checks["status_checks_no_busy"] = status_checks_no_busy
        checks["status_checks_any_busy_flag_present"] = (
            status_checks_any_busy_flag_present
        )
        checks["status_checks_any_busy_flag_matches"] = (
            status_checks_any_busy_flag_matches
        )

    if "decoder_passed" in record:
        decoder_passed = record.get("decoder_passed") is True
        decoder_command = record.get("decoder_command")
        decoder_command_present = isinstance(decoder_command, str) and bool(
            decoder_command
        )
        decoder_timeout_seconds = record.get("decoder_timeout_seconds")
        decoder_timeout_seconds_positive = (
            is_strict_number(decoder_timeout_seconds)
            and math.isfinite(decoder_timeout_seconds)
            and decoder_timeout_seconds > 0
        )
        decoder_elapsed_seconds = record.get("decoder_elapsed_seconds")
        decoder_elapsed_seconds_nonnegative = (
            is_strict_number(decoder_elapsed_seconds)
            and math.isfinite(decoder_elapsed_seconds)
            and decoder_elapsed_seconds >= 0
        )
        decoder_returncode_zero = record.get("decoder_returncode") == 0
        decoder_argv = record.get("decoder_argv")
        decoder_argv_present = (
            isinstance(decoder_argv, list)
            and bool(decoder_argv)
            and all(isinstance(arg, str) for arg in decoder_argv)
        )
        decoder_stdout = record.get("decoder_stdout")
        decoder_stderr = record.get("decoder_stderr")
        decoder_stdout_present = isinstance(decoder_stdout, str)
        decoder_stderr_present = isinstance(decoder_stderr, str)
        decoder_stdout_length_matches = (
            decoder_stdout_present
            and is_strict_int(record.get("decoder_stdout_chars"))
            and record.get("decoder_stdout_chars") == len(decoder_stdout)
        )
        decoder_stderr_length_matches = (
            decoder_stderr_present
            and is_strict_int(record.get("decoder_stderr_chars"))
            and record.get("decoder_stderr_chars") == len(decoder_stderr)
        )
        decoder_argv_matches_command = False
        jpeg_path = record.get("jpeg")
        if (
            decoder_command_present
            and isinstance(jpeg_path, str)
            and isinstance(decoder_argv, list)
        ):
            try:
                decoder_argv_matches_command = decoder_argv == decoder_command_argv(
                    Path(jpeg_path), decoder_command
                )
            except ValueError:
                decoder_argv_matches_command = False
        decoder_output_capture_chars = record.get("decoder_output_capture_chars")
        decoder_output_capture_chars_positive = (
            is_strict_int(decoder_output_capture_chars)
            and decoder_output_capture_chars > 0
        )
        decoder_stdout_within_capture = (
            decoder_stdout_present
            and is_strict_int(decoder_output_capture_chars)
            and len(decoder_stdout) <= decoder_output_capture_chars
        )
        decoder_stderr_within_capture = (
            decoder_stderr_present
            and is_strict_int(decoder_output_capture_chars)
            and len(decoder_stderr) <= decoder_output_capture_chars
        )
        decoder_output_not_truncated = (
            record.get("decoder_stdout_truncated") is False
            and record.get("decoder_stderr_truncated") is False
        )
        evidence_present["decoder"] = (
            decoder_passed
            and decoder_command_present
            and decoder_timeout_seconds_positive
            and decoder_elapsed_seconds_nonnegative
            and decoder_returncode_zero
            and decoder_argv_present
            and decoder_argv_matches_command
            and decoder_stdout_present
            and decoder_stderr_present
            and decoder_stdout_length_matches
            and decoder_stderr_length_matches
            and decoder_output_capture_chars_positive
            and decoder_stdout_within_capture
            and decoder_stderr_within_capture
            and decoder_output_not_truncated
        )
        checks["decoder_passed"] = decoder_passed
        checks["decoder_command_present"] = decoder_command_present
        checks["decoder_timeout_seconds_positive"] = decoder_timeout_seconds_positive
        checks["decoder_elapsed_seconds_nonnegative"] = (
            decoder_elapsed_seconds_nonnegative
        )
        checks["decoder_returncode_zero"] = decoder_returncode_zero
        checks["decoder_argv_present"] = decoder_argv_present
        checks["decoder_argv_matches_command"] = decoder_argv_matches_command
        checks["decoder_stdout_present"] = decoder_stdout_present
        checks["decoder_stderr_present"] = decoder_stderr_present
        checks["decoder_stdout_length_matches"] = decoder_stdout_length_matches
        checks["decoder_stderr_length_matches"] = decoder_stderr_length_matches
        checks["decoder_output_capture_chars_positive"] = (
            decoder_output_capture_chars_positive
        )
        checks["decoder_stdout_within_capture"] = decoder_stdout_within_capture
        checks["decoder_stderr_within_capture"] = decoder_stderr_within_capture
        checks["decoder_output_not_truncated"] = decoder_output_not_truncated

    transfer_elapsed = record.get("transfer_elapsed_seconds")
    if "transfer_elapsed_seconds" in record:
        transfer_elapsed_positive = (
            is_strict_number(transfer_elapsed)
            and math.isfinite(transfer_elapsed)
            and transfer_elapsed > 0
        )
        host_transfer_rates = record.get("host_transfer_rates")
        host_transfer_rates_present = isinstance(host_transfer_rates, dict)
        input_rgb_rate = (
            host_transfer_rates.get("input_rgb_bytes_per_second")
            if host_transfer_rates_present
            else None
        )
        output_jpeg_rate = (
            host_transfer_rates.get("output_jpeg_bytes_per_second")
            if host_transfer_rates_present
            else None
        )
        host_input_rgb_rate_positive = (
            is_strict_number(input_rgb_rate)
            and math.isfinite(input_rgb_rate)
            and input_rgb_rate > 0
        )
        host_output_jpeg_rate_positive = (
            is_strict_number(output_jpeg_rate)
            and math.isfinite(output_jpeg_rate)
            and output_jpeg_rate > 0
        )
        input_rgb = record.get("input_rgb")
        input_rgb_byte_length = (
            input_rgb.get("byte_length") if isinstance(input_rgb, dict) else None
        )
        jpeg_byte_length = record.get("byte_length")
        host_input_rgb_rate_matches_elapsed = (
            is_strict_int(input_rgb_byte_length)
            and is_strict_number(input_rgb_rate)
            and transfer_elapsed_positive
            and math.isclose(
                input_rgb_rate,
                input_rgb_byte_length / transfer_elapsed,
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
        )
        host_output_jpeg_rate_matches_elapsed = (
            is_strict_int(jpeg_byte_length)
            and is_strict_number(output_jpeg_rate)
            and transfer_elapsed_positive
            and math.isclose(
                output_jpeg_rate,
                jpeg_byte_length / transfer_elapsed,
                rel_tol=1e-9,
                abs_tol=1e-12,
            )
        )
        evidence_present["transfer_timing"] = (
            transfer_elapsed_positive
            and host_transfer_rates_present
            and host_input_rgb_rate_positive
            and host_output_jpeg_rate_positive
            and host_input_rgb_rate_matches_elapsed
            and host_output_jpeg_rate_matches_elapsed
        )
        checks["transfer_elapsed_seconds_positive"] = transfer_elapsed_positive
        checks["host_transfer_rates_present"] = host_transfer_rates_present
        checks["host_input_rgb_rate_positive"] = host_input_rgb_rate_positive
        checks["host_output_jpeg_rate_positive"] = host_output_jpeg_rate_positive
        checks["host_input_rgb_rate_matches_elapsed"] = (
            host_input_rgb_rate_matches_elapsed
        )
        checks["host_output_jpeg_rate_matches_elapsed"] = (
            host_output_jpeg_rate_matches_elapsed
        )

    evidence_group_count = len(evidence_present)
    evidence_present_count = sum(
        1 for present in evidence_present.values() if present is True
    )
    present_evidence = [
        str(name) for name, present in evidence_present.items() if present is True
    ]
    missing_evidence = [
        str(name) for name, present in evidence_present.items() if present is not True
    ]
    evidence_missing_count = len(missing_evidence)
    recorded_check_count = len(checks)
    passing_check_count = sum(1 for passed in checks.values() if passed is True)
    passing_checks = [
        str(name) for name, passed in checks.items() if passed is True
    ]
    failing_checks = [
        str(name) for name, passed in checks.items() if passed is not True
    ]
    failing_check_count = len(failing_checks)
    all_recorded_checks_passed = not failing_checks
    required_evidence_groups = list(evidence_present.keys())
    recorded_check_names = list(checks.keys())
    return {
        "required_evidence_groups": required_evidence_groups,
        "evidence_present": evidence_present,
        "evidence_group_count": evidence_group_count,
        "evidence_present_count": evidence_present_count,
        "evidence_missing_count": evidence_missing_count,
        "present_evidence": present_evidence,
        "missing_evidence": missing_evidence,
        "checks": checks,
        "recorded_check_names": recorded_check_names,
        "recorded_check_count": recorded_check_count,
        "passing_check_count": passing_check_count,
        "passing_checks": passing_checks,
        "failing_check_count": failing_check_count,
        "failing_checks": failing_checks,
        "all_recorded_checks_passed": all_recorded_checks_passed,
        "complete_hardware_run_evidence": not missing_evidence
        and all_recorded_checks_passed,
    }


def run_evidence_record(
    jpeg: Path,
    info: JpegInfo,
    input_info: FileInfo | None = None,
    expected_input_rgb_bytes: int | None = None,
    axi_lite: dict[str, object] | None = None,
    encoder_config: dict[str, object] | None = None,
    capture_config: dict[str, object] | None = None,
    status_checks: list[dict[str, object]] | None = None,
    decoder_passed: bool | None = None,
    decoder_command: str | None = None,
    decoder_timeout_seconds: float | None = None,
    decoder_result: DecoderCommandResult | None = None,
    validation_expectations: dict[str, object] | None = None,
    transfer_elapsed_seconds: float | None = None,
    input_ppm: dict[str, object] | None = None,
    stream_devices: dict[str, object] | None = None,
) -> dict[str, object]:
    record = jpeg_info_record(
        jpeg,
        info,
        decoder_passed,
        decoder_command,
        decoder_timeout_seconds,
        decoder_result,
        validation_expectations,
    )
    if input_info is not None:
        input_record: dict[str, object] = {
            "path": input_info.path,
            "path_resolved": input_info.path_resolved,
            "byte_length": input_info.byte_length,
            "sha256": input_info.sha256,
        }
        if expected_input_rgb_bytes is not None:
            input_record["expected_byte_length"] = expected_input_rgb_bytes
            input_record["byte_length_matches_expected"] = (
                input_info.byte_length == expected_input_rgb_bytes
            )
        record["input_rgb"] = input_record
    if input_ppm is not None:
        record["input_ppm"] = input_ppm
    if stream_devices is not None:
        record["stream_devices"] = stream_devices
    if axi_lite is not None:
        record["axi_lite"] = axi_lite
    if encoder_config is not None:
        record["encoder_config"] = encoder_config
    if capture_config is not None:
        record["capture_config"] = capture_config
    if status_checks is not None:
        record["status_checks"] = status_checks
        record["status_check_count"] = len(status_checks)
        record["status_check_contexts"] = [
            str(status_check.get("context", "")) for status_check in status_checks
        ]
        record["expected_status_check_contexts"] = RUN_STATUS_CHECK_CONTEXTS
        record["status_check_contexts_match_expected"] = (
            record["status_check_contexts"] == RUN_STATUS_CHECK_CONTEXTS
        )
        record["status_checks_all_idle"] = all(
            status_check.get("text") == "idle" for status_check in status_checks
        )
        record["status_checks_any_protocol_error"] = any(
            bool(status_check.get("protocol_error", False))
            for status_check in status_checks
        )
        record["status_checks_any_busy"] = any(
            bool(status_check.get("busy", False)) for status_check in status_checks
        )
    if transfer_elapsed_seconds is not None:
        if not math.isfinite(transfer_elapsed_seconds) or transfer_elapsed_seconds < 0:
            raise ValueError("transfer elapsed seconds must be finite and nonnegative")
        record["transfer_elapsed_seconds"] = transfer_elapsed_seconds
        if transfer_elapsed_seconds > 0 and input_info is not None:
            record["host_transfer_rates"] = {
                "input_rgb_bytes_per_second": input_info.byte_length
                / transfer_elapsed_seconds,
                "output_jpeg_bytes_per_second": info.byte_length
                / transfer_elapsed_seconds,
            }
    record["hardware_run_summary"] = hardware_run_summary_record(record)
    return record


def run_stream_devices_arguments_record(args: argparse.Namespace) -> dict[str, object]:
    return {
        "dev": str(args.dev),
        "base_addr": args.base_addr,
        "tx_device": str(args.tx_device),
        "rx_device": str(args.rx_device),
        "input_rgb": str(args.input_rgb),
        "input_ppm": None if args.input_ppm is None else str(args.input_ppm),
        "output_jpeg": str(args.output_jpeg),
        "width": args.width,
        "height": args.height,
        "max_width": args.max_width,
        "max_height": args.max_height,
        "quality": args.quality,
        "restart_interval": args.restart_interval,
        "chroma_subsample": args.chroma_subsample,
        "emit_jfif": not args.no_jfif,
        "clear_error": args.clear_error,
        "max_output_bytes": args.max_output_bytes,
        "timeout_seconds": args.timeout_seconds,
        "decoder_command": args.decoder_command,
        "decoder_timeout_seconds": args.decoder_timeout_seconds,
        "require_complete_evidence": args.require_complete_evidence,
        "json": args.json,
    }


def check_run_evidence_record(
    path: Path,
    record: object,
    vivado_hjpeg_base_addresses: tuple[int, ...] = (),
) -> tuple[dict[str, object], list[str]]:
    result: dict[str, object] = {
        "path": str(path),
        "exists": True,
        "passed": False,
    }
    failures: list[str] = []
    if not isinstance(record, dict):
        failures.append(f"{path}: run evidence JSON root must be an object")
        result["error"] = "root is not an object"
        return result, failures

    computed_summary = hardware_run_summary_record(record)
    result["computed_hardware_run_summary"] = computed_summary

    summary = record.get("hardware_run_summary")
    if not isinstance(summary, dict):
        failures.append(f"{path}: missing hardware_run_summary object")
        result["error"] = "missing hardware_run_summary"
        return result, failures

    summary_matches_computed = summary == computed_summary
    if not summary_matches_computed:
        failures.append(
            f"{path}: hardware_run_summary does not match recomputed summary"
        )

    if not isinstance(summary.get("evidence_present"), dict):
        failures.append(
            f"{path}: missing hardware_run_summary.evidence_present object"
        )
    missing_evidence_value = computed_summary.get("missing_evidence")
    missing_evidence = (
        [str(name) for name in missing_evidence_value]
        if isinstance(missing_evidence_value, list)
        else []
    )
    present_evidence_value = computed_summary.get("present_evidence")
    present_evidence = (
        [str(name) for name in present_evidence_value]
        if isinstance(present_evidence_value, list)
        else []
    )
    failing_checks_value = computed_summary.get("failing_checks")
    failing_checks = (
        [str(name) for name in failing_checks_value]
        if isinstance(failing_checks_value, list)
        else []
    )
    passing_checks_value = computed_summary.get("passing_checks")
    passing_checks = (
        [str(name) for name in passing_checks_value]
        if isinstance(passing_checks_value, list)
        else []
    )
    complete = bool(computed_summary.get("complete_hardware_run_evidence", False))
    all_checks = bool(computed_summary.get("all_recorded_checks_passed", False))
    complete_evidence_flag_present = isinstance(
        record.get("complete_hardware_run_evidence"), bool
    )
    complete_evidence_matches = record.get("complete_hardware_run_evidence") is complete
    complete_evidence_required_flag_present = isinstance(
        record.get("complete_hardware_run_evidence_required"), bool
    )
    complete_evidence_required = (
        record.get("complete_hardware_run_evidence_required") is True
    )
    arguments = record.get("arguments")
    arguments_require_complete_evidence_flag_present = (
        isinstance(arguments, dict)
        and isinstance(arguments.get("require_complete_evidence"), bool)
    )
    arguments_require_complete_evidence = (
        isinstance(arguments, dict)
        and arguments.get("require_complete_evidence") is True
    )
    arguments_match_record = False
    if isinstance(arguments, dict):
        def optional_matches(
            evidence: object,
            pairs: Sequence[tuple[str, str]],
        ) -> bool:
            if not isinstance(evidence, dict):
                return True
            return all(
                arguments.get(argument_key) == evidence.get(evidence_key)
                for argument_key, evidence_key in pairs
            )

        stream_devices = record.get("stream_devices")
        input_rgb = record.get("input_rgb")
        input_ppm = record.get("input_ppm")
        axi_lite = record.get("axi_lite")
        encoder_config = record.get("encoder_config")
        capture_config = record.get("capture_config")
        arguments_match_record = (
            optional_matches(axi_lite, (("dev", "device"), ("base_addr", "base_addr")))
            and optional_matches(
                stream_devices,
                (("tx_device", "tx_device"), ("rx_device", "rx_device")),
            )
            and optional_matches(input_rgb, (("input_rgb", "path"),))
            and optional_matches(input_ppm, (("input_ppm", "path"),))
            and optional_matches(
                encoder_config,
                (
                    ("max_width", "max_width"),
                    ("max_height", "max_height"),
                    ("quality", "quality"),
                    ("restart_interval", "restart_interval"),
                    ("chroma_subsample", "chroma_subsample"),
                    ("emit_jfif", "emit_jfif"),
                    ("clear_error", "clear_error"),
                ),
            )
            and optional_matches(
                capture_config,
                (
                    ("max_output_bytes", "max_output_bytes"),
                    ("timeout_seconds", "timeout_seconds"),
                ),
            )
            and arguments.get("output_jpeg") == record.get("jpeg")
            and arguments.get("width") == record.get("width")
            and arguments.get("height") == record.get("height")
            and arguments.get("decoder_command") == record.get("decoder_command")
        )
    complete_evidence_missing_matches = (
        record.get("complete_hardware_run_evidence_missing") == missing_evidence
    )
    complete_evidence_failing_checks_matches = (
        record.get("complete_hardware_run_evidence_failing_checks") == failing_checks
    )
    result.update(
        {
            "complete_hardware_run_evidence": complete,
            "complete_hardware_run_evidence_flag_present": (
                complete_evidence_flag_present
            ),
            "complete_hardware_run_evidence_matches": complete_evidence_matches,
            "complete_hardware_run_evidence_required": complete_evidence_required,
            "complete_hardware_run_evidence_required_flag_present": (
                complete_evidence_required_flag_present
            ),
            "arguments_require_complete_evidence": (
                arguments_require_complete_evidence
            ),
            "arguments_require_complete_evidence_flag_present": (
                arguments_require_complete_evidence_flag_present
            ),
            "arguments_match_record": arguments_match_record,
            "complete_hardware_run_evidence_missing_matches": (
                complete_evidence_missing_matches
            ),
            "complete_hardware_run_evidence_failing_checks_matches": (
                complete_evidence_failing_checks_matches
            ),
            "all_recorded_checks_passed": all_checks,
            "hardware_run_summary_matches_computed": summary_matches_computed,
            "required_evidence_groups": computed_summary.get(
                "required_evidence_groups"
            ),
            "evidence_group_count": computed_summary.get("evidence_group_count"),
            "evidence_present_count": computed_summary.get("evidence_present_count"),
            "evidence_missing_count": computed_summary.get("evidence_missing_count"),
            "present_evidence": present_evidence,
            "missing_evidence": missing_evidence,
            "recorded_check_names": computed_summary.get("recorded_check_names"),
            "recorded_check_count": computed_summary.get("recorded_check_count"),
            "passing_check_count": computed_summary.get("passing_check_count"),
            "passing_checks": passing_checks,
            "failing_check_count": computed_summary.get("failing_check_count"),
            "failing_checks": failing_checks,
        }
    )
    width = record.get("width")
    height = record.get("height")
    if is_strict_int(width):
        result["width"] = int(width)
    if is_strict_int(height):
        result["height"] = int(height)
    encoder_config = record.get("encoder_config")
    if isinstance(encoder_config, dict):
        for source_key, result_key in (
            ("width", "encoder_width"),
            ("height", "encoder_height"),
            ("max_width", "encoder_max_width"),
            ("max_height", "encoder_max_height"),
            ("quality", "encoder_quality"),
            ("restart_interval", "encoder_restart_interval"),
            ("control", "encoder_control"),
        ):
            value = encoder_config.get(source_key)
            if is_strict_int(value):
                result[result_key] = int(value)
        for source_key, result_key in (
            ("chroma_subsample", "encoder_chroma_subsample"),
            ("emit_jfif", "encoder_emit_jfif"),
            ("clear_error", "encoder_clear_error"),
        ):
            value = encoder_config.get(source_key)
            if isinstance(value, bool):
                result[result_key] = value
        control_hex = encoder_config.get("control_hex")
        if isinstance(control_hex, str):
            result["encoder_control_hex"] = control_hex
    validation_expectations = record.get("validation_expectations")
    if isinstance(validation_expectations, dict):
        for source_key, result_key in (
            ("width", "validation_width"),
            ("height", "validation_height"),
            ("restart_interval", "validation_restart_interval"),
            ("expected_restart_markers", "validation_expected_restart_markers"),
            ("quality", "validation_quality"),
        ):
            value = validation_expectations.get(source_key)
            if is_strict_int(value):
                result[result_key] = int(value)
        for source_key, result_key in (
            ("check_chroma_mode", "validation_check_chroma_mode"),
            ("chroma_subsample", "validation_chroma_subsample"),
            ("require_standard_huffman", "validation_require_standard_huffman"),
        ):
            value = validation_expectations.get(source_key)
            if isinstance(value, bool):
                result[result_key] = value
        for source_key, result_key in (
            ("expected_chroma_mode", "validation_expected_chroma_mode"),
            ("expect_jfif", "validation_expect_jfif"),
        ):
            value = validation_expectations.get(source_key)
            if isinstance(value, str):
                result[result_key] = value
    status_check_count = record.get("status_check_count")
    if is_strict_int(status_check_count):
        result["status_check_count"] = int(status_check_count)
    for key in ("status_check_contexts", "expected_status_check_contexts"):
        value = record.get(key)
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            result[key] = list(value)
    for key in (
        "status_check_contexts_match_expected",
        "status_checks_all_idle",
        "status_checks_any_protocol_error",
        "status_checks_any_busy",
    ):
        value = record.get(key)
        if isinstance(value, bool):
            result[key] = value
    transfer_elapsed_seconds = record.get("transfer_elapsed_seconds")
    if is_finite_strict_number(transfer_elapsed_seconds):
        result["transfer_elapsed_seconds"] = transfer_elapsed_seconds
    host_transfer_rates = record.get("host_transfer_rates")
    if isinstance(host_transfer_rates, dict):
        input_rgb_rate = host_transfer_rates.get("input_rgb_bytes_per_second")
        output_jpeg_rate = host_transfer_rates.get("output_jpeg_bytes_per_second")
        if is_finite_strict_number(input_rgb_rate):
            result["host_input_rgb_bytes_per_second"] = input_rgb_rate
        if is_finite_strict_number(output_jpeg_rate):
            result["host_output_jpeg_bytes_per_second"] = output_jpeg_rate
    stream_devices = record.get("stream_devices")
    if isinstance(stream_devices, dict):
        stream_tx_device = stream_devices.get("tx_device")
        stream_rx_device = stream_devices.get("rx_device")
        stream_tx_device_resolved = stream_devices.get("tx_device_resolved")
        stream_rx_device_resolved = stream_devices.get("rx_device_resolved")
        if isinstance(stream_tx_device, str):
            result["stream_tx_device"] = stream_tx_device
        if isinstance(stream_rx_device, str):
            result["stream_rx_device"] = stream_rx_device
        if isinstance(stream_tx_device_resolved, str):
            result["stream_tx_device_resolved"] = stream_tx_device_resolved
        if isinstance(stream_rx_device_resolved, str):
            result["stream_rx_device_resolved"] = stream_rx_device_resolved
    axi_lite = record.get("axi_lite")
    if isinstance(axi_lite, dict):
        axi_lite_device = axi_lite.get("device")
        axi_lite_base_addr = axi_lite.get("base_addr")
        axi_lite_base_addr_hex = axi_lite.get("base_addr_hex")
        if isinstance(axi_lite_device, str):
            result["axi_lite_device"] = axi_lite_device
        if is_strict_int(axi_lite_base_addr):
            result["axi_lite_base_addr"] = int(axi_lite_base_addr)
            result["axi_lite_base_addr_hex"] = f"0x{int(axi_lite_base_addr):x}"
        if isinstance(axi_lite_base_addr_hex, str):
            result["recorded_axi_lite_base_addr_hex"] = axi_lite_base_addr_hex
    capture_config = record.get("capture_config")
    if isinstance(capture_config, dict):
        max_output_bytes = capture_config.get("max_output_bytes")
        timeout_seconds = capture_config.get("timeout_seconds")
        if is_strict_int(max_output_bytes):
            result["capture_max_output_bytes"] = int(max_output_bytes)
        if is_finite_strict_number(timeout_seconds):
            result["capture_timeout_seconds"] = timeout_seconds
    jpeg_path = record.get("jpeg")
    if isinstance(jpeg_path, str):
        result["jpeg"] = jpeg_path
    jpeg_path_resolved = record.get("jpeg_resolved")
    if isinstance(jpeg_path_resolved, str):
        result["jpeg_resolved"] = jpeg_path_resolved
    for source_key, result_key in (
        ("byte_length", "jpeg_byte_length"),
        ("mcu_count", "jpeg_mcu_count"),
        ("component_count", "jpeg_component_count"),
        ("scan_data_bytes", "jpeg_scan_data_bytes"),
        ("stuffed_ff_bytes", "jpeg_stuffed_ff_bytes"),
        ("app0_segments", "jpeg_app0_segments"),
        ("jfif_app0_segments", "jpeg_jfif_app0_segments"),
        ("dqt_segments", "jpeg_dqt_segments"),
        ("sof0_segments", "jpeg_sof0_segments"),
        ("dht_segments", "jpeg_dht_segments"),
        ("sos_segments", "jpeg_sos_segments"),
        ("dri_segments", "jpeg_dri_segments"),
        ("restart_interval", "jpeg_restart_interval"),
        ("restart_markers", "jpeg_restart_markers"),
    ):
        value = record.get(source_key)
        if is_strict_int(value):
            result[result_key] = int(value)
    for source_key, result_key in (
        ("sha256", "jpeg_sha256"),
        ("scan_data_sha256", "jpeg_scan_data_sha256"),
        ("chroma_mode", "jpeg_chroma_mode"),
    ):
        value = record.get(source_key)
        if isinstance(value, str):
            result[result_key] = value
    marker_sequence = record.get("marker_sequence")
    if (
        isinstance(marker_sequence, list)
        and all(isinstance(marker, str) for marker in marker_sequence)
    ):
        result["jpeg_marker_sequence"] = list(marker_sequence)
    restart_marker_sequence = record.get("restart_marker_sequence")
    if (
        isinstance(restart_marker_sequence, list)
        and all(is_strict_int(marker) for marker in restart_marker_sequence)
    ):
        result["jpeg_restart_marker_sequence"] = [
            int(marker) for marker in restart_marker_sequence
        ]
    input_rgb = record.get("input_rgb")
    if isinstance(input_rgb, dict):
        input_rgb_path = input_rgb.get("path")
        if isinstance(input_rgb_path, str):
            result["input_rgb"] = input_rgb_path
        input_rgb_path_resolved = input_rgb.get("path_resolved")
        if isinstance(input_rgb_path_resolved, str):
            result["input_rgb_resolved"] = input_rgb_path_resolved
        input_rgb_byte_length = input_rgb.get("byte_length")
        input_rgb_expected_byte_length = input_rgb.get("expected_byte_length")
        input_rgb_length_matches_expected = input_rgb.get(
            "byte_length_matches_expected"
        )
        if is_strict_int(input_rgb_byte_length):
            result["input_rgb_byte_length"] = int(input_rgb_byte_length)
        if is_strict_int(input_rgb_expected_byte_length):
            result["input_rgb_expected_byte_length"] = int(
                input_rgb_expected_byte_length
            )
        if isinstance(input_rgb_length_matches_expected, bool):
            result["input_rgb_length_matches_expected"] = (
                input_rgb_length_matches_expected
            )
    input_ppm = record.get("input_ppm")
    if isinstance(input_ppm, dict):
        input_ppm_path = input_ppm.get("path")
        if isinstance(input_ppm_path, str):
            result["input_ppm"] = input_ppm_path
        input_ppm_path_resolved = input_ppm.get("path_resolved")
        if isinstance(input_ppm_path_resolved, str):
            result["input_ppm_resolved"] = input_ppm_path_resolved
        for source_key, result_key in (
            ("width", "input_ppm_width"),
            ("height", "input_ppm_height"),
            ("byte_length", "input_ppm_byte_length"),
            ("rgb_bytes", "input_ppm_rgb_bytes"),
            ("packed_rgb_byte_length", "input_ppm_packed_rgb_byte_length"),
        ):
            value = input_ppm.get(source_key)
            if is_strict_int(value):
                result[result_key] = int(value)
        packed_rgb_matches_input = input_ppm.get("packed_rgb_matches_input")
        if isinstance(packed_rgb_matches_input, bool):
            result["input_ppm_packed_rgb_matches_input"] = packed_rgb_matches_input
        image_stats = input_ppm.get("image_stats")
        if isinstance(image_stats, dict):
            non_flat = image_stats.get("non_flat")
            has_color_pixels = image_stats.get("has_color_pixels")
            if isinstance(non_flat, bool):
                result["input_ppm_non_flat"] = non_flat
            if isinstance(has_color_pixels, bool):
                result["input_ppm_has_color_pixels"] = has_color_pixels
    decoder_command = record.get("decoder_command")
    if isinstance(decoder_command, str):
        result["decoder_command"] = decoder_command
    decoder_argv = record.get("decoder_argv")
    if isinstance(decoder_argv, list) and all(
        isinstance(arg, str) for arg in decoder_argv
    ):
        result["decoder_argv"] = list(decoder_argv)
    for key in (
        "decoder_passed",
        "decoder_stdout_truncated",
        "decoder_stderr_truncated",
    ):
        value = record.get(key)
        if isinstance(value, bool):
            result[key] = value
    for key in (
        "decoder_returncode",
        "decoder_stdout_chars",
        "decoder_stderr_chars",
        "decoder_output_capture_chars",
    ):
        value = record.get(key)
        if is_strict_int(value):
            result[key] = int(value)
    for key in ("decoder_timeout_seconds", "decoder_elapsed_seconds"):
        value = record.get(key)
        if is_finite_strict_number(value):
            result[key] = value
    if not complete:
        failures.append(f"{path}: complete_hardware_run_evidence is false")
    if not complete_evidence_flag_present:
        failures.append(
            f"{path}: complete_hardware_run_evidence is not a JSON boolean"
        )
    if not complete_evidence_matches:
        failures.append(
            f"{path}: top-level complete_hardware_run_evidence does not match "
            "recomputed summary"
        )
    if not complete_evidence_required:
        failures.append(f"{path}: complete hardware evidence was not required")
    if not complete_evidence_required_flag_present:
        failures.append(
            f"{path}: complete_hardware_run_evidence_required is not a JSON boolean"
        )
    if not arguments_require_complete_evidence_flag_present:
        failures.append(
            f"{path}: arguments.require_complete_evidence is not a JSON boolean"
        )
    if not arguments_require_complete_evidence:
        failures.append(f"{path}: arguments.require_complete_evidence is not true")
    if not arguments_match_record:
        failures.append(f"{path}: arguments do not match run evidence record")
    if not complete_evidence_missing_matches:
        failures.append(
            f"{path}: complete_hardware_run_evidence_missing does not match "
            "recomputed missing evidence"
        )
    if not complete_evidence_failing_checks_matches:
        failures.append(
            f"{path}: complete_hardware_run_evidence_failing_checks does not "
            "match recomputed failing checks"
        )
    if not all_checks:
        failures.append(f"{path}: all_recorded_checks_passed is false")
    if missing_evidence:
        failures.append(
            f"{path}: missing hardware evidence groups: {', '.join(missing_evidence)}"
        )
    if failing_checks:
        failures.append(f"{path}: failing hardware checks: {', '.join(failing_checks)}")

    if vivado_hjpeg_base_addresses:
        axi_lite_base_addr = (
            axi_lite.get("base_addr") if isinstance(axi_lite, dict) else None
        )
        axi_lite_base_matches_vivado = (
            is_strict_int(axi_lite_base_addr)
            and axi_lite_base_addr in vivado_hjpeg_base_addresses
        )
        result.update(
            {
                "vivado_hjpeg_base_addresses": list(vivado_hjpeg_base_addresses),
                "vivado_hjpeg_base_addresses_hex": [
                    f"0x{base:x}" for base in vivado_hjpeg_base_addresses
                ],
                "axi_lite_base_addr": axi_lite_base_addr,
                "axi_lite_base_matches_vivado_evidence": (
                    axi_lite_base_matches_vivado
                ),
            }
        )
        if not axi_lite_base_matches_vivado:
            failures.append(
                f"{path}: AXI-Lite base address {axi_lite_base_addr!r} does not "
                "match Vivado hjpeg_0/s_axi_lite address evidence "
                + ", ".join(f"0x{base:x}" for base in vivado_hjpeg_base_addresses)
            )
    result["passed"] = not failures
    return result, failures


def check_run_evidence_file(
    path: Path,
    vivado_hjpeg_base_addresses: tuple[int, ...] = (),
) -> tuple[dict[str, object], list[str]]:
    if not path.exists():
        record = {
            "path": str(path),
            "exists": False,
            "passed": False,
            "error": "file not found",
        }
        return record, [f"{path}: run evidence file not found"]
    if not path.is_file():
        record = {
            "path": str(path),
            "exists": True,
            "passed": False,
            "error": "not a file",
        }
        return record, [f"{path}: run evidence path is not a file"]
    try:
        parsed = strict_json_loads(path.read_text())
    except (json.JSONDecodeError, ValueError) as exc:
        record = {
            "path": str(path),
            "exists": True,
            "passed": False,
            "error": f"invalid JSON: {exc}",
        }
        return record, [f"{path}: invalid JSON: {exc}"]
    return check_run_evidence_record(path, parsed, vivado_hjpeg_base_addresses)


def vivado_hjpeg_base_addresses_from_record(record: object) -> tuple[int, ...]:
    if not isinstance(record, dict):
        return ()
    address_maps = record.get("address_map")
    if not isinstance(address_maps, list):
        return ()
    bases = []
    for address_map in address_maps:
        if not isinstance(address_map, dict) or address_map.get("passed") is not True:
            continue
        entries = address_map.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("interface") != "hjpeg_0/s_axi_lite":
                continue
            base_address = entry.get("base_address")
            if is_strict_int(base_address) and base_address >= 0:
                bases.append(base_address)
    return tuple(dict.fromkeys(bases))


def _hex_field_matches_int(value: object, expected: int | None) -> bool:
    if expected is None:
        return value is None
    if not isinstance(value, str):
        return False
    try:
        return int(value, 16) == expected
    except ValueError:
        return False


def vivado_address_map_hex_fields_consistent(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    address_maps = record.get("address_map")
    if not isinstance(address_maps, list):
        return False
    checked_entries = 0
    for address_map in address_maps:
        if not isinstance(address_map, dict) or address_map.get("passed") is not True:
            continue
        entries = address_map.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            base_address = entry.get("base_address")
            high_address = entry.get("high_address")
            if not is_strict_int(base_address) or base_address < 0:
                return False
            if high_address is not None and (
                not is_strict_int(high_address) or high_address < base_address
            ):
                return False
            if not _hex_field_matches_int(entry.get("base_address_hex"), base_address):
                return False
            if not _hex_field_matches_int(entry.get("high_address_hex"), high_address):
                return False
            checked_entries += 1
    return checked_entries > 0


def vivado_required_artifact_suffixes_present(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    artifact_suffixes = record.get("artifact_suffixes")
    if not isinstance(artifact_suffixes, dict):
        return False
    required_suffixes_present = artifact_suffixes.get("required_suffixes_present")
    return (
        artifact_suffixes.get("all_required_suffixes_present") is True
        and isinstance(required_suffixes_present, dict)
        and required_suffixes_present.get(".bit") is True
        and required_suffixes_present.get(".xsa") is True
        and required_suffixes_present.get(".dcp") is True
        and artifact_suffixes.get("missing_required_suffixes", []) == []
        and artifact_suffixes.get("failing_required_suffixes", []) == []
    )


def vivado_required_artifact_filenames_present(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    artifact_filenames = record.get("artifact_filenames")
    if not isinstance(artifact_filenames, dict):
        return False
    required_filenames_present = artifact_filenames.get("required_filenames_present")
    return (
        artifact_filenames.get("all_required_filenames_present") is True
        and isinstance(required_filenames_present, dict)
        and required_filenames_present.get("hjpeg_kv260.bit") is True
        and required_filenames_present.get("hjpeg_kv260.xsa") is True
        and required_filenames_present.get("post_impl.dcp") is True
        and artifact_filenames.get("missing_required_filenames", []) == []
        and artifact_filenames.get("failing_required_filenames", []) == []
    )


def vivado_required_address_map_filenames_present(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    address_map_filenames = record.get("address_map_filenames")
    if not isinstance(address_map_filenames, dict):
        return False
    required_filenames_present = address_map_filenames.get("required_filenames_present")
    return (
        address_map_filenames.get("all_required_filenames_present") is True
        and isinstance(required_filenames_present, dict)
        and required_filenames_present.get("hjpeg_kv260_address_map.rpt") is True
        and address_map_filenames.get("missing_required_filenames", []) == []
        and address_map_filenames.get("failing_required_filenames", []) == []
    )


def vivado_required_report_filenames_present(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    report_filenames = record.get("report_filenames")
    if not isinstance(report_filenames, dict):
        return False
    for category, required_filenames in VIVADO_REQUIRED_REPORT_FILENAMES.items():
        category_record = report_filenames.get(category)
        if not isinstance(category_record, dict):
            return False
        required_filenames_present = category_record.get("required_filenames_present")
        if (
            category_record.get("all_required_filenames_present") is not True
            or not isinstance(required_filenames_present, dict)
            or category_record.get("missing_required_filenames", []) != []
            or category_record.get("failing_required_filenames", []) != []
        ):
            return False
        for filename in required_filenames:
            if required_filenames_present.get(filename) is not True:
                return False
    return True


def vivado_required_hold_timing_filenames_present(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    hold_timing_filenames = record.get("hold_timing_filenames")
    if not isinstance(hold_timing_filenames, dict):
        return False
    required_filenames_present = hold_timing_filenames.get(
        "required_filenames_present"
    )
    if (
        hold_timing_filenames.get("all_required_filenames_present") is not True
        or not isinstance(required_filenames_present, dict)
        or hold_timing_filenames.get("missing_required_filenames", []) != []
        or hold_timing_filenames.get("failing_required_filenames", []) != []
    ):
        return False
    for filename in VIVADO_REQUIRED_HOLD_TIMING_FILENAMES:
        if required_filenames_present.get(filename) is not True:
            return False
    return True


def vivado_missing_report_filenames(record: object) -> dict[str, list[str]]:
    if not isinstance(record, dict):
        return {}
    report_filenames = record.get("report_filenames")
    if not isinstance(report_filenames, dict):
        return {}
    return {
        category: [
            str(filename)
            for filename in category_record.get("missing_required_filenames", [])
        ]
        for category, category_record in report_filenames.items()
        if isinstance(category_record, dict)
        and category_record.get("missing_required_filenames")
    }


def vivado_failing_report_filenames(record: object) -> dict[str, list[str]]:
    if not isinstance(record, dict):
        return {}
    report_filenames = record.get("report_filenames")
    if not isinstance(report_filenames, dict):
        return {}
    return {
        category: [
            str(filename)
            for filename in category_record.get("failing_required_filenames", [])
        ]
        for category, category_record in report_filenames.items()
        if isinstance(category_record, dict)
        and category_record.get("failing_required_filenames")
    }


def vivado_clock_target_present(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    clock_period_ns = record.get("clock_period_ns")
    clock_frequency_mhz = record.get("clock_frequency_mhz")
    clock_target = record.get("clock_target")
    if not (
        is_strict_number(clock_period_ns)
        and is_strict_number(clock_frequency_mhz)
        and math.isfinite(clock_period_ns)
        and math.isfinite(clock_frequency_mhz)
        and clock_period_ns > 0
        and clock_frequency_mhz > 0
    ):
        return False
    if not math.isclose(clock_frequency_mhz, 1000.0 / clock_period_ns, rel_tol=1e-12):
        return False
    if not (
        record.get("clock_target_valid") is True
        and isinstance(clock_target, dict)
        and clock_target.get("valid") is True
        and clock_target.get("clock_period_finite") is True
        and clock_target.get("clock_period_positive") is True
        and clock_target.get("clock_frequency_finite") is True
        and clock_target.get("clock_frequency_positive") is True
        and clock_target.get("period_frequency_match") is True
        and is_strict_number(clock_target.get("clock_period_ns"))
        and is_strict_number(clock_target.get("clock_frequency_mhz"))
    ):
        return False
    target_period_ns = clock_target["clock_period_ns"]
    target_frequency_mhz = clock_target["clock_frequency_mhz"]
    return (
        math.isfinite(target_period_ns)
        and math.isfinite(target_frequency_mhz)
        and math.isclose(target_period_ns, clock_period_ns, rel_tol=0.0, abs_tol=0.0)
        and math.isclose(
            target_frequency_mhz,
            clock_frequency_mhz,
            rel_tol=0.0,
            abs_tol=0.0,
        )
    )


def vivado_evidence_categories_present(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    evidence_categories = record.get("evidence_categories")
    if not isinstance(evidence_categories, dict):
        return False
    present = evidence_categories.get("present")
    passing_counts = evidence_categories.get("passing_counts")
    failing_counts = evidence_categories.get("failing_counts")
    if not (
        isinstance(present, dict)
        and isinstance(passing_counts, dict)
        and isinstance(failing_counts, dict)
        and evidence_categories.get("all_required_present") is True
        and evidence_categories.get("missing_required_categories", []) == []
        and evidence_categories.get("failing_categories", []) == []
    ):
        return False
    for category in VIVADO_REQUIRED_EVIDENCE_CATEGORIES:
        if present.get(category) is not True:
            return False
        if (
            not is_strict_int(passing_counts.get(category))
            or passing_counts[category] <= 0
        ):
            return False
        if (
            not is_strict_int(failing_counts.get(category))
            or failing_counts[category] != 0
        ):
            return False
    return True


def vivado_summary_counts_consistent(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    checked_count = record.get("checked_count")
    passed_count = record.get("passed_count")
    failed_count = record.get("failed_count")
    failure_count = record.get("failure_count")
    failures = record.get("failures")
    checked_counts = record.get("checked_counts")
    checked_paths = record.get("checked_paths")
    failed_paths = record.get("failed_paths")
    passed_paths = record.get("passed_paths")
    evidence_categories = record.get("evidence_categories")
    category_passing_counts = {}
    category_failing_counts = {}
    if isinstance(evidence_categories, dict):
        passing_counts = evidence_categories.get("passing_counts")
        failing_counts = evidence_categories.get("failing_counts")
        if isinstance(passing_counts, dict):
            category_passing_counts = passing_counts
        if isinstance(failing_counts, dict):
            category_failing_counts = failing_counts
    if not (
        is_strict_int(checked_count)
        and checked_count > 0
        and is_strict_int(passed_count)
        and is_strict_int(failed_count)
        and is_strict_int(failure_count)
        and isinstance(failures, list)
        and isinstance(checked_counts, dict)
        and isinstance(checked_paths, list)
        and isinstance(failed_paths, list)
        and isinstance(passed_paths, list)
    ):
        return False
    if set(checked_counts.keys()) != set(VIVADO_REQUIRED_EVIDENCE_CATEGORIES):
        return False
    return (
        passed_count == checked_count
        and failed_count == 0
        and failure_count == 0
        and failures == []
        and len(checked_paths) == checked_count
        and failed_paths == []
        and len(passed_paths) == passed_count
        and checked_paths == passed_paths
        and all(
            is_strict_int(checked_counts.get(category))
            and checked_counts[category] > 0
            and is_strict_int(category_passing_counts.get(category))
            and is_strict_int(category_failing_counts.get(category))
            and checked_counts[category]
            == category_passing_counts[category] + category_failing_counts[category]
            for category in VIVADO_REQUIRED_EVIDENCE_CATEGORIES
        )
        and sum(
            int(count)
            for count in checked_counts.values()
            if is_strict_int(count)
        )
        == checked_count
    )


def expected_vivado_diagnostic_summary(record: object) -> dict[str, object] | None:
    if not isinstance(record, dict):
        return None
    checked_count = record.get("checked_count")
    passed_count = record.get("passed_count")
    failed_count = record.get("failed_count")
    failure_count = record.get("failure_count")
    failures = record.get("failures")
    checked_counts = record.get("checked_counts")
    checked_paths = record.get("checked_paths")
    failed_paths = record.get("failed_paths")
    passed_paths = record.get("passed_paths")
    evidence_categories = record.get("evidence_categories")
    if not (
        is_strict_int(checked_count)
        and is_strict_int(passed_count)
        and is_strict_int(failed_count)
        and is_strict_int(failure_count)
        and isinstance(failures, list)
        and isinstance(checked_counts, dict)
        and isinstance(checked_paths, list)
        and isinstance(failed_paths, list)
        and isinstance(passed_paths, list)
        and isinstance(evidence_categories, dict)
    ):
        return None
    passing_counts = evidence_categories.get("passing_counts")
    failing_counts = evidence_categories.get("failing_counts")
    if not (isinstance(passing_counts, dict) and isinstance(failing_counts, dict)):
        return None
    checked_count_values = [
        checked_counts.get(category)
        for category in VIVADO_REQUIRED_EVIDENCE_CATEGORIES
    ]
    checked_counts_categories_match = set(checked_counts.keys()) == set(
        VIVADO_REQUIRED_EVIDENCE_CATEGORIES
    )
    checked_counts_strict_numbers = all(
        is_strict_int(count) and count >= 0 for count in checked_count_values
    )
    checked_counts_sum = sum(
        int(count)
        for count in checked_count_values
        if is_strict_int(count)
    )
    checked_counts_sum_matches = checked_counts_sum == checked_count
    checked_counts_positive = all(
        is_strict_int(count) and count > 0 for count in checked_count_values
    )
    checked_counts_match_categories = all(
        is_strict_int(checked_counts.get(category))
        and is_strict_int(passing_counts.get(category))
        and is_strict_int(failing_counts.get(category))
        and checked_counts[category]
        == passing_counts[category] + failing_counts[category]
        for category in VIVADO_REQUIRED_EVIDENCE_CATEGORIES
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
        and failure_count == 0
        and checked_counts_sum_matches
        and checked_counts_categories_match
        and checked_counts_strict_numbers
        and checked_counts_positive
        and checked_counts_match_categories
        and count_balance_valid
        and path_counts_valid
        and checked_paths_match_passed_paths
        and no_failed_paths
        and no_failures
    )
    return {
        "checked_count": checked_count,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "failure_count": failure_count,
        "checked_counts_sum": checked_counts_sum,
        "checked_counts_sum_matches": checked_counts_sum_matches,
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


def vivado_diagnostic_summary_consistent(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    diagnostic_summary = record.get("diagnostic_summary")
    expected = expected_vivado_diagnostic_summary(record)
    return isinstance(diagnostic_summary, dict) and diagnostic_summary == expected


def vivado_record_inventory_consistent(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    failures = record.get("failures")
    checked_counts = record.get("checked_counts")
    if not isinstance(failures, list) or not isinstance(checked_counts, dict):
        return False

    checked_records: list[dict[str, object]] = []
    for category in VIVADO_REQUIRED_EVIDENCE_CATEGORIES:
        records = record.get(category)
        if not isinstance(records, list):
            return False
        checked_category_count = checked_counts.get(category)
        if (
            not is_strict_int(checked_category_count)
            or checked_category_count != len(records)
        ):
            return False
        for item in records:
            if not isinstance(item, dict):
                return False
            checked_records.append(item)

    checked_paths = [str(item.get("path")) for item in checked_records]
    passed_paths = [
        str(item.get("path"))
        for item in checked_records
        if item.get("passed") is True
    ]
    failed_paths = [
        str(item.get("path"))
        for item in checked_records
        if item.get("passed") is not True
    ]
    checked_count = record.get("checked_count")
    passed_count = record.get("passed_count")
    failed_count = record.get("failed_count")
    failure_count = record.get("failure_count")
    if not (
        is_strict_int(checked_count)
        and is_strict_int(passed_count)
        and is_strict_int(failed_count)
        and is_strict_int(failure_count)
    ):
        return False
    return (
        checked_count == len(checked_records)
        and passed_count == len(passed_paths)
        and failed_count == len(failed_paths)
        and failure_count == len(failures)
        and record.get("checked_paths") == checked_paths
        and record.get("passed_paths") == passed_paths
        and record.get("failed_paths") == failed_paths
    )


def vivado_route_status_counts_present(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    route_records = record.get("route_status")
    if not isinstance(route_records, list):
        return False
    for route_record in route_records:
        if not isinstance(route_record, dict) or route_record.get("passed") is not True:
            continue
        counts = route_record.get("counts")
        required_counts = route_record.get("required_counts")
        missing_counts = route_record.get("missing_counts")
        if (
            isinstance(counts, dict)
            and isinstance(required_counts, list)
            and missing_counts == []
            and all(
                is_strict_int(counts.get(name)) and counts.get(name) == 0
                for name in VIVADO_REQUIRED_ROUTE_STATUS_COUNTS
            )
            and all(name in required_counts for name in VIVADO_REQUIRED_ROUTE_STATUS_COUNTS)
        ):
            return True
    return False


def vivado_floorplan_evidence_present(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    floorplan_records = record.get("floorplan")
    if not isinstance(floorplan_records, list):
        return False
    for floorplan_record in floorplan_records:
        if (
            isinstance(floorplan_record, dict)
            and floorplan_record.get("passed") is True
            and floorplan_record.get("exists") is True
            and is_strict_int(floorplan_record.get("pblock_count"))
            and floorplan_record["pblock_count"] >= 0
            and is_strict_int(floorplan_record.get("placed_cell_count"))
            and floorplan_record["placed_cell_count"] > 0
            and is_sha256_hex(floorplan_record.get("sha256"))
        ):
            return True
    return False


def vivado_record_hashes_present(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    for category in VIVADO_REQUIRED_EVIDENCE_CATEGORIES:
        records = record.get(category)
        if not isinstance(records, list):
            return False
        passing_records = [
            item for item in records
            if isinstance(item, dict) and item.get("passed") is True
        ]
        if not passing_records:
            return False
        if any(
            item.get("exists") is not True
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or not isinstance(item.get("path_resolved"), str)
            or not item["path_resolved"]
            or item["path_resolved"] != str(Path(item["path"]).resolve(strict=False))
            or not is_strict_int(item.get("byte_length"))
            or item["byte_length"] <= 0
            or not is_sha256_hex(item.get("sha256"))
            for item in passing_records
        ):
            return False
    return True


def vivado_evidence_file_record(path: Path) -> tuple[dict[str, object], list[str]]:
    result: dict[str, object] = {
        "path": str(path),
        "path_resolved": str(path.resolve(strict=False)),
        "exists": False,
        "passed": False,
        "hjpeg_base_addresses": [],
        "hjpeg_base_addresses_hex": [],
    }
    if not path.exists():
        return result, [f"{path}: Vivado evidence file not found"]
    result["exists"] = True
    try:
        parsed = strict_json_loads(path.read_text())
    except (json.JSONDecodeError, ValueError) as exc:
        return result, [f"{path}: invalid Vivado evidence JSON: {exc}"]

    vivado_passed_flag_present = (
        isinstance(parsed, dict) and isinstance(parsed.get("passed"), bool)
    )
    vivado_passed = isinstance(parsed, dict) and parsed.get("passed") is True
    complete_vivado_flow_evidence_flag_present = (
        isinstance(parsed, dict)
        and isinstance(parsed.get("complete_vivado_flow_evidence"), bool)
    )
    complete_vivado_flow_evidence = (
        isinstance(parsed, dict)
        and parsed.get("complete_vivado_flow_evidence") is True
    )
    complete_vivado_flow_evidence_required_flag_present = (
        isinstance(parsed, dict)
        and isinstance(parsed.get("complete_vivado_flow_evidence_required"), bool)
    )
    complete_vivado_flow_evidence_required = (
        isinstance(parsed, dict)
        and parsed.get("complete_vivado_flow_evidence_required") is True
    )
    arguments = parsed.get("arguments") if isinstance(parsed, dict) else None
    complete_vivado_flow_evidence_argument_required = (
        isinstance(arguments, dict)
        and arguments.get("require_complete_evidence") is True
    )
    complete_vivado_flow_evidence_argument_required_flag_present = (
        isinstance(arguments, dict)
        and isinstance(arguments.get("require_complete_evidence"), bool)
    )
    vivado_arguments_match_record = False
    if isinstance(parsed, dict) and isinstance(arguments, dict):
        def record_paths(record_key: str) -> list[str]:
            records = parsed.get(record_key)
            if not isinstance(records, list):
                return []
            return [
                str(record.get("path"))
                for record in records
                if isinstance(record, dict)
            ]

        timing_records = (
            parsed.get("timing") if isinstance(parsed.get("timing"), list) else []
        )
        utilization_records = (
            parsed.get("utilization")
            if isinstance(parsed.get("utilization"), list)
            else []
        )
        hold_timing_paths = [
            str(record.get("path"))
            for record in timing_records
            if isinstance(record, dict) and record.get("check_whs") is True
        ]
        argument_timing_paths = arguments.get("timing")
        argument_hold_timing_paths = arguments.get("hold_timing")
        if not isinstance(argument_timing_paths, list):
            argument_timing_paths = []
        if not isinstance(argument_hold_timing_paths, list):
            argument_hold_timing_paths = []
        expected_timing_paths = []
        for path_value in [*argument_timing_paths, *argument_hold_timing_paths]:
            if path_value not in expected_timing_paths:
                expected_timing_paths.append(path_value)
        timing_thresholds_match = all(
            isinstance(record, dict)
            and record.get("min_wns_ns") == arguments.get("min_wns")
            and record.get("min_whs_ns") == arguments.get("min_whs")
            for record in timing_records
        )
        utilization_thresholds_match = all(
            isinstance(record, dict)
            and record.get("max_percent") == arguments.get("max_utilization")
            for record in utilization_records
        )
        vivado_arguments_match_record = (
            arguments.get("artifacts") == record_paths("artifacts")
            and arguments.get("address_map") == record_paths("address_map")
            and expected_timing_paths == record_paths("timing")
            and arguments.get("hold_timing") == hold_timing_paths
            and arguments.get("utilization") == record_paths("utilization")
            and arguments.get("drc") == record_paths("drc")
            and arguments.get("route_status") == record_paths("route_status")
            and arguments.get("clock_utilization")
            == record_paths("clock_utilization")
            and arguments.get("floorplan") == record_paths("floorplan")
            and timing_thresholds_match
            and utilization_thresholds_match
            and arguments.get("clock_period_ns") == parsed.get("clock_period_ns")
        )
    vivado_artifact_suffixes_present = vivado_required_artifact_suffixes_present(parsed)
    vivado_artifact_filenames_present = vivado_required_artifact_filenames_present(parsed)
    vivado_address_map_filenames_present = (
        vivado_required_address_map_filenames_present(parsed)
    )
    vivado_report_filenames_present = vivado_required_report_filenames_present(parsed)
    vivado_hold_timing_filenames_present = (
        vivado_required_hold_timing_filenames_present(parsed)
    )
    clock_target_present = vivado_clock_target_present(parsed)
    evidence_categories_present = vivado_evidence_categories_present(parsed)
    summary_counts_consistent = vivado_summary_counts_consistent(parsed)
    diagnostic_summary_consistent = vivado_diagnostic_summary_consistent(parsed)
    record_inventory_consistent = vivado_record_inventory_consistent(parsed)
    route_status_counts_present = vivado_route_status_counts_present(parsed)
    floorplan_evidence_present = vivado_floorplan_evidence_present(parsed)
    address_map_hex_fields_consistent = vivado_address_map_hex_fields_consistent(parsed)
    record_hashes_present = vivado_record_hashes_present(parsed)
    bases = vivado_hjpeg_base_addresses_from_record(parsed)
    evidence_categories = (
        parsed.get("evidence_categories") if isinstance(parsed, dict) else None
    )
    artifact_suffixes = (
        parsed.get("artifact_suffixes") if isinstance(parsed, dict) else None
    )
    artifact_filenames = (
        parsed.get("artifact_filenames") if isinstance(parsed, dict) else None
    )
    address_map_filenames = (
        parsed.get("address_map_filenames") if isinstance(parsed, dict) else None
    )
    hold_timing_filenames = (
        parsed.get("hold_timing_filenames") if isinstance(parsed, dict) else None
    )
    expected_missing_categories = (
        evidence_categories.get("missing_required_categories", [])
        if isinstance(evidence_categories, dict)
        else []
    )
    expected_failing_categories = (
        evidence_categories.get("failing_categories", [])
        if isinstance(evidence_categories, dict)
        else []
    )
    expected_missing_suffixes = (
        artifact_suffixes.get("missing_required_suffixes", [])
        if isinstance(artifact_suffixes, dict)
        else []
    )
    expected_failing_suffixes = (
        artifact_suffixes.get("failing_required_suffixes", [])
        if isinstance(artifact_suffixes, dict)
        else []
    )
    expected_missing_filenames = (
        artifact_filenames.get("missing_required_filenames", [])
        if isinstance(artifact_filenames, dict)
        else []
    )
    expected_failing_filenames = (
        artifact_filenames.get("failing_required_filenames", [])
        if isinstance(artifact_filenames, dict)
        else []
    )
    expected_missing_address_map_filenames = (
        address_map_filenames.get("missing_required_filenames", [])
        if isinstance(address_map_filenames, dict)
        else []
    )
    expected_failing_address_map_filenames = (
        address_map_filenames.get("failing_required_filenames", [])
        if isinstance(address_map_filenames, dict)
        else []
    )
    expected_missing_report_filenames = vivado_missing_report_filenames(parsed)
    expected_failing_report_filenames = vivado_failing_report_filenames(parsed)
    expected_missing_hold_timing_filenames = (
        hold_timing_filenames.get("missing_required_filenames", [])
        if isinstance(hold_timing_filenames, dict)
        else []
    )
    expected_failing_hold_timing_filenames = (
        hold_timing_filenames.get("failing_required_filenames", [])
        if isinstance(hold_timing_filenames, dict)
        else []
    )
    complete_vivado_flow_evidence_recomputed = (
        vivado_artifact_suffixes_present
        and vivado_artifact_filenames_present
        and vivado_address_map_filenames_present
        and vivado_report_filenames_present
        and vivado_hold_timing_filenames_present
        and clock_target_present
        and evidence_categories_present
        and summary_counts_consistent
        and diagnostic_summary_consistent
        and record_inventory_consistent
        and route_status_counts_present
        and floorplan_evidence_present
        and address_map_hex_fields_consistent
        and record_hashes_present
    )
    complete_vivado_flow_evidence_matches = (
        isinstance(parsed, dict)
        and parsed.get("complete_vivado_flow_evidence")
        is complete_vivado_flow_evidence_recomputed
    )
    complete_vivado_flow_evidence_diagnostics_match = (
        isinstance(parsed, dict)
        and parsed.get("complete_vivado_flow_evidence_missing_categories")
        == expected_missing_categories
        and parsed.get("complete_vivado_flow_evidence_failing_categories")
        == expected_failing_categories
        and parsed.get("complete_vivado_flow_evidence_missing_suffixes")
        == expected_missing_suffixes
        and parsed.get("complete_vivado_flow_evidence_failing_suffixes")
        == expected_failing_suffixes
        and parsed.get("complete_vivado_flow_evidence_missing_filenames")
        == expected_missing_filenames
        and parsed.get("complete_vivado_flow_evidence_failing_filenames")
        == expected_failing_filenames
        and parsed.get("complete_vivado_flow_evidence_missing_address_map_filenames")
        == expected_missing_address_map_filenames
        and parsed.get("complete_vivado_flow_evidence_failing_address_map_filenames")
        == expected_failing_address_map_filenames
        and parsed.get("complete_vivado_flow_evidence_missing_report_filenames")
        == expected_missing_report_filenames
        and parsed.get("complete_vivado_flow_evidence_failing_report_filenames")
        == expected_failing_report_filenames
        and parsed.get("complete_vivado_flow_evidence_missing_hold_timing_filenames")
        == expected_missing_hold_timing_filenames
        and parsed.get("complete_vivado_flow_evidence_failing_hold_timing_filenames")
        == expected_failing_hold_timing_filenames
    )
    result.update(
        {
            "vivado_passed": vivado_passed,
            "vivado_passed_flag_present": vivado_passed_flag_present,
            "complete_vivado_flow_evidence": complete_vivado_flow_evidence,
            "complete_vivado_flow_evidence_flag_present": (
                complete_vivado_flow_evidence_flag_present
            ),
            "complete_vivado_flow_evidence_matches": (
                complete_vivado_flow_evidence_matches
            ),
            "complete_vivado_flow_evidence_required": (
                complete_vivado_flow_evidence_required
            ),
            "complete_vivado_flow_evidence_required_flag_present": (
                complete_vivado_flow_evidence_required_flag_present
            ),
            "complete_vivado_flow_evidence_argument_required": (
                complete_vivado_flow_evidence_argument_required
            ),
            "complete_vivado_flow_evidence_argument_required_flag_present": (
                complete_vivado_flow_evidence_argument_required_flag_present
            ),
            "vivado_arguments_match_record": vivado_arguments_match_record,
            "vivado_artifact_suffixes_present": vivado_artifact_suffixes_present,
            "vivado_artifact_filenames_present": vivado_artifact_filenames_present,
            "vivado_address_map_filenames_present": vivado_address_map_filenames_present,
            "vivado_report_filenames_present": vivado_report_filenames_present,
            "vivado_hold_timing_filenames_present": vivado_hold_timing_filenames_present,
            "vivado_clock_target_present": clock_target_present,
            "vivado_evidence_categories_present": evidence_categories_present,
            "vivado_summary_counts_consistent": summary_counts_consistent,
            "vivado_diagnostic_summary_consistent": diagnostic_summary_consistent,
            "vivado_record_inventory_consistent": record_inventory_consistent,
            "vivado_route_status_counts_present": route_status_counts_present,
            "vivado_floorplan_evidence_present": floorplan_evidence_present,
            "vivado_address_map_hex_fields_consistent": address_map_hex_fields_consistent,
            "vivado_record_hashes_present": record_hashes_present,
            "complete_vivado_flow_evidence_diagnostics_match": (
                complete_vivado_flow_evidence_diagnostics_match
            ),
            "hjpeg_base_addresses": list(bases),
            "hjpeg_base_addresses_hex": [f"0x{base:x}" for base in bases],
            "passed": (
                bool(bases)
                and vivado_passed_flag_present
                and vivado_passed
                and complete_vivado_flow_evidence_flag_present
                and complete_vivado_flow_evidence
                and complete_vivado_flow_evidence_matches
                and complete_vivado_flow_evidence_required_flag_present
                and complete_vivado_flow_evidence_required
                and complete_vivado_flow_evidence_argument_required_flag_present
                and complete_vivado_flow_evidence_argument_required
                and vivado_arguments_match_record
                and vivado_artifact_suffixes_present
                and vivado_artifact_filenames_present
                and vivado_address_map_filenames_present
                and vivado_report_filenames_present
                and vivado_hold_timing_filenames_present
                and clock_target_present
                and evidence_categories_present
                and summary_counts_consistent
                and diagnostic_summary_consistent
                and record_inventory_consistent
                and route_status_counts_present
                and floorplan_evidence_present
                and address_map_hex_fields_consistent
                and record_hashes_present
                and complete_vivado_flow_evidence_diagnostics_match
            ),
        }
    )
    failures = []
    if not vivado_passed_flag_present:
        failures.append(f"{path}: passed is not a JSON boolean")
    if not vivado_passed:
        failures.append(f"{path}: Vivado evidence did not pass")
    if not complete_vivado_flow_evidence_flag_present:
        failures.append(
            f"{path}: complete_vivado_flow_evidence is not a JSON boolean"
        )
    if not complete_vivado_flow_evidence:
        failures.append(f"{path}: complete_vivado_flow_evidence is false")
    if not complete_vivado_flow_evidence_matches:
        failures.append(
            f"{path}: top-level complete_vivado_flow_evidence does not match "
            "nested Vivado evidence summaries"
        )
    if not complete_vivado_flow_evidence_required:
        failures.append(
            f"{path}: complete_vivado_flow_evidence_required is not true"
        )
    if not complete_vivado_flow_evidence_required_flag_present:
        failures.append(
            f"{path}: complete_vivado_flow_evidence_required is not a JSON boolean"
        )
    if not complete_vivado_flow_evidence_argument_required_flag_present:
        failures.append(
            f"{path}: arguments.require_complete_evidence is not a JSON boolean"
        )
    if not complete_vivado_flow_evidence_argument_required:
        failures.append(
            f"{path}: arguments.require_complete_evidence is not true"
        )
    if not vivado_arguments_match_record:
        failures.append(f"{path}: arguments do not match Vivado evidence record")
    if not vivado_artifact_suffixes_present:
        failures.append(f"{path}: Vivado evidence missing required .bit/.xsa/.dcp artifacts")
    if not vivado_artifact_filenames_present:
        failures.append(
            f"{path}: Vivado evidence missing required bitstream/XSA/post-implementation checkpoint filenames"
        )
    if not vivado_address_map_filenames_present:
        failures.append(
            f"{path}: Vivado evidence missing required hjpeg_kv260_address_map.rpt filename"
        )
    if not vivado_report_filenames_present:
        failures.append(
            f"{path}: Vivado evidence missing required timing/utilization/implementation report filenames"
        )
    if not vivado_hold_timing_filenames_present:
        failures.append(
            f"{path}: Vivado evidence missing required post-implementation hold-timing filename"
        )
    if not clock_target_present:
        failures.append(
            f"{path}: Vivado evidence missing finite positive clock target"
        )
    if not evidence_categories_present:
        failures.append(
            f"{path}: Vivado evidence missing passing required evidence category summary"
        )
    if not summary_counts_consistent:
        failures.append(
            f"{path}: Vivado evidence diagnostic summary counts are inconsistent"
        )
    if not diagnostic_summary_consistent:
        failures.append(
            f"{path}: Vivado evidence diagnostic_summary does not match "
            "the aggregate Vivado evidence fields"
        )
    if not record_inventory_consistent:
        failures.append(
            f"{path}: Vivado evidence record inventory does not match nested records"
        )
    if not route_status_counts_present:
        failures.append(
            f"{path}: Vivado evidence missing required route-status unrouted/routing-error count records"
        )
    if not floorplan_evidence_present:
        failures.append(
            f"{path}: Vivado evidence missing post-implementation floorplan placed-cell evidence"
        )
    if not address_map_hex_fields_consistent:
        failures.append(
            f"{path}: Vivado evidence address-map hex fields do not match numeric address fields"
        )
    if not record_hashes_present:
        failures.append(
            f"{path}: Vivado evidence missing file metadata for passing required records"
        )
    if not complete_vivado_flow_evidence_diagnostics_match:
        failures.append(
            f"{path}: Vivado complete-evidence diagnostic lists do not match "
            "nested evidence summaries"
        )
    if not bases:
        failures.append(f"{path}: no passing hjpeg_0/s_axi_lite address-map evidence")
    return result, failures


def unique_string_values(records: list[dict[str, object]], key: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for record in records:
        record_values = record.get(key)
        if not isinstance(record_values, list):
            continue
        for value in record_values:
            text = str(value)
            if text not in seen:
                seen.add(text)
                values.append(text)
    return values


def unique_scalar_string_values(records: list[dict[str, object]], key: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for record in records:
        value = record.get(key)
        if not isinstance(value, str):
            continue
        if value not in seen:
            seen.add(value)
            values.append(value)
    return values


def unique_int_values(records: list[dict[str, object]], key: str) -> list[int]:
    values: list[int] = []
    seen: set[int] = set()
    for record in records:
        value = record.get(key)
        if not is_strict_int(value):
            continue
        number = int(value)
        if number not in seen:
            seen.add(number)
            values.append(number)
    return values


def unique_number_values(
    records: list[dict[str, object]], key: str
) -> list[int | float]:
    values: list[int | float] = []
    seen: set[int | float] = set()
    for record in records:
        value = record.get(key)
        if not is_strict_number(value):
            continue
        number = int(value) if isinstance(value, int) else float(value)
        if not math.isfinite(number):
            continue
        if number not in seen:
            seen.add(number)
            values.append(number)
    return values


def unique_bool_values(records: list[dict[str, object]], key: str) -> list[bool]:
    values: list[bool] = []
    seen: set[bool] = set()
    for record in records:
        value = record.get(key)
        if not isinstance(value, bool):
            continue
        if value not in seen:
            seen.add(value)
            values.append(value)
    return values


def require_capture_config(max_output_bytes: int, timeout_seconds: float | None) -> None:
    if max_output_bytes <= 0:
        raise ValueError("max output bytes must be positive")
    if timeout_seconds is not None and (
        not math.isfinite(timeout_seconds) or timeout_seconds <= 0
    ):
        raise ValueError("timeout seconds must be finite and positive")


def capture_config_record(max_output_bytes: int, timeout_seconds: float | None) -> dict[str, object]:
    require_capture_config(max_output_bytes, timeout_seconds)
    return {
        "max_output_bytes": max_output_bytes,
        "timeout_seconds": timeout_seconds,
    }


def require_stream_devices(tx_device: Path, rx_device: Path) -> None:
    if tx_device.resolve(strict=False) == rx_device.resolve(strict=False):
        raise ValueError("TX and RX stream devices must be distinct")


def stream_devices_record(tx_device: Path, rx_device: Path) -> dict[str, object]:
    require_stream_devices(tx_device, rx_device)
    return {
        "tx_device": str(tx_device),
        "rx_device": str(rx_device),
        "tx_device_resolved": str(tx_device.resolve(strict=False)),
        "rx_device_resolved": str(rx_device.resolve(strict=False)),
    }


def read_until_jpeg_eoi(stream: BinaryIO, max_bytes: int) -> bytes:
    if max_bytes <= 0:
        raise ValueError("max output bytes must be positive")

    output = bytearray()
    while len(output) < max_bytes:
        chunk = stream.read(min(4096, max_bytes - len(output)))
        if chunk == b"":
            break
        output.extend(chunk)
        eoi_offset = output.find(b"\xff\xd9")
        if eoi_offset >= 0:
            if eoi_offset + 2 != len(output):
                raise ValueError("RX device produced trailing data after JPEG EOI")
            return bytes(output[: eoi_offset + 2])

    raise ValueError(f"JPEG EOI not found within {max_bytes} output bytes")


def run_stream_devices(
    input_rgb: Path,
    output_jpeg: Path,
    tx_device: Path,
    rx_device: Path,
    max_output_bytes: int,
    expected_width: int,
    expected_height: int,
    expected_restart_interval: int | None = None,
    expected_chroma_subsample: bool | None = None,
    expected_emit_jfif: bool | None = None,
    quality: int = 50,
    max_width: int = DEFAULT_MAX_FRAME_WIDTH,
    max_height: int = DEFAULT_MAX_FRAME_HEIGHT,
    timeout_seconds: float | None = 30.0,
    configure: Callable[[], None] | None = None,
    check_status: Callable[[str], None] | None = None,
    decoder_command: str | None = None,
    decoder_timeout_seconds: float = 30.0,
    decoder_results: list[DecoderCommandResult] | None = None,
    transfer_elapsed_seconds: list[float] | None = None,
) -> tuple[JpegInfo, FileInfo]:
    require_supported_dimensions(expected_width, expected_height, max_width, max_height)
    require_capture_config(max_output_bytes, timeout_seconds)
    require_stream_devices(tx_device, rx_device)
    if not 1 <= quality <= 100:
        raise ValueError("quality must be in 1..100")
    if expected_restart_interval is not None and not 0 <= expected_restart_interval <= 0xFFFF:
        raise ValueError("restart interval must be in 0..65535")
    rgb = input_rgb.read_bytes()
    input_info = file_info(input_rgb, rgb)
    expected_input_bytes = expected_width * expected_height * 4
    if len(rgb) != expected_input_bytes:
        raise ValueError(
            f"{input_rgb}: expected {expected_input_bytes} RGB stream bytes for "
            f"{expected_width}x{expected_height}, found {len(rgb)}"
        )

    if configure is not None:
        configure()
    if check_status is not None:
        check_status("before transfer")

    read_result: list[bytes] = []
    read_errors: list[BaseException] = []

    with rx_device.open("rb", buffering=0) as rx_stream, tx_device.open(
        "wb", buffering=0
    ) as tx_stream:
        def read_rx() -> None:
            try:
                read_result.append(read_until_jpeg_eoi(rx_stream, max_output_bytes))
            except BaseException as exc:
                read_errors.append(exc)

        rx_thread = threading.Thread(target=read_rx, daemon=True)
        transfer_start = time.perf_counter()
        rx_thread.start()
        tx_stream.write(rgb)
        if hasattr(tx_stream, "flush"):
            tx_stream.flush()
        rx_thread.join(timeout_seconds)
        elapsed = time.perf_counter() - transfer_start
        if transfer_elapsed_seconds is not None:
            transfer_elapsed_seconds.append(elapsed)
        if rx_thread.is_alive():
            raise TimeoutError(
                f"RX device did not produce JPEG EOI within {timeout_seconds} seconds"
            )

    if read_errors:
        raise read_errors[0]
    if not read_result:
        raise ValueError("RX device produced no JPEG output")

    jpeg = read_result[0]
    with tempfile.NamedTemporaryFile(
        prefix=f".{output_jpeg.name}.",
        suffix=".tmp",
        dir=output_jpeg.parent,
        delete=False,
    ) as temp_file:
        temp_output = Path(temp_file.name)
    try:
        temp_output.write_bytes(jpeg)
        info = validate_jpeg(
            temp_output,
            expected_width,
            expected_height,
            expected_restart_interval=expected_restart_interval,
            expected_chroma_subsample=expected_chroma_subsample,
            expected_emit_jfif=expected_emit_jfif,
            expected_quality=quality,
            require_standard_huffman=True,
        )
        os.replace(temp_output, output_jpeg)
    except BaseException:
        if temp_output.exists():
            temp_output.unlink()
        raise
    if decoder_command is not None:
        decoder_result = run_decoder_command(
            output_jpeg,
            decoder_command,
            decoder_timeout_seconds,
        )
        if decoder_results is not None:
            decoder_results.append(decoder_result)
    if check_status is not None:
        check_status("after validation")
    return info, input_info


class AxiLiteWindow:
    def __init__(self, device: Path, base_address: int, aperture: int = AXI_LITE_APERTURE_BYTES):
        if base_address < 0:
            raise ValueError("base address must be nonnegative")
        self.device = device
        self.base_address = base_address
        self.aperture = aperture
        self.page_size = mmap.PAGESIZE
        self.page_base = base_address - (base_address % self.page_size)
        self.page_offset = base_address - self.page_base
        self.map_size = self.page_offset + aperture
        self.fd: int | None = None
        self.mapping: mmap.mmap | None = None

    def __enter__(self) -> "AxiLiteWindow":
        self.fd = os.open(self.device, os.O_RDWR | getattr(os, "O_SYNC", 0))
        self.mapping = mmap.mmap(self.fd, self.map_size, offset=self.page_base)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read32(self, offset: int) -> int:
        self._check_offset(offset)
        assert self.mapping is not None
        start = self.page_offset + offset
        return struct.unpack_from("<I", self.mapping, start)[0]

    def write32(self, offset: int, value: int) -> None:
        self._check_offset(offset)
        assert self.mapping is not None
        start = self.page_offset + offset
        struct.pack_into("<I", self.mapping, start, value & 0xFFFFFFFF)

    def _check_offset(self, offset: int) -> None:
        if offset < 0 or offset + 4 > self.aperture or offset % 4 != 0:
            raise ValueError(f"invalid 32-bit AXI-Lite offset 0x{offset:x}")


def configure_registers(
    regs: AxiLiteWindow,
    width: int,
    height: int,
    quality: int,
    restart_interval: int,
    chroma_subsample: bool,
    emit_jfif: bool,
    clear_error: bool,
    max_width: int = DEFAULT_MAX_FRAME_WIDTH,
    max_height: int = DEFAULT_MAX_FRAME_HEIGHT,
) -> None:
    require_supported_dimensions(width, height, max_width, max_height)
    if not 1 <= quality <= 100:
        raise ValueError("quality must be in 1..100")
    if not 0 <= restart_interval <= 0xFFFF:
        raise ValueError("restart interval must be in 0..65535")

    control = control_value(chroma_subsample, emit_jfif, clear_error)

    regs.write32(REG_XSIZE, width)
    regs.write32(REG_YSIZE, height)
    regs.write32(REG_QUALITY, quality)
    regs.write32(REG_RESTART_INTERVAL, restart_interval)
    regs.write32(REG_CONTROL, control)


def control_value(chroma_subsample: bool, emit_jfif: bool, clear_error: bool) -> int:
    control = 0
    if clear_error:
        control |= CONTROL_CLEAR_PROTOCOL_ERROR
    if chroma_subsample:
        control |= CONTROL_ENABLE_CHROMA_SUBSAMPLE
    if emit_jfif:
        control |= CONTROL_EMIT_JFIF
    return control


def encoder_config_record(
    width: int,
    height: int,
    quality: int,
    restart_interval: int,
    chroma_subsample: bool,
    emit_jfif: bool,
    clear_error: bool,
    max_width: int = DEFAULT_MAX_FRAME_WIDTH,
    max_height: int = DEFAULT_MAX_FRAME_HEIGHT,
) -> dict[str, object]:
    require_supported_dimensions(width, height, max_width, max_height)
    if not 1 <= quality <= 100:
        raise ValueError("quality must be in 1..100")
    if not 0 <= restart_interval <= 0xFFFF:
        raise ValueError("restart interval must be in 0..65535")

    control = control_value(chroma_subsample, emit_jfif, clear_error)
    return {
        "width": width,
        "height": height,
        "max_width": max_width,
        "max_height": max_height,
        "quality": quality,
        "restart_interval": restart_interval,
        "chroma_subsample": chroma_subsample,
        "emit_jfif": emit_jfif,
        "clear_error": clear_error,
        "control": control,
        "control_hex": f"0x{control:08x}",
    }


def axi_lite_target_record(device: Path, base_address: int) -> dict[str, object]:
    if base_address < 0:
        raise ValueError("base address must be nonnegative")

    return {
        "device": str(device),
        "base_addr": base_address,
        "base_addr_hex": f"0x{base_address:x}",
    }


def status_text(status: int) -> str:
    flags = []
    if status & STATUS_BUSY:
        flags.append("busy")
    if status & STATUS_PROTOCOL_ERROR:
        flags.append("protocol_error")
    return ",".join(flags) if flags else "idle"


def status_record(status: int) -> dict[str, object]:
    return {
        "status": status & 0xFFFFFFFF,
        "status_hex": f"0x{status & 0xFFFFFFFF:08x}",
        "busy": bool(status & STATUS_BUSY),
        "protocol_error": bool(status & STATUS_PROTOCOL_ERROR),
        "text": status_text(status),
    }


def status_evidence_record(device: Path, base_address: int, status: int) -> dict[str, object]:
    record = status_record(status)
    record["axi_lite"] = axi_lite_target_record(device, base_address)
    return record


def clear_error_record(device: Path, base_address: int, control: int) -> dict[str, object]:
    return {
        "axi_lite": axi_lite_target_record(device, base_address),
        "clear_protocol_error": True,
        "control": control & 0xFFFFFFFF,
        "control_hex": f"0x{control & 0xFFFFFFFF:08x}",
    }


def require_idle_status_value(status: int, context: str = "status") -> None:
    if status & STATUS_BUSY:
        raise RuntimeError(f"{context}: encoder is busy (status 0x{status:08x})")
    if status & STATUS_PROTOCOL_ERROR:
        raise RuntimeError(f"{context}: protocol_error is set (status 0x{status:08x})")


def require_idle_status(regs: AxiLiteWindow, context: str = "status") -> None:
    require_idle_status_value(regs.read32(REG_STATUS), context)


def clear_protocol_error(regs: AxiLiteWindow) -> int:
    current_control = regs.read32(REG_CONTROL)
    persistent_control = current_control & (
        CONTROL_ENABLE_CHROMA_SUBSAMPLE | CONTROL_EMIT_JFIF
    )
    control = persistent_control | CONTROL_CLEAR_PROTOCOL_ERROR
    regs.write32(REG_CONTROL, control)
    return control


def _integer_arg(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be an integer") from None


def _positive_int(value: str) -> int:
    parsed = _integer_arg(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = _integer_arg(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("value must be finite and positive") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be finite and positive")
    return parsed


def _quality_value(value: str) -> int:
    parsed = _integer_arg(value)
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("value must be in 1..100")
    return parsed


def _restart_interval_value(value: str) -> int:
    parsed = _integer_arg(value)
    if not 0 <= parsed <= 0xFFFF:
        raise argparse.ArgumentTypeError("value must be in 0..65535")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="hjpeg KV260 host-side helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pack = subparsers.add_parser("pack-ppm", help="pack a binary P6 PPM as RGB stream bytes")
    pack.add_argument("input", type=Path)
    pack.add_argument("output", type=Path)
    pack.add_argument("--max-width", type=_positive_int, default=DEFAULT_MAX_FRAME_WIDTH)
    pack.add_argument("--max-height", type=_positive_int, default=DEFAULT_MAX_FRAME_HEIGHT)
    pack.add_argument("--json", action="store_true", help="print packed stream evidence as JSON")

    make_ppm = subparsers.add_parser(
        "make-test-ppm",
        help="write a deterministic non-flat binary P6 PPM test image",
    )
    make_ppm.add_argument("output", type=Path)
    make_ppm.add_argument("--width", type=_positive_int, required=True)
    make_ppm.add_argument("--height", type=_positive_int, required=True)
    make_ppm.add_argument(
        "--pattern",
        choices=("gradient-checker", "seeded-random"),
        default="gradient-checker",
        help="deterministic pixel pattern (default: gradient-checker)",
    )
    make_ppm.add_argument("--max-width", type=_positive_int, default=DEFAULT_MAX_FRAME_WIDTH)
    make_ppm.add_argument("--max-height", type=_positive_int, default=DEFAULT_MAX_FRAME_HEIGHT)
    make_ppm.add_argument("--json", action="store_true", help="print generated PPM evidence as JSON")

    validate = subparsers.add_parser("validate-jpeg", help="validate JPEG markers and dimensions")
    validate.add_argument("jpeg", type=Path)
    validate.add_argument("--width", type=_positive_int, required=True)
    validate.add_argument("--height", type=_positive_int, required=True)
    validate.add_argument(
        "--restart-interval",
        type=_restart_interval_value,
        help="optional expected DRI restart interval; use 0 to require no DRI/RST markers",
    )
    validate.add_argument(
        "--chroma-subsample",
        action="store_true",
        help="require JPEG SOF0 sampling factors for 4:2:0 instead of 4:4:4",
    )
    validate.add_argument(
        "--check-chroma-mode",
        action="store_true",
        help="check SOF0 sampling factors against --chroma-subsample",
    )
    validate.add_argument(
        "--expect-jfif",
        choices=("present", "absent"),
        help="optionally require JFIF APP0 signature presence or absence",
    )
    validate.add_argument(
        "--decoder-command",
        help="optional external decoder command; {jpeg} is replaced with the JPEG path, otherwise the path is appended",
    )
    validate.add_argument(
        "--quality",
        type=_quality_value,
        help="optional expected quality for standard DQT payload validation",
    )
    validate.add_argument(
        "--require-standard-huffman",
        action="store_true",
        help="require the four standard baseline JPEG DHT payloads",
    )
    validate.add_argument(
        "--decoder-timeout-seconds",
        type=_positive_float,
        default=30.0,
        help="maximum seconds to wait for --decoder-command",
    )
    validate.add_argument("--json", action="store_true", help="print validation evidence as JSON")

    check_run = subparsers.add_parser(
        "check-run-evidence",
        help="check saved run-stream-devices JSON for complete hardware evidence",
    )
    check_run.add_argument("json_files", nargs="+", type=Path)
    check_run.add_argument(
        "--vivado-evidence",
        type=Path,
        action="append",
        default=[],
        help="optional check_reports.py JSON evidence whose hjpeg_0/s_axi_lite base address must match each run",
    )
    check_run.add_argument("--json", action="store_true", help="print check evidence as JSON")

    config = subparsers.add_parser("config", help="write encoder AXI-Lite configuration registers")
    config.add_argument("--dev", type=Path, default=Path("/dev/mem"))
    config.add_argument("--base-addr", type=_nonnegative_int, required=True)
    config.add_argument("--width", type=_positive_int, required=True)
    config.add_argument("--height", type=_positive_int, required=True)
    config.add_argument("--max-width", type=_positive_int, default=DEFAULT_MAX_FRAME_WIDTH)
    config.add_argument("--max-height", type=_positive_int, default=DEFAULT_MAX_FRAME_HEIGHT)
    config.add_argument("--quality", type=_quality_value, default=50)
    config.add_argument("--restart-interval", type=_restart_interval_value, default=0)
    config.add_argument("--chroma-subsample", action="store_true")
    config.add_argument("--no-jfif", action="store_true")
    config.add_argument("--clear-error", action="store_true")
    config.add_argument("--json", action="store_true", help="print configuration evidence as JSON")

    status = subparsers.add_parser("status", help="read encoder AXI-Lite status register")
    status.add_argument("--dev", type=Path, default=Path("/dev/mem"))
    status.add_argument("--base-addr", type=_nonnegative_int, required=True)
    status.add_argument("--json", action="store_true", help="print status evidence as JSON")

    clear = subparsers.add_parser("clear-error", help="pulse the protocol-error clear bit")
    clear.add_argument("--dev", type=Path, default=Path("/dev/mem"))
    clear.add_argument("--base-addr", type=_nonnegative_int, required=True)
    clear.add_argument("--json", action="store_true", help="print clear-error evidence as JSON")

    run = subparsers.add_parser(
        "run-stream-devices",
        help="configure hjpeg, stream RGB to a TX device, and capture JPEG from an RX device",
    )
    run.add_argument("--dev", type=Path, default=Path("/dev/mem"))
    run.add_argument("--base-addr", type=_nonnegative_int, required=True)
    run.add_argument("--tx-device", type=Path, required=True)
    run.add_argument("--rx-device", type=Path, required=True)
    run.add_argument("--input-rgb", type=Path, required=True)
    run.add_argument(
        "--input-ppm",
        type=Path,
        help="optional source PPM fixture to validate and record alongside --input-rgb",
    )
    run.add_argument("--output-jpeg", type=Path, required=True)
    run.add_argument("--width", type=_positive_int, required=True)
    run.add_argument("--height", type=_positive_int, required=True)
    run.add_argument("--max-width", type=_positive_int, default=DEFAULT_MAX_FRAME_WIDTH)
    run.add_argument("--max-height", type=_positive_int, default=DEFAULT_MAX_FRAME_HEIGHT)
    run.add_argument("--quality", type=_quality_value, default=50)
    run.add_argument("--restart-interval", type=_restart_interval_value, default=0)
    run.add_argument("--chroma-subsample", action="store_true")
    run.add_argument("--no-jfif", action="store_true")
    run.add_argument("--clear-error", action="store_true")
    run.add_argument("--max-output-bytes", type=_positive_int, default=16 * 1024 * 1024)
    run.add_argument("--timeout-seconds", type=_positive_float, default=30.0)
    run.add_argument(
        "--decoder-command",
        help="optional external decoder command to prove the captured JPEG opens",
    )
    run.add_argument(
        "--decoder-timeout-seconds",
        type=_positive_float,
        default=30.0,
        help="maximum seconds to wait for --decoder-command",
    )
    run.add_argument(
        "--require-complete-evidence",
        action="store_true",
        help="fail unless run JSON contains complete hardware evidence",
    )
    run.add_argument("--json", action="store_true", help="print capture evidence as JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "pack-ppm":
        image = read_ppm(args.input, args.max_width, args.max_height)
        write_rgb_stream(image, args.output)
        if args.json:
            output_data = args.output.read_bytes()
            print(
                strict_json_dumps(
                    {
                        "input_ppm": ppm_evidence_record(args.input, image),
                        "output_rgb": file_info_record(file_info(args.output, output_data)),
                        "width": image.width,
                        "height": image.height,
                        "max_width": args.max_width,
                        "max_height": args.max_height,
                        "expected_rgb_stream_bytes": image.width * image.height * 4,
                    },
                    sort_keys=True,
                )
            )
            return 0
        print(f"wrote {image.width * image.height * 4} RGB stream bytes for {image.width}x{image.height}")
        return 0

    if args.command == "make-test-ppm":
        require_supported_dimensions(args.width, args.height, args.max_width, args.max_height)
        image = make_test_image(args.width, args.height, args.pattern)
        write_ppm(image, args.output)
        if args.json:
            print(
                strict_json_dumps(
                    {
                        "output_ppm": ppm_evidence_record(args.output, image),
                        "deterministic_pattern": True,
                        "pattern": args.pattern,
                        "max_width": args.max_width,
                        "max_height": args.max_height,
                    },
                    sort_keys=True,
                )
            )
            return 0
        print(f"wrote deterministic {args.pattern} P6 PPM {args.width}x{args.height} to {args.output}")
        return 0

    if args.command == "validate-jpeg":
        expected_emit_jfif = None
        if args.expect_jfif is not None:
            expected_emit_jfif = args.expect_jfif == "present"
        info = validate_jpeg(
            args.jpeg,
            args.width,
            args.height,
            expected_restart_interval=args.restart_interval,
            expected_chroma_subsample=args.chroma_subsample if args.check_chroma_mode else None,
            expected_emit_jfif=expected_emit_jfif,
            expected_quality=args.quality,
            require_standard_huffman=args.require_standard_huffman,
        )
        decoder_passed = None
        decoder_result = None
        if args.decoder_command is not None:
            decoder_result = run_decoder_command(
                args.jpeg,
                args.decoder_command,
                args.decoder_timeout_seconds,
            )
            decoder_passed = True
        decoder_timeout = (
            args.decoder_timeout_seconds if args.decoder_command is not None else None
        )
        if args.json:
            print(
                strict_json_dumps(
                    jpeg_info_record(
                        args.jpeg,
                        info,
                        decoder_passed,
                        args.decoder_command,
                        decoder_timeout,
                        decoder_result,
                        validation_expectations_record(
                            info=info,
                            width=args.width,
                            height=args.height,
                            restart_interval=args.restart_interval,
                            check_chroma_mode=args.check_chroma_mode,
                            chroma_subsample=args.chroma_subsample
                            if args.check_chroma_mode
                            else None,
                            expect_jfif=args.expect_jfif,
                            quality=args.quality,
                            require_standard_huffman=args.require_standard_huffman,
                        ),
                    ),
                    sort_keys=True,
                )
            )
            return 0
        if decoder_passed:
            decoder_text = " decoder=pass"
        else:
            decoder_text = ""
        print(
            f"{args.jpeg}: valid baseline JPEG dimensions {info.width}x{info.height}; "
            f"scan_data_bytes={info.scan_data_bytes} "
            f"scan_data_sha256={info.scan_data_sha256} "
            f"stuffed_ff_bytes={info.stuffed_ff_bytes} "
            f"byte_length={info.byte_length} "
            f"sha256={info.sha256}{decoder_text}"
        )
        return 0

    if args.command == "check-run-evidence":
        vivado_records = []
        vivado_failures = []
        for vivado_path in args.vivado_evidence:
            vivado_record, record_failures = vivado_evidence_file_record(vivado_path)
            vivado_records.append(vivado_record)
            vivado_failures.extend(record_failures)
        vivado_hjpeg_base_addresses = tuple(
            dict.fromkeys(
                base
                for vivado_record in vivado_records
                for base in vivado_record.get("hjpeg_base_addresses", [])
                if is_strict_int(base)
            )
        )
        vivado_hjpeg_base_addresses_consistent = len(vivado_hjpeg_base_addresses) <= 1
        if not vivado_hjpeg_base_addresses_consistent:
            vivado_failures.append(
                "conflicting Vivado hjpeg_0/s_axi_lite base addresses: "
                + ", ".join(f"0x{base:x}" for base in vivado_hjpeg_base_addresses)
            )
        records = []
        failures = list(vivado_failures)
        for evidence_path in args.json_files:
            record, record_failures = check_run_evidence_file(
                evidence_path,
                vivado_hjpeg_base_addresses,
            )
            records.append(record)
            failures.extend(record_failures)
        passed_count = sum(1 for record in records if record.get("passed") is True)
        failed_count = len(records) - passed_count
        checked_paths = [str(record.get("path")) for record in records]
        passed_paths = [
            str(record.get("path"))
            for record in records
            if record.get("passed") is True
        ]
        failed_paths = [
            str(record.get("path"))
            for record in records
            if record.get("passed") is not True
        ]
        aggregate_evidence_group_count = sum(
            int(record.get("evidence_group_count", 0))
            for record in records
            if is_strict_int(record.get("evidence_group_count"))
        )
        aggregate_evidence_present_count = sum(
            int(record.get("evidence_present_count", 0))
            for record in records
            if is_strict_int(record.get("evidence_present_count"))
        )
        aggregate_evidence_missing_count = sum(
            int(record.get("evidence_missing_count", 0))
            for record in records
            if is_strict_int(record.get("evidence_missing_count"))
        )
        aggregate_recorded_check_count = sum(
            int(record.get("recorded_check_count", 0))
            for record in records
            if is_strict_int(record.get("recorded_check_count"))
        )
        aggregate_passing_check_count = sum(
            int(record.get("passing_check_count", 0))
            for record in records
            if is_strict_int(record.get("passing_check_count"))
        )
        aggregate_failing_check_count = sum(
            int(record.get("failing_check_count", 0))
            for record in records
            if is_strict_int(record.get("failing_check_count"))
        )
        summary_checked_paths = [
            str(record.get("path"))
            for record in records
            if isinstance(record.get("hardware_run_summary_matches_computed"), bool)
        ]
        summary_matched_paths = [
            str(record.get("path"))
            for record in records
            if record.get("hardware_run_summary_matches_computed") is True
        ]
        summary_mismatched_paths = [
            str(record.get("path"))
            for record in records
            if record.get("hardware_run_summary_matches_computed") is False
        ]
        summary_all_checked = len(summary_checked_paths) == len(records)
        summary_all_matched = summary_all_checked and not summary_mismatched_paths
        aggregate_present_evidence = unique_string_values(records, "present_evidence")
        aggregate_missing_evidence = unique_string_values(records, "missing_evidence")
        aggregate_passing_checks = unique_string_values(records, "passing_checks")
        aggregate_failing_checks = unique_string_values(records, "failing_checks")
        aggregate_stream_tx_devices = unique_scalar_string_values(
            records, "stream_tx_device"
        )
        aggregate_stream_rx_devices = unique_scalar_string_values(
            records, "stream_rx_device"
        )
        aggregate_stream_tx_device_resolved = unique_scalar_string_values(
            records, "stream_tx_device_resolved"
        )
        aggregate_stream_rx_device_resolved = unique_scalar_string_values(
            records, "stream_rx_device_resolved"
        )
        aggregate_axi_lite_devices = unique_scalar_string_values(
            records, "axi_lite_device"
        )
        aggregate_axi_lite_base_addresses = unique_int_values(
            records, "axi_lite_base_addr"
        )
        aggregate_axi_lite_base_addresses_hex = [
            f"0x{base:x}" for base in aggregate_axi_lite_base_addresses
        ]
        aggregate_frame_widths = unique_int_values(records, "width")
        aggregate_frame_heights = unique_int_values(records, "height")
        aggregate_encoder_widths = unique_int_values(records, "encoder_width")
        aggregate_encoder_heights = unique_int_values(records, "encoder_height")
        aggregate_encoder_max_widths = unique_int_values(records, "encoder_max_width")
        aggregate_encoder_max_heights = unique_int_values(
            records, "encoder_max_height"
        )
        aggregate_encoder_qualities = unique_int_values(records, "encoder_quality")
        aggregate_encoder_restart_intervals = unique_int_values(
            records, "encoder_restart_interval"
        )
        aggregate_encoder_controls = unique_int_values(records, "encoder_control")
        aggregate_encoder_control_hex_values = unique_scalar_string_values(
            records, "encoder_control_hex"
        )
        aggregate_encoder_chroma_subsample_values = unique_bool_values(
            records, "encoder_chroma_subsample"
        )
        aggregate_encoder_emit_jfif_values = unique_bool_values(
            records, "encoder_emit_jfif"
        )
        aggregate_encoder_clear_error_values = unique_bool_values(
            records, "encoder_clear_error"
        )
        aggregate_validation_widths = unique_int_values(records, "validation_width")
        aggregate_validation_heights = unique_int_values(records, "validation_height")
        aggregate_validation_restart_intervals = unique_int_values(
            records, "validation_restart_interval"
        )
        aggregate_validation_expected_restart_markers = unique_int_values(
            records, "validation_expected_restart_markers"
        )
        aggregate_validation_qualities = unique_int_values(
            records, "validation_quality"
        )
        aggregate_validation_check_chroma_mode_values = unique_bool_values(
            records, "validation_check_chroma_mode"
        )
        aggregate_validation_chroma_subsample_values = unique_bool_values(
            records, "validation_chroma_subsample"
        )
        aggregate_validation_require_standard_huffman_values = unique_bool_values(
            records, "validation_require_standard_huffman"
        )
        aggregate_validation_expected_chroma_modes = unique_scalar_string_values(
            records, "validation_expected_chroma_mode"
        )
        aggregate_validation_expect_jfif_values = unique_scalar_string_values(
            records, "validation_expect_jfif"
        )
        aggregate_status_check_counts = unique_int_values(
            records, "status_check_count"
        )
        aggregate_status_check_contexts = unique_string_values(
            records, "status_check_contexts"
        )
        aggregate_expected_status_check_contexts = unique_string_values(
            records, "expected_status_check_contexts"
        )
        aggregate_status_check_contexts_match_expected_values = unique_bool_values(
            records, "status_check_contexts_match_expected"
        )
        aggregate_status_checks_all_idle_values = unique_bool_values(
            records, "status_checks_all_idle"
        )
        aggregate_status_checks_any_protocol_error_values = unique_bool_values(
            records, "status_checks_any_protocol_error"
        )
        aggregate_status_checks_any_busy_values = unique_bool_values(
            records, "status_checks_any_busy"
        )
        aggregate_transfer_elapsed_seconds = unique_number_values(
            records, "transfer_elapsed_seconds"
        )
        aggregate_host_input_rgb_bytes_per_second = unique_number_values(
            records, "host_input_rgb_bytes_per_second"
        )
        aggregate_host_output_jpeg_bytes_per_second = unique_number_values(
            records, "host_output_jpeg_bytes_per_second"
        )
        aggregate_capture_max_output_bytes = unique_int_values(
            records, "capture_max_output_bytes"
        )
        aggregate_capture_timeout_seconds = unique_number_values(
            records, "capture_timeout_seconds"
        )
        aggregate_input_rgb_byte_lengths = unique_int_values(
            records, "input_rgb_byte_length"
        )
        aggregate_input_rgb_expected_byte_lengths = unique_int_values(
            records, "input_rgb_expected_byte_length"
        )
        aggregate_input_rgb_length_matches_expected_values = unique_bool_values(
            records, "input_rgb_length_matches_expected"
        )
        aggregate_input_ppm_widths = unique_int_values(records, "input_ppm_width")
        aggregate_input_ppm_heights = unique_int_values(records, "input_ppm_height")
        aggregate_input_ppm_byte_lengths = unique_int_values(
            records, "input_ppm_byte_length"
        )
        aggregate_input_ppm_rgb_bytes = unique_int_values(
            records, "input_ppm_rgb_bytes"
        )
        aggregate_input_ppm_packed_rgb_byte_lengths = unique_int_values(
            records, "input_ppm_packed_rgb_byte_length"
        )
        aggregate_input_ppm_packed_rgb_matches_input_values = unique_bool_values(
            records, "input_ppm_packed_rgb_matches_input"
        )
        aggregate_input_ppm_non_flat_values = unique_bool_values(
            records, "input_ppm_non_flat"
        )
        aggregate_input_ppm_has_color_pixels_values = unique_bool_values(
            records, "input_ppm_has_color_pixels"
        )
        aggregate_jpeg_byte_lengths = unique_int_values(records, "jpeg_byte_length")
        aggregate_jpeg_mcu_counts = unique_int_values(records, "jpeg_mcu_count")
        aggregate_jpeg_component_counts = unique_int_values(
            records, "jpeg_component_count"
        )
        aggregate_jpeg_scan_data_bytes = unique_int_values(
            records, "jpeg_scan_data_bytes"
        )
        aggregate_jpeg_stuffed_ff_bytes = unique_int_values(
            records, "jpeg_stuffed_ff_bytes"
        )
        aggregate_jpeg_restart_intervals = unique_int_values(
            records, "jpeg_restart_interval"
        )
        aggregate_jpeg_restart_markers = unique_int_values(
            records, "jpeg_restart_markers"
        )
        aggregate_jpeg_chroma_modes = unique_scalar_string_values(
            records, "jpeg_chroma_mode"
        )
        aggregate_jpeg_marker_names = unique_string_values(
            records, "jpeg_marker_sequence"
        )
        aggregate_jpeg_scan_data_sha256_values = unique_scalar_string_values(
            records, "jpeg_scan_data_sha256"
        )
        aggregate_jpeg_sha256_values = unique_scalar_string_values(
            records, "jpeg_sha256"
        )
        aggregate_decoder_passed_values = unique_bool_values(
            records, "decoder_passed"
        )
        aggregate_decoder_returncodes = unique_int_values(
            records, "decoder_returncode"
        )
        aggregate_decoder_timeout_seconds = unique_number_values(
            records, "decoder_timeout_seconds"
        )
        aggregate_decoder_elapsed_seconds = unique_number_values(
            records, "decoder_elapsed_seconds"
        )
        aggregate_decoder_stdout_chars = unique_int_values(
            records, "decoder_stdout_chars"
        )
        aggregate_decoder_stderr_chars = unique_int_values(
            records, "decoder_stderr_chars"
        )
        aggregate_decoder_stdout_truncated_values = unique_bool_values(
            records, "decoder_stdout_truncated"
        )
        aggregate_decoder_stderr_truncated_values = unique_bool_values(
            records, "decoder_stderr_truncated"
        )
        aggregate_jpeg_paths = unique_scalar_string_values(records, "jpeg")
        aggregate_jpeg_paths_resolved = unique_scalar_string_values(
            records, "jpeg_resolved"
        )
        aggregate_input_rgb_paths = unique_scalar_string_values(records, "input_rgb")
        aggregate_input_rgb_paths_resolved = unique_scalar_string_values(
            records, "input_rgb_resolved"
        )
        aggregate_input_ppm_paths = unique_scalar_string_values(records, "input_ppm")
        aggregate_input_ppm_paths_resolved = unique_scalar_string_values(
            records, "input_ppm_resolved"
        )
        aggregate_decoder_commands = unique_scalar_string_values(
            records, "decoder_command"
        )
        vivado_passed_count = sum(
            1 for record in vivado_records if record.get("passed") is True
        )
        vivado_failed_count = len(vivado_records) - vivado_passed_count
        vivado_evidence_paths = [str(path) for path in args.vivado_evidence]
        vivado_evidence_paths_resolved = [
            str(path.resolve(strict=False)) for path in args.vivado_evidence
        ]
        vivado_evidence_passed_paths = [
            str(record.get("path"))
            for record in vivado_records
            if record.get("passed") is True
        ]
        vivado_evidence_passed_paths_resolved = [
            str(record.get("path_resolved"))
            for record in vivado_records
            if record.get("passed") is True
        ]
        vivado_evidence_failed_paths = [
            str(record.get("path"))
            for record in vivado_records
            if record.get("passed") is not True
        ]
        vivado_evidence_failed_paths_resolved = [
            str(record.get("path_resolved"))
            for record in vivado_records
            if record.get("passed") is not True
        ]
        if args.json:
            print(
                strict_json_dumps(
                    {
                        "passed": not failures,
                        "checked_count": len(records),
                        "passed_count": passed_count,
                        "failed_count": failed_count,
                        "failure_count": len(failures),
                        "checked_paths": checked_paths,
                        "passed_paths": passed_paths,
                        "failed_paths": failed_paths,
                        "aggregate_evidence_group_count": (
                            aggregate_evidence_group_count
                        ),
                        "aggregate_evidence_present_count": (
                            aggregate_evidence_present_count
                        ),
                        "aggregate_evidence_missing_count": (
                            aggregate_evidence_missing_count
                        ),
                        "aggregate_recorded_check_count": (
                            aggregate_recorded_check_count
                        ),
                        "aggregate_passing_check_count": (
                            aggregate_passing_check_count
                        ),
                        "aggregate_failing_check_count": (
                            aggregate_failing_check_count
                        ),
                        "summary_checked_count": len(summary_checked_paths),
                        "summary_match_count": len(summary_matched_paths),
                        "summary_mismatch_count": len(summary_mismatched_paths),
                        "summary_all_checked": summary_all_checked,
                        "summary_all_matched": summary_all_matched,
                        "summary_checked_paths": summary_checked_paths,
                        "summary_matched_paths": summary_matched_paths,
                        "summary_mismatched_paths": summary_mismatched_paths,
                        "aggregate_present_evidence": aggregate_present_evidence,
                        "aggregate_missing_evidence": aggregate_missing_evidence,
                        "aggregate_passing_checks": aggregate_passing_checks,
                        "aggregate_failing_checks": aggregate_failing_checks,
                        "aggregate_stream_tx_device_count": len(
                            aggregate_stream_tx_devices
                        ),
                        "aggregate_stream_rx_device_count": len(
                            aggregate_stream_rx_devices
                        ),
                        "aggregate_stream_tx_device_resolved_count": len(
                            aggregate_stream_tx_device_resolved
                        ),
                        "aggregate_stream_rx_device_resolved_count": len(
                            aggregate_stream_rx_device_resolved
                        ),
                        "aggregate_stream_tx_devices": aggregate_stream_tx_devices,
                        "aggregate_stream_rx_devices": aggregate_stream_rx_devices,
                        "aggregate_stream_tx_device_resolved": (
                            aggregate_stream_tx_device_resolved
                        ),
                        "aggregate_stream_rx_device_resolved": (
                            aggregate_stream_rx_device_resolved
                        ),
                        "aggregate_axi_lite_device_count": len(
                            aggregate_axi_lite_devices
                        ),
                        "aggregate_axi_lite_base_address_count": len(
                            aggregate_axi_lite_base_addresses
                        ),
                        "aggregate_axi_lite_devices": aggregate_axi_lite_devices,
                        "aggregate_axi_lite_base_addresses": (
                            aggregate_axi_lite_base_addresses
                        ),
                        "aggregate_axi_lite_base_addresses_hex": (
                            aggregate_axi_lite_base_addresses_hex
                        ),
                        "aggregate_frame_width_count": len(aggregate_frame_widths),
                        "aggregate_frame_height_count": len(aggregate_frame_heights),
                        "aggregate_encoder_width_count": len(
                            aggregate_encoder_widths
                        ),
                        "aggregate_encoder_height_count": len(
                            aggregate_encoder_heights
                        ),
                        "aggregate_encoder_max_width_count": len(
                            aggregate_encoder_max_widths
                        ),
                        "aggregate_encoder_max_height_count": len(
                            aggregate_encoder_max_heights
                        ),
                        "aggregate_encoder_quality_count": len(
                            aggregate_encoder_qualities
                        ),
                        "aggregate_encoder_restart_interval_count": len(
                            aggregate_encoder_restart_intervals
                        ),
                        "aggregate_encoder_control_count": len(
                            aggregate_encoder_controls
                        ),
                        "aggregate_encoder_control_hex_count": len(
                            aggregate_encoder_control_hex_values
                        ),
                        "aggregate_encoder_chroma_subsample_count": len(
                            aggregate_encoder_chroma_subsample_values
                        ),
                        "aggregate_encoder_emit_jfif_count": len(
                            aggregate_encoder_emit_jfif_values
                        ),
                        "aggregate_encoder_clear_error_count": len(
                            aggregate_encoder_clear_error_values
                        ),
                        "aggregate_validation_width_count": len(
                            aggregate_validation_widths
                        ),
                        "aggregate_validation_height_count": len(
                            aggregate_validation_heights
                        ),
                        "aggregate_validation_restart_interval_count": len(
                            aggregate_validation_restart_intervals
                        ),
                        "aggregate_validation_expected_restart_marker_count": len(
                            aggregate_validation_expected_restart_markers
                        ),
                        "aggregate_validation_quality_count": len(
                            aggregate_validation_qualities
                        ),
                        "aggregate_validation_check_chroma_mode_count": len(
                            aggregate_validation_check_chroma_mode_values
                        ),
                        "aggregate_validation_chroma_subsample_count": len(
                            aggregate_validation_chroma_subsample_values
                        ),
                        "aggregate_validation_require_standard_huffman_count": len(
                            aggregate_validation_require_standard_huffman_values
                        ),
                        "aggregate_validation_expected_chroma_mode_count": len(
                            aggregate_validation_expected_chroma_modes
                        ),
                        "aggregate_validation_expect_jfif_count": len(
                            aggregate_validation_expect_jfif_values
                        ),
                        "aggregate_status_check_count_value_count": len(
                            aggregate_status_check_counts
                        ),
                        "aggregate_status_check_context_count": len(
                            aggregate_status_check_contexts
                        ),
                        "aggregate_expected_status_check_context_count": len(
                            aggregate_expected_status_check_contexts
                        ),
                        "aggregate_status_check_contexts_match_expected_count": len(
                            aggregate_status_check_contexts_match_expected_values
                        ),
                        "aggregate_status_checks_all_idle_count": len(
                            aggregate_status_checks_all_idle_values
                        ),
                        "aggregate_status_checks_any_protocol_error_count": len(
                            aggregate_status_checks_any_protocol_error_values
                        ),
                        "aggregate_status_checks_any_busy_count": len(
                            aggregate_status_checks_any_busy_values
                        ),
                        "aggregate_transfer_elapsed_seconds_count": len(
                            aggregate_transfer_elapsed_seconds
                        ),
                        "aggregate_host_input_rgb_bytes_per_second_count": len(
                            aggregate_host_input_rgb_bytes_per_second
                        ),
                        "aggregate_host_output_jpeg_bytes_per_second_count": len(
                            aggregate_host_output_jpeg_bytes_per_second
                        ),
                        "aggregate_capture_max_output_byte_count": len(
                            aggregate_capture_max_output_bytes
                        ),
                        "aggregate_capture_timeout_second_count": len(
                            aggregate_capture_timeout_seconds
                        ),
                        "aggregate_input_rgb_byte_length_count": len(
                            aggregate_input_rgb_byte_lengths
                        ),
                        "aggregate_input_rgb_expected_byte_length_count": len(
                            aggregate_input_rgb_expected_byte_lengths
                        ),
                        "aggregate_input_rgb_length_matches_expected_count": len(
                            aggregate_input_rgb_length_matches_expected_values
                        ),
                        "aggregate_input_ppm_width_count": len(
                            aggregate_input_ppm_widths
                        ),
                        "aggregate_input_ppm_height_count": len(
                            aggregate_input_ppm_heights
                        ),
                        "aggregate_input_ppm_byte_length_count": len(
                            aggregate_input_ppm_byte_lengths
                        ),
                        "aggregate_input_ppm_rgb_byte_count": len(
                            aggregate_input_ppm_rgb_bytes
                        ),
                        "aggregate_input_ppm_packed_rgb_byte_length_count": len(
                            aggregate_input_ppm_packed_rgb_byte_lengths
                        ),
                        "aggregate_input_ppm_packed_rgb_matches_input_count": len(
                            aggregate_input_ppm_packed_rgb_matches_input_values
                        ),
                        "aggregate_input_ppm_non_flat_count": len(
                            aggregate_input_ppm_non_flat_values
                        ),
                        "aggregate_input_ppm_has_color_pixels_count": len(
                            aggregate_input_ppm_has_color_pixels_values
                        ),
                        "aggregate_jpeg_byte_length_count": len(
                            aggregate_jpeg_byte_lengths
                        ),
                        "aggregate_jpeg_mcu_count_count": len(
                            aggregate_jpeg_mcu_counts
                        ),
                        "aggregate_jpeg_component_count_count": len(
                            aggregate_jpeg_component_counts
                        ),
                        "aggregate_jpeg_scan_data_byte_count": len(
                            aggregate_jpeg_scan_data_bytes
                        ),
                        "aggregate_jpeg_stuffed_ff_byte_count": len(
                            aggregate_jpeg_stuffed_ff_bytes
                        ),
                        "aggregate_jpeg_restart_interval_count": len(
                            aggregate_jpeg_restart_intervals
                        ),
                        "aggregate_jpeg_restart_marker_count": len(
                            aggregate_jpeg_restart_markers
                        ),
                        "aggregate_jpeg_chroma_mode_count": len(
                            aggregate_jpeg_chroma_modes
                        ),
                        "aggregate_jpeg_marker_name_count": len(
                            aggregate_jpeg_marker_names
                        ),
                        "aggregate_jpeg_scan_data_sha256_count": len(
                            aggregate_jpeg_scan_data_sha256_values
                        ),
                        "aggregate_jpeg_sha256_count": len(
                            aggregate_jpeg_sha256_values
                        ),
                        "aggregate_decoder_passed_value_count": len(
                            aggregate_decoder_passed_values
                        ),
                        "aggregate_decoder_returncode_count": len(
                            aggregate_decoder_returncodes
                        ),
                        "aggregate_decoder_timeout_second_count": len(
                            aggregate_decoder_timeout_seconds
                        ),
                        "aggregate_decoder_elapsed_second_count": len(
                            aggregate_decoder_elapsed_seconds
                        ),
                        "aggregate_decoder_stdout_char_count": len(
                            aggregate_decoder_stdout_chars
                        ),
                        "aggregate_decoder_stderr_char_count": len(
                            aggregate_decoder_stderr_chars
                        ),
                        "aggregate_decoder_stdout_truncated_count": len(
                            aggregate_decoder_stdout_truncated_values
                        ),
                        "aggregate_decoder_stderr_truncated_count": len(
                            aggregate_decoder_stderr_truncated_values
                        ),
                        "aggregate_frame_widths": aggregate_frame_widths,
                        "aggregate_frame_heights": aggregate_frame_heights,
                        "aggregate_encoder_widths": aggregate_encoder_widths,
                        "aggregate_encoder_heights": aggregate_encoder_heights,
                        "aggregate_encoder_max_widths": (
                            aggregate_encoder_max_widths
                        ),
                        "aggregate_encoder_max_heights": (
                            aggregate_encoder_max_heights
                        ),
                        "aggregate_encoder_qualities": aggregate_encoder_qualities,
                        "aggregate_encoder_restart_intervals": (
                            aggregate_encoder_restart_intervals
                        ),
                        "aggregate_encoder_controls": aggregate_encoder_controls,
                        "aggregate_encoder_control_hex_values": (
                            aggregate_encoder_control_hex_values
                        ),
                        "aggregate_encoder_chroma_subsample_values": (
                            aggregate_encoder_chroma_subsample_values
                        ),
                        "aggregate_encoder_emit_jfif_values": (
                            aggregate_encoder_emit_jfif_values
                        ),
                        "aggregate_encoder_clear_error_values": (
                            aggregate_encoder_clear_error_values
                        ),
                        "aggregate_validation_widths": aggregate_validation_widths,
                        "aggregate_validation_heights": aggregate_validation_heights,
                        "aggregate_validation_restart_intervals": (
                            aggregate_validation_restart_intervals
                        ),
                        "aggregate_validation_expected_restart_markers": (
                            aggregate_validation_expected_restart_markers
                        ),
                        "aggregate_validation_qualities": (
                            aggregate_validation_qualities
                        ),
                        "aggregate_validation_check_chroma_mode_values": (
                            aggregate_validation_check_chroma_mode_values
                        ),
                        "aggregate_validation_chroma_subsample_values": (
                            aggregate_validation_chroma_subsample_values
                        ),
                        "aggregate_validation_require_standard_huffman_values": (
                            aggregate_validation_require_standard_huffman_values
                        ),
                        "aggregate_validation_expected_chroma_modes": (
                            aggregate_validation_expected_chroma_modes
                        ),
                        "aggregate_validation_expect_jfif_values": (
                            aggregate_validation_expect_jfif_values
                        ),
                        "aggregate_status_check_counts": aggregate_status_check_counts,
                        "aggregate_status_check_contexts": (
                            aggregate_status_check_contexts
                        ),
                        "aggregate_expected_status_check_contexts": (
                            aggregate_expected_status_check_contexts
                        ),
                        "aggregate_status_check_contexts_match_expected_values": (
                            aggregate_status_check_contexts_match_expected_values
                        ),
                        "aggregate_status_checks_all_idle_values": (
                            aggregate_status_checks_all_idle_values
                        ),
                        "aggregate_status_checks_any_protocol_error_values": (
                            aggregate_status_checks_any_protocol_error_values
                        ),
                        "aggregate_status_checks_any_busy_values": (
                            aggregate_status_checks_any_busy_values
                        ),
                        "aggregate_transfer_elapsed_seconds": (
                            aggregate_transfer_elapsed_seconds
                        ),
                        "aggregate_host_input_rgb_bytes_per_second": (
                            aggregate_host_input_rgb_bytes_per_second
                        ),
                        "aggregate_host_output_jpeg_bytes_per_second": (
                            aggregate_host_output_jpeg_bytes_per_second
                        ),
                        "aggregate_capture_max_output_bytes": (
                            aggregate_capture_max_output_bytes
                        ),
                        "aggregate_capture_timeout_seconds": (
                            aggregate_capture_timeout_seconds
                        ),
                        "aggregate_input_rgb_byte_lengths": (
                            aggregate_input_rgb_byte_lengths
                        ),
                        "aggregate_input_rgb_expected_byte_lengths": (
                            aggregate_input_rgb_expected_byte_lengths
                        ),
                        "aggregate_input_rgb_length_matches_expected_values": (
                            aggregate_input_rgb_length_matches_expected_values
                        ),
                        "aggregate_input_ppm_widths": aggregate_input_ppm_widths,
                        "aggregate_input_ppm_heights": aggregate_input_ppm_heights,
                        "aggregate_input_ppm_byte_lengths": (
                            aggregate_input_ppm_byte_lengths
                        ),
                        "aggregate_input_ppm_rgb_bytes": aggregate_input_ppm_rgb_bytes,
                        "aggregate_input_ppm_packed_rgb_byte_lengths": (
                            aggregate_input_ppm_packed_rgb_byte_lengths
                        ),
                        "aggregate_input_ppm_packed_rgb_matches_input_values": (
                            aggregate_input_ppm_packed_rgb_matches_input_values
                        ),
                        "aggregate_input_ppm_non_flat_values": (
                            aggregate_input_ppm_non_flat_values
                        ),
                        "aggregate_input_ppm_has_color_pixels_values": (
                            aggregate_input_ppm_has_color_pixels_values
                        ),
                        "aggregate_jpeg_byte_lengths": aggregate_jpeg_byte_lengths,
                        "aggregate_jpeg_mcu_counts": aggregate_jpeg_mcu_counts,
                        "aggregate_jpeg_component_counts": (
                            aggregate_jpeg_component_counts
                        ),
                        "aggregate_jpeg_scan_data_bytes": (
                            aggregate_jpeg_scan_data_bytes
                        ),
                        "aggregate_jpeg_stuffed_ff_bytes": (
                            aggregate_jpeg_stuffed_ff_bytes
                        ),
                        "aggregate_jpeg_restart_intervals": (
                            aggregate_jpeg_restart_intervals
                        ),
                        "aggregate_jpeg_restart_markers": (
                            aggregate_jpeg_restart_markers
                        ),
                        "aggregate_jpeg_chroma_modes": aggregate_jpeg_chroma_modes,
                        "aggregate_jpeg_marker_names": aggregate_jpeg_marker_names,
                        "aggregate_jpeg_scan_data_sha256_values": (
                            aggregate_jpeg_scan_data_sha256_values
                        ),
                        "aggregate_jpeg_sha256_values": aggregate_jpeg_sha256_values,
                        "aggregate_decoder_passed_values": (
                            aggregate_decoder_passed_values
                        ),
                        "aggregate_decoder_returncodes": aggregate_decoder_returncodes,
                        "aggregate_decoder_timeout_seconds": (
                            aggregate_decoder_timeout_seconds
                        ),
                        "aggregate_decoder_elapsed_seconds": (
                            aggregate_decoder_elapsed_seconds
                        ),
                        "aggregate_decoder_stdout_chars": (
                            aggregate_decoder_stdout_chars
                        ),
                        "aggregate_decoder_stderr_chars": (
                            aggregate_decoder_stderr_chars
                        ),
                        "aggregate_decoder_stdout_truncated_values": (
                            aggregate_decoder_stdout_truncated_values
                        ),
                        "aggregate_decoder_stderr_truncated_values": (
                            aggregate_decoder_stderr_truncated_values
                        ),
                        "aggregate_jpeg_path_count": len(aggregate_jpeg_paths),
                        "aggregate_jpeg_path_resolved_count": len(
                            aggregate_jpeg_paths_resolved
                        ),
                        "aggregate_input_rgb_path_count": len(
                            aggregate_input_rgb_paths
                        ),
                        "aggregate_input_rgb_path_resolved_count": len(
                            aggregate_input_rgb_paths_resolved
                        ),
                        "aggregate_input_ppm_path_count": len(
                            aggregate_input_ppm_paths
                        ),
                        "aggregate_input_ppm_path_resolved_count": len(
                            aggregate_input_ppm_paths_resolved
                        ),
                        "aggregate_decoder_command_count": len(
                            aggregate_decoder_commands
                        ),
                        "aggregate_jpeg_paths": aggregate_jpeg_paths,
                        "aggregate_jpeg_paths_resolved": (
                            aggregate_jpeg_paths_resolved
                        ),
                        "aggregate_input_rgb_paths": aggregate_input_rgb_paths,
                        "aggregate_input_rgb_paths_resolved": (
                            aggregate_input_rgb_paths_resolved
                        ),
                        "aggregate_input_ppm_paths": aggregate_input_ppm_paths,
                        "aggregate_input_ppm_paths_resolved": (
                            aggregate_input_ppm_paths_resolved
                        ),
                        "aggregate_decoder_commands": aggregate_decoder_commands,
                        "vivado_evidence_checked_count": len(vivado_records),
                        "vivado_evidence_passed_count": vivado_passed_count,
                        "vivado_evidence_failed_count": vivado_failed_count,
                        "vivado_evidence_paths": vivado_evidence_paths,
                        "vivado_evidence_paths_resolved": (
                            vivado_evidence_paths_resolved
                        ),
                        "vivado_evidence_passed_paths": (
                            vivado_evidence_passed_paths
                        ),
                        "vivado_evidence_passed_paths_resolved": (
                            vivado_evidence_passed_paths_resolved
                        ),
                        "vivado_evidence_failed_paths": (
                            vivado_evidence_failed_paths
                        ),
                        "vivado_evidence_failed_paths_resolved": (
                            vivado_evidence_failed_paths_resolved
                        ),
                        "vivado_hjpeg_base_addresses": list(
                            vivado_hjpeg_base_addresses
                        ),
                        "vivado_hjpeg_base_address_count": len(
                            vivado_hjpeg_base_addresses
                        ),
                        "vivado_hjpeg_base_addresses_consistent": (
                            vivado_hjpeg_base_addresses_consistent
                        ),
                        "vivado_hjpeg_base_addresses_hex": [
                            f"0x{base:x}" for base in vivado_hjpeg_base_addresses
                        ],
                        "vivado_evidence": vivado_records,
                        "records": records,
                        "failures": failures,
                    },
                    sort_keys=True,
                )
            )
        else:
            for failure in failures:
                print(f"FAIL: {failure}", file=sys.stderr)
            if not failures:
                print(f"PASS: checked {len(records)} hardware run evidence file(s)")
        return 0 if not failures else 1

    if args.command == "config":
        with AxiLiteWindow(args.dev, args.base_addr) as regs:
            configure_registers(
                regs=regs,
                width=args.width,
                height=args.height,
                quality=args.quality,
                restart_interval=args.restart_interval,
                chroma_subsample=args.chroma_subsample,
                emit_jfif=not args.no_jfif,
                clear_error=args.clear_error,
                max_width=args.max_width,
                max_height=args.max_height,
            )
        if args.json:
            print(
                strict_json_dumps(
                    {
                        "axi_lite": axi_lite_target_record(args.dev, args.base_addr),
                        "encoder_config": encoder_config_record(
                            width=args.width,
                            height=args.height,
                            quality=args.quality,
                            restart_interval=args.restart_interval,
                            chroma_subsample=args.chroma_subsample,
                            emit_jfif=not args.no_jfif,
                            clear_error=args.clear_error,
                            max_width=args.max_width,
                            max_height=args.max_height,
                        ),
                    },
                    sort_keys=True,
                )
            )
            return 0
        print(f"configured hjpeg at 0x{args.base_addr:x} for {args.width}x{args.height}")
        return 0

    if args.command == "status":
        with AxiLiteWindow(args.dev, args.base_addr) as regs:
            status = regs.read32(REG_STATUS)
        if args.json:
            print(
                strict_json_dumps(
                    status_evidence_record(args.dev, args.base_addr, status),
                    sort_keys=True,
                )
            )
            return 0
        print(f"0x{status:08x} {status_text(status)}")
        return 0

    if args.command == "clear-error":
        with AxiLiteWindow(args.dev, args.base_addr) as regs:
            control = clear_protocol_error(regs)
        if args.json:
            print(
                strict_json_dumps(
                    clear_error_record(args.dev, args.base_addr, control),
                    sort_keys=True,
                )
            )
            return 0
        print(f"cleared hjpeg protocol error at 0x{args.base_addr:x}")
        return 0

    if args.command == "run-stream-devices":
        require_stream_devices(args.tx_device, args.rx_device)
        status_checks: list[dict[str, object]] = []

        def record_status(context: str, status: int) -> None:
            record = status_evidence_record(args.dev, args.base_addr, status)
            record["context"] = context
            status_checks.append(record)

        def configure() -> None:
            with AxiLiteWindow(args.dev, args.base_addr) as regs:
                configure_registers(
                    regs=regs,
                    width=args.width,
                    height=args.height,
                    quality=args.quality,
                    restart_interval=args.restart_interval,
                    chroma_subsample=args.chroma_subsample,
                    emit_jfif=not args.no_jfif,
                    clear_error=args.clear_error,
                    max_width=args.max_width,
                    max_height=args.max_height,
                )
                status = regs.read32(REG_STATUS)
                require_idle_status_value(status, "after configuration")
                record_status("after configuration", status)

        def check_status(context: str) -> None:
            with AxiLiteWindow(args.dev, args.base_addr) as regs:
                status = regs.read32(REG_STATUS)
                require_idle_status_value(status, context)
                record_status(context, status)

        decoder_results: list[DecoderCommandResult] = []
        transfer_elapsed_seconds: list[float] = []
        input_ppm_record = None
        if args.input_ppm is not None:
            input_ppm_record = run_input_ppm_record(
                args.input_ppm,
                args.width,
                args.height,
                args.input_rgb.read_bytes(),
                args.max_width,
                args.max_height,
            )
        info, input_info = run_stream_devices(
            input_rgb=args.input_rgb,
            output_jpeg=args.output_jpeg,
            tx_device=args.tx_device,
            rx_device=args.rx_device,
            max_output_bytes=args.max_output_bytes,
            expected_width=args.width,
            expected_height=args.height,
            expected_restart_interval=args.restart_interval,
            expected_chroma_subsample=args.chroma_subsample,
            expected_emit_jfif=not args.no_jfif,
            quality=args.quality,
            max_width=args.max_width,
            max_height=args.max_height,
            timeout_seconds=args.timeout_seconds,
            configure=configure,
            check_status=check_status,
            decoder_command=args.decoder_command,
            decoder_timeout_seconds=args.decoder_timeout_seconds,
            decoder_results=decoder_results,
            transfer_elapsed_seconds=transfer_elapsed_seconds,
        )
        decoder_passed = True if args.decoder_command is not None else None
        decoder_timeout = (
            args.decoder_timeout_seconds if args.decoder_command is not None else None
        )
        decoder_result = decoder_results[0] if decoder_results else None
        transfer_elapsed = (
            transfer_elapsed_seconds[0] if transfer_elapsed_seconds else None
        )
        record = run_evidence_record(
            args.output_jpeg,
            info,
            input_info,
            args.width * args.height * 4,
            axi_lite_target_record(args.dev, args.base_addr),
            encoder_config_record(
                width=args.width,
                height=args.height,
                quality=args.quality,
                restart_interval=args.restart_interval,
                chroma_subsample=args.chroma_subsample,
                emit_jfif=not args.no_jfif,
                clear_error=args.clear_error,
                max_width=args.max_width,
                max_height=args.max_height,
            ),
            capture_config_record(args.max_output_bytes, args.timeout_seconds),
            status_checks,
            decoder_passed,
            args.decoder_command,
            decoder_timeout,
            decoder_result,
            validation_expectations_record(
                info=info,
                width=args.width,
                height=args.height,
                restart_interval=args.restart_interval,
                check_chroma_mode=True,
                chroma_subsample=args.chroma_subsample,
                expect_jfif="absent" if args.no_jfif else "present",
                quality=args.quality,
                require_standard_huffman=True,
            ),
            transfer_elapsed,
            input_ppm=input_ppm_record,
            stream_devices=stream_devices_record(args.tx_device, args.rx_device),
        )
        record["arguments"] = run_stream_devices_arguments_record(args)
        complete_evidence = bool(
            record["hardware_run_summary"]["complete_hardware_run_evidence"]
        )
        missing_complete_evidence = [
            name
            for name, present in record["hardware_run_summary"][
                "evidence_present"
            ].items()
            if not present
        ]
        failing_complete_checks = list(
            record["hardware_run_summary"]["failing_checks"]
        )
        record["complete_hardware_run_evidence_required"] = (
            args.require_complete_evidence
        )
        record["complete_hardware_run_evidence"] = complete_evidence
        record["complete_hardware_run_evidence_missing"] = (
            missing_complete_evidence
        )
        record["complete_hardware_run_evidence_failing_checks"] = (
            failing_complete_checks
        )
        if args.json:
            print(
                strict_json_dumps(
                    record,
                    sort_keys=True,
                )
            )
            return 0 if complete_evidence or not args.require_complete_evidence else 1
        if args.require_complete_evidence and not complete_evidence:
            print(
                "complete hardware run evidence missing: "
                + ", ".join(missing_complete_evidence),
                file=sys.stderr,
            )
            return 1
        decoder_text = " decoder=pass" if args.decoder_command is not None else ""
        print(
            f"captured validated JPEG to {args.output_jpeg}: "
            f"dimensions={info.width}x{info.height} "
            f"scan_data_bytes={info.scan_data_bytes} "
            f"scan_data_sha256={info.scan_data_sha256} "
            f"stuffed_ff_bytes={info.stuffed_ff_bytes} "
            f"byte_length={info.byte_length} "
            f"sha256={info.sha256} "
            f"input_rgb_bytes={input_info.byte_length} "
            f"input_rgb_sha256={input_info.sha256}{decoder_text}"
        )
        return 0

    raise AssertionError(f"unhandled command {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(1)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
