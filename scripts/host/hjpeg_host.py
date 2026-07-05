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
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable


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
    byte_length: int
    sha256: str


@dataclass(frozen=True)
class DecoderCommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    stdout_chars: int
    stderr_chars: int
    output_capture_chars: int
    stdout_truncated: bool
    stderr_truncated: bool


def make_test_image(width: int, height: int) -> PpmImage:
    if width <= 0 or height <= 0:
        raise ValueError("PPM dimensions must be positive")

    rgb = bytearray()
    denom_x = max(width - 1, 1)
    denom_y = max(height - 1, 1)
    for y in range(height):
        for x in range(width):
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


def write_rgb_stream(image: PpmImage, output: Path) -> None:
    stream = bytearray()
    for offset in range(0, len(image.rgb), 3):
        stream.extend(image.rgb[offset : offset + 3])
        stream.append(0)
    output.write_bytes(bytes(stream))


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
    expected_restart_markers = (
        jpeg_mcu_count(info) - 1
    ) // expected_restart_interval
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
        completed = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
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
) -> dict[str, object]:
    record: dict[str, object] = {
        "jpeg": str(jpeg),
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
        record["decoder_stdout_chars"] = decoder_result.stdout_chars
        record["decoder_stderr_chars"] = decoder_result.stderr_chars
        record["decoder_output_capture_chars"] = decoder_result.output_capture_chars
        record["decoder_stdout_truncated"] = decoder_result.stdout_truncated
        record["decoder_stderr_truncated"] = decoder_result.stderr_truncated
    return record


def file_info(path: Path, data: bytes) -> FileInfo:
    return FileInfo(
        path=str(path),
        byte_length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def file_info_record(info: FileInfo) -> dict[str, object]:
    return {
        "path": info.path,
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
    transfer_elapsed_seconds: float | None = None,
) -> dict[str, object]:
    record = jpeg_info_record(
        jpeg,
        info,
        decoder_passed,
        decoder_command,
        decoder_timeout_seconds,
        decoder_result,
    )
    if input_info is not None:
        input_record: dict[str, object] = {
            "path": input_info.path,
            "byte_length": input_info.byte_length,
            "sha256": input_info.sha256,
        }
        if expected_input_rgb_bytes is not None:
            input_record["expected_byte_length"] = expected_input_rgb_bytes
        record["input_rgb"] = input_record
    if axi_lite is not None:
        record["axi_lite"] = axi_lite
    if encoder_config is not None:
        record["encoder_config"] = encoder_config
    if capture_config is not None:
        record["capture_config"] = capture_config
    if status_checks is not None:
        record["status_checks"] = status_checks
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
    return record


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
    output_jpeg.write_bytes(jpeg)
    info = validate_jpeg(
        output_jpeg,
        expected_width,
        expected_height,
        expected_restart_interval=expected_restart_interval,
        expected_chroma_subsample=expected_chroma_subsample,
        expected_emit_jfif=expected_emit_jfif,
        expected_quality=quality,
        require_standard_huffman=True,
    )
    if decoder_command is not None:
        decoder_result = run_decoder_command(
            output_jpeg,
            decoder_command,
            decoder_timeout_seconds,
        )
        if decoder_results is not None:
            decoder_results.append(decoder_result)
    if check_status is not None:
        check_status("after transfer")
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


def _positive_int(value: str) -> int:
    parsed = int(value, 0)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value, 0)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be finite and positive")
    return parsed


def _quality_value(value: str) -> int:
    parsed = int(value, 0)
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("value must be in 1..100")
    return parsed


def _restart_interval_value(value: str) -> int:
    parsed = int(value, 0)
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
                json.dumps(
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
        image = make_test_image(args.width, args.height)
        write_ppm(image, args.output)
        if args.json:
            print(
                json.dumps(
                    {
                        "output_ppm": ppm_evidence_record(args.output, image),
                        "deterministic_pattern": True,
                        "max_width": args.max_width,
                        "max_height": args.max_height,
                    },
                    sort_keys=True,
                )
            )
            return 0
        print(f"wrote deterministic P6 PPM {args.width}x{args.height} to {args.output}")
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
                json.dumps(
                    jpeg_info_record(
                        args.jpeg,
                        info,
                        decoder_passed,
                        args.decoder_command,
                        decoder_timeout,
                        decoder_result,
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
                json.dumps(
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
                json.dumps(
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
                json.dumps(
                    clear_error_record(args.dev, args.base_addr, control),
                    sort_keys=True,
                )
            )
            return 0
        print(f"cleared hjpeg protocol error at 0x{args.base_addr:x}")
        return 0

    if args.command == "run-stream-devices":
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
        if args.json:
            print(
                json.dumps(
                    run_evidence_record(
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
                        transfer_elapsed,
                    ),
                    sort_keys=True,
                )
            )
            return 0
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
