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
import mmap
import os
import shlex
import struct
import subprocess
import sys
import threading
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


@dataclass(frozen=True)
class JpegScanComponent:
    component_id: int
    dc_table: int
    ac_table: int


@dataclass(frozen=True)
class JpegInfo:
    width: int
    height: int
    sample_precision: int
    components: tuple[JpegComponent, ...]
    scan_components: tuple[JpegScanComponent, ...]
    spectral_start: int
    spectral_end: int
    successive_approximation: int
    quantization_tables: tuple[int, ...]
    huffman_tables: tuple[JpegHuffmanTable, ...]
    scan_data_bytes: int
    byte_length: int
    sha256: str
    app0_segments: int
    jfif_app0_segments: int
    dqt_segments: int
    dht_segments: int
    dri_segments: int
    restart_interval: int | None
    restart_markers: int


@dataclass(frozen=True)
class FileInfo:
    path: str
    byte_length: int
    sha256: str


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


def read_ppm(path: Path) -> PpmImage:
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
    huffman_tables: set[tuple[int, int]] = set()
    scan_data_bytes = 0
    app0_segments = 0
    jfif_app0_segments = 0
    dqt_segments = 0
    dht_segments = 0
    dri_segments = 0
    restart_interval: int | None = None
    restart_markers = 0
    saw_sos = False
    saw_eoi = False
    while offset < len(data):
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break

        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            saw_eoi = True
            break
        if 0xD0 <= marker <= 0xD7:
            restart_markers += 1
            continue
        if offset + 2 > len(data):
            break

        segment_length = _read_be16(data, offset)
        if segment_length < 2 or offset + segment_length > len(data):
            raise ValueError(f"invalid JPEG segment length at byte {offset - 1}")

        if marker == 0xE0:
            app0_segments += 1
            if data[offset + 2 : offset + 7] == b"JFIF\x00":
                jfif_app0_segments += 1
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
                table_bytes = 64 * (2 if precision else 1)
                table_offset += 1 + table_bytes
                if table_offset > segment_end:
                    raise ValueError("DQT segment table overruns segment length")
                quantization_tables.add(table_id)
        if marker == 0xDD:
            if segment_length != 4:
                raise ValueError("DRI segment has invalid length")
            dri_segments += 1
            restart_interval = _read_be16(data, offset + 2)
        if marker == 0xC0:
            if segment_length < 8:
                raise ValueError("SOF0 segment is too short")
            sample_precision = data[offset + 2]
            height = _read_be16(data, offset + 3)
            width = _read_be16(data, offset + 5)
            component_count = data[offset + 7]
            if segment_length != 8 + component_count * 3:
                raise ValueError("SOF0 segment length does not match component count")
            parsed_components = []
            for component_index in range(component_count):
                component_offset = offset + 8 + component_index * 3
                sampling = data[component_offset + 1]
                parsed_components.append(
                    JpegComponent(
                        component_id=data[component_offset],
                        horizontal_sampling=(sampling >> 4) & 0x0F,
                        vertical_sampling=sampling & 0x0F,
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
                value_count = sum(data[table_offset + 1 : table_offset + 17])
                table_offset += 17 + value_count
                if table_offset > segment_end:
                    raise ValueError("DHT segment table overruns segment length")
                huffman_tables.add((table_class, table_id))

        if marker == 0xDA:
            saw_sos = True
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
                    scan_data_bytes += 1
                    offset += 1
                    continue

                following = data[offset + 1]
                if following == 0x00:
                    scan_data_bytes += 1
                    offset += 2
                elif following == 0xFF:
                    offset += 1
                elif 0xD0 <= following <= 0xD7:
                    restart_markers += 1
                    offset += 2
                else:
                    break
            continue

        offset += segment_length

    if not saw_eoi:
        raise ValueError("JPEG output does not contain EOI")
    if dimensions is None:
        raise ValueError("JPEG output does not contain a baseline SOF0 segment")
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
    if dqt_segments == 0:
        raise ValueError("JPEG output does not contain a DQT segment")
    if dht_segments == 0:
        raise ValueError("JPEG output does not contain a DHT segment")
    if not saw_sos:
        raise ValueError("JPEG output does not contain an SOS segment")
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
    return JpegInfo(
        width=dimensions[0],
        height=dimensions[1],
        sample_precision=sample_precision,
        components=components,
        scan_components=scan_components,
        spectral_start=spectral_start,
        spectral_end=spectral_end,
        successive_approximation=successive_approximation,
        quantization_tables=tuple(sorted(quantization_tables)),
        huffman_tables=tuple(
            JpegHuffmanTable(table_class=table_class, table_id=table_id)
            for table_class, table_id in sorted(huffman_tables)
        ),
        scan_data_bytes=scan_data_bytes,
        byte_length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        app0_segments=app0_segments,
        jfif_app0_segments=jfif_app0_segments,
        dqt_segments=dqt_segments,
        dht_segments=dht_segments,
        dri_segments=dri_segments,
        restart_interval=restart_interval,
        restart_markers=restart_markers,
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


def require_restart_interval(info: JpegInfo, expected_restart_interval: int) -> None:
    if expected_restart_interval < 0:
        raise ValueError("expected restart interval must be nonnegative")
    if expected_restart_interval == 0:
        if info.dri_segments != 0 or info.restart_interval is not None or info.restart_markers != 0:
            raise ValueError("JPEG contains restart markers or DRI, but restart interval 0 was expected")
        return
    if info.restart_interval != expected_restart_interval:
        actual = "none" if info.restart_interval is None else str(info.restart_interval)
        raise ValueError(
            f"JPEG restart interval is {actual}, expected {expected_restart_interval}"
        )


def validate_jpeg(
    path: Path,
    expected_width: int,
    expected_height: int,
    expected_restart_interval: int | None = None,
    expected_chroma_subsample: bool | None = None,
    expected_emit_jfif: bool | None = None,
) -> JpegInfo:
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


def run_decoder_command(jpeg: Path, command: str) -> None:
    argv = decoder_command_argv(jpeg, command)

    try:
        completed = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"decoder command not found: {argv[0]}") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"decoder command failed with exit code {completed.returncode}{suffix}")


def jpeg_info_record(
    jpeg: Path,
    info: JpegInfo,
    decoder_passed: bool | None = None,
    decoder_command: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "jpeg": str(jpeg),
        "width": info.width,
        "height": info.height,
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
        "huffman_tables": [
            {
                "table_class": table.table_class,
                "table_id": table.table_id,
            }
            for table in info.huffman_tables
        ],
        "chroma_mode": jpeg_chroma_mode(info),
        "scan_data_bytes": info.scan_data_bytes,
        "app0_segments": info.app0_segments,
        "jfif_app0_segments": info.jfif_app0_segments,
        "dqt_segments": info.dqt_segments,
        "dht_segments": info.dht_segments,
        "dri_segments": info.dri_segments,
        "restart_interval": info.restart_interval,
        "restart_markers": info.restart_markers,
        "byte_length": info.byte_length,
        "sha256": info.sha256,
    }
    if decoder_passed is not None:
        record["decoder_passed"] = decoder_passed
    if decoder_command is not None:
        record["decoder_command"] = decoder_command
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


def ppm_evidence_record(path: Path, image: PpmImage) -> dict[str, object]:
    record = file_info_record(file_info(path, path.read_bytes()))
    record.update(
        {
            "width": image.width,
            "height": image.height,
            "rgb_bytes": len(image.rgb),
        }
    )
    return record


def run_evidence_record(
    jpeg: Path,
    info: JpegInfo,
    input_info: FileInfo | None = None,
    axi_lite: dict[str, object] | None = None,
    encoder_config: dict[str, object] | None = None,
    status_checks: list[dict[str, object]] | None = None,
    decoder_passed: bool | None = None,
    decoder_command: str | None = None,
) -> dict[str, object]:
    record = jpeg_info_record(jpeg, info, decoder_passed, decoder_command)
    if input_info is not None:
        record["input_rgb"] = {
            "path": input_info.path,
            "byte_length": input_info.byte_length,
            "sha256": input_info.sha256,
        }
    if axi_lite is not None:
        record["axi_lite"] = axi_lite
    if encoder_config is not None:
        record["encoder_config"] = encoder_config
    if status_checks is not None:
        record["status_checks"] = status_checks
    return record


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
    max_width: int = DEFAULT_MAX_FRAME_WIDTH,
    max_height: int = DEFAULT_MAX_FRAME_HEIGHT,
    timeout_seconds: float | None = 30.0,
    configure: Callable[[], None] | None = None,
    check_status: Callable[[str], None] | None = None,
    decoder_command: str | None = None,
) -> tuple[JpegInfo, FileInfo]:
    require_supported_dimensions(expected_width, expected_height, max_width, max_height)
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
        rx_thread.start()
        tx_stream.write(rgb)
        if hasattr(tx_stream, "flush"):
            tx_stream.flush()
        rx_thread.join(timeout_seconds)
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
    )
    if decoder_command is not None:
        run_decoder_command(output_jpeg, decoder_command)
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

    control = 0
    if clear_error:
        control |= CONTROL_CLEAR_PROTOCOL_ERROR
    if chroma_subsample:
        control |= CONTROL_ENABLE_CHROMA_SUBSAMPLE
    if emit_jfif:
        control |= CONTROL_EMIT_JFIF

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


def require_idle_status_value(status: int, context: str = "status") -> None:
    if status & STATUS_BUSY:
        raise RuntimeError(f"{context}: encoder is busy (status 0x{status:08x})")
    if status & STATUS_PROTOCOL_ERROR:
        raise RuntimeError(f"{context}: protocol_error is set (status 0x{status:08x})")


def require_idle_status(regs: AxiLiteWindow, context: str = "status") -> None:
    require_idle_status_value(regs.read32(REG_STATUS), context)


def clear_protocol_error(regs: AxiLiteWindow) -> None:
    current_control = regs.read32(REG_CONTROL)
    persistent_control = current_control & (
        CONTROL_ENABLE_CHROMA_SUBSAMPLE | CONTROL_EMIT_JFIF
    )
    regs.write32(REG_CONTROL, persistent_control | CONTROL_CLEAR_PROTOCOL_ERROR)


def _parse_int(value: str) -> int:
    return int(value, 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="hjpeg KV260 host-side helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pack = subparsers.add_parser("pack-ppm", help="pack a binary P6 PPM as RGB stream bytes")
    pack.add_argument("input", type=Path)
    pack.add_argument("output", type=Path)
    pack.add_argument("--max-width", type=int, default=DEFAULT_MAX_FRAME_WIDTH)
    pack.add_argument("--max-height", type=int, default=DEFAULT_MAX_FRAME_HEIGHT)
    pack.add_argument("--json", action="store_true", help="print packed stream evidence as JSON")

    make_ppm = subparsers.add_parser(
        "make-test-ppm",
        help="write a deterministic non-flat binary P6 PPM test image",
    )
    make_ppm.add_argument("output", type=Path)
    make_ppm.add_argument("--width", type=int, required=True)
    make_ppm.add_argument("--height", type=int, required=True)
    make_ppm.add_argument("--max-width", type=int, default=DEFAULT_MAX_FRAME_WIDTH)
    make_ppm.add_argument("--max-height", type=int, default=DEFAULT_MAX_FRAME_HEIGHT)
    make_ppm.add_argument("--json", action="store_true", help="print generated PPM evidence as JSON")

    validate = subparsers.add_parser("validate-jpeg", help="validate JPEG markers and dimensions")
    validate.add_argument("jpeg", type=Path)
    validate.add_argument("--width", type=int, required=True)
    validate.add_argument("--height", type=int, required=True)
    validate.add_argument(
        "--restart-interval",
        type=int,
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
    validate.add_argument("--json", action="store_true", help="print validation evidence as JSON")

    config = subparsers.add_parser("config", help="write encoder AXI-Lite configuration registers")
    config.add_argument("--dev", type=Path, default=Path("/dev/mem"))
    config.add_argument("--base-addr", type=_parse_int, required=True)
    config.add_argument("--width", type=int, required=True)
    config.add_argument("--height", type=int, required=True)
    config.add_argument("--max-width", type=int, default=DEFAULT_MAX_FRAME_WIDTH)
    config.add_argument("--max-height", type=int, default=DEFAULT_MAX_FRAME_HEIGHT)
    config.add_argument("--quality", type=int, default=50)
    config.add_argument("--restart-interval", type=int, default=0)
    config.add_argument("--chroma-subsample", action="store_true")
    config.add_argument("--no-jfif", action="store_true")
    config.add_argument("--clear-error", action="store_true")
    config.add_argument("--json", action="store_true", help="print configuration evidence as JSON")

    status = subparsers.add_parser("status", help="read encoder AXI-Lite status register")
    status.add_argument("--dev", type=Path, default=Path("/dev/mem"))
    status.add_argument("--base-addr", type=_parse_int, required=True)
    status.add_argument("--json", action="store_true", help="print status evidence as JSON")

    clear = subparsers.add_parser("clear-error", help="pulse the protocol-error clear bit")
    clear.add_argument("--dev", type=Path, default=Path("/dev/mem"))
    clear.add_argument("--base-addr", type=_parse_int, required=True)

    run = subparsers.add_parser(
        "run-stream-devices",
        help="configure hjpeg, stream RGB to a TX device, and capture JPEG from an RX device",
    )
    run.add_argument("--dev", type=Path, default=Path("/dev/mem"))
    run.add_argument("--base-addr", type=_parse_int, required=True)
    run.add_argument("--tx-device", type=Path, required=True)
    run.add_argument("--rx-device", type=Path, required=True)
    run.add_argument("--input-rgb", type=Path, required=True)
    run.add_argument("--output-jpeg", type=Path, required=True)
    run.add_argument("--width", type=int, required=True)
    run.add_argument("--height", type=int, required=True)
    run.add_argument("--max-width", type=int, default=DEFAULT_MAX_FRAME_WIDTH)
    run.add_argument("--max-height", type=int, default=DEFAULT_MAX_FRAME_HEIGHT)
    run.add_argument("--quality", type=int, default=50)
    run.add_argument("--restart-interval", type=int, default=0)
    run.add_argument("--chroma-subsample", action="store_true")
    run.add_argument("--no-jfif", action="store_true")
    run.add_argument("--clear-error", action="store_true")
    run.add_argument("--max-output-bytes", type=int, default=16 * 1024 * 1024)
    run.add_argument("--timeout-seconds", type=float, default=30.0)
    run.add_argument(
        "--decoder-command",
        help="optional external decoder command to prove the captured JPEG opens",
    )
    run.add_argument("--json", action="store_true", help="print capture evidence as JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "pack-ppm":
        image = read_ppm(args.input)
        require_supported_dimensions(image.width, image.height, args.max_width, args.max_height)
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
        )
        decoder_passed = None
        if args.decoder_command is not None:
            run_decoder_command(args.jpeg, args.decoder_command)
            decoder_passed = True
        if args.json:
            print(
                json.dumps(
                    jpeg_info_record(args.jpeg, info, decoder_passed, args.decoder_command),
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
            print(json.dumps(status_record(status), sort_keys=True))
            return 0
        print(f"0x{status:08x} {status_text(status)}")
        return 0

    if args.command == "clear-error":
        with AxiLiteWindow(args.dev, args.base_addr) as regs:
            clear_protocol_error(regs)
        print(f"cleared hjpeg protocol error at 0x{args.base_addr:x}")
        return 0

    if args.command == "run-stream-devices":
        status_checks: list[dict[str, object]] = []

        def record_status(context: str, status: int) -> None:
            record = status_record(status)
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
            max_width=args.max_width,
            max_height=args.max_height,
            timeout_seconds=args.timeout_seconds,
            configure=configure,
            check_status=check_status,
            decoder_command=args.decoder_command,
        )
        decoder_passed = True if args.decoder_command is not None else None
        if args.json:
            print(
                json.dumps(
                    run_evidence_record(
                        args.output_jpeg,
                        info,
                        input_info,
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
                        status_checks,
                        decoder_passed,
                        args.decoder_command,
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
