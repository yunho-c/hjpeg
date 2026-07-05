#!/usr/bin/env python3

import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import hjpeg_host


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    length = len(payload) + 2
    return bytes([0xFF, marker, (length >> 8) & 0xFF, length & 0xFF]) + payload


def standard_dqt_segments(quality: int) -> bytes:
    return b"".join(
        (
            jpeg_segment(
                0xDB,
                bytes([0x00])
                + hjpeg_host.scaled_quantization_payload(
                    hjpeg_host.STANDARD_LUMINANCE_QUANT,
                    quality,
                ),
            ),
            jpeg_segment(
                0xDB,
                bytes([0x01])
                + hjpeg_host.scaled_quantization_payload(
                    hjpeg_host.STANDARD_CHROMINANCE_QUANT,
                    quality,
                ),
            ),
        )
    )


def standard_dht_segments() -> bytes:
    table_infos = {
        (0, 0): 0x00,
        (0, 1): 0x01,
        (1, 0): 0x10,
        (1, 1): 0x11,
    }
    return b"".join(
        jpeg_segment(0xC4, bytes([table_infos[table]]) + payload)
        for table, payload in hjpeg_host.standard_huffman_payloads().items()
    )


def minimal_jpeg(width: int, height: int, chroma_subsample: bool = False, quality: int = 50) -> bytes:
    y_sampling = 0x22 if chroma_subsample else 0x11
    return (
        bytes([0xFF, 0xD8])
        + jpeg_segment(
            0xE0,
            bytes(
                [
                    0x4A,
                    0x46,
                    0x49,
                    0x46,
                    0x00,
                    0x01,
                    0x01,
                    0x00,
                    0x00,
                    0x01,
                    0x00,
                    0x01,
                    0x00,
                    0x00,
                ]
            ),
        )
        + standard_dqt_segments(quality)
        + jpeg_segment(
            0xC0,
            bytes(
                [
                    0x08,
                    (height >> 8) & 0xFF,
                    height & 0xFF,
                    (width >> 8) & 0xFF,
                    width & 0xFF,
                    0x03,
                    0x01,
                    y_sampling,
                    0x00,
                    0x02,
                    0x11,
                    0x01,
                    0x03,
                    0x11,
                    0x01,
                ]
            ),
        )
        + standard_dht_segments()
        + jpeg_segment(
            0xDA,
            bytes(
                [
                    0x03,
                    0x01,
                    0x00,
                    0x02,
                    0x11,
                    0x03,
                    0x11,
                    0x00,
                    0x3F,
                    0x00,
                ]
            ),
        )
        + bytes([0x7F, 0xFF, 0xD9])
    )


def single_component_jpeg(width: int, height: int) -> bytes:
    return bytes(
        [
            0xFF,
            0xD8,
            0xFF,
            0xDB,
            0x00,
            0x43,
            0x00,
            *([0x10] * 64),
            0xFF,
            0xC0,
            0x00,
            0x0B,
            0x08,
            (height >> 8) & 0xFF,
            height & 0xFF,
            (width >> 8) & 0xFF,
            width & 0xFF,
            0x01,
            0x01,
            0x11,
            0x00,
            0xFF,
            0xC4,
            0x00,
            0x14,
            0x00,
            *([0x00] * 15),
            0x01,
            0x00,
            0xFF,
            0xC4,
            0x00,
            0x14,
            0x10,
            *([0x00] * 15),
            0x01,
            0x00,
            0xFF,
            0xDA,
            0x00,
            0x08,
            0x01,
            0x01,
            0x00,
            0x00,
            0x3F,
            0x00,
            0x7F,
            0xFF,
            0xD9,
        ]
    )


def header_only_jpeg(width: int, height: int) -> bytes:
    return minimal_jpeg(width, height).split(b"\xff\xda", maxsplit=1)[0] + b"\xff\xd9"


def with_dri_segment(jpeg: bytes, restart_interval: int) -> bytes:
    sos_offset = jpeg.find(b"\xff\xda")
    if sos_offset < 0:
        raise AssertionError("SOS marker not found")
    dri = bytes(
        [
            0xFF,
            0xDD,
            0x00,
            0x04,
            (restart_interval >> 8) & 0xFF,
            restart_interval & 0xFF,
        ]
    )
    return jpeg[:sos_offset] + dri + jpeg[sos_offset:]


def with_scan_restart_marker(jpeg: bytes, restart_marker: int = 0) -> bytes:
    if not 0 <= restart_marker <= 7:
        raise AssertionError("restart marker must be in 0..7")
    eoi_payload = b"\x7f\xff\xd9"
    if eoi_payload not in jpeg:
        raise AssertionError("minimal scan payload not found")
    return jpeg.replace(
        eoi_payload,
        bytes([0x7F, 0xFF, 0xD0 + restart_marker, 0x55, 0xFF, 0xD9]),
        1,
    )


def with_scan_restart_markers(jpeg: bytes, restart_markers: list[int]) -> bytes:
    eoi_payload = b"\x7f\xff\xd9"
    if eoi_payload not in jpeg:
        raise AssertionError("minimal scan payload not found")
    scan = bytearray([0x7F])
    for marker_index, restart_marker in enumerate(restart_markers):
        if not 0 <= restart_marker <= 7:
            raise AssertionError("restart marker must be in 0..7")
        scan.extend([0xFF, 0xD0 + restart_marker, 0x40 + marker_index])
    scan.extend([0xFF, 0xD9])
    return jpeg.replace(eoi_payload, bytes(scan), 1)


def with_stuffed_entropy_ff(jpeg: bytes) -> bytes:
    eoi_payload = b"\x7f\xff\xd9"
    if eoi_payload not in jpeg:
        raise AssertionError("minimal scan payload not found")
    return jpeg.replace(eoi_payload, b"\x7f\xff\x00\xff\xd9", 1)


def with_unexpected_scan_marker(jpeg: bytes) -> bytes:
    eoi_payload = b"\x7f\xff\xd9"
    if eoi_payload not in jpeg:
        raise AssertionError("minimal scan payload not found")
    app1 = bytes([0xFF, 0xE1, 0x00, 0x02])
    return jpeg.replace(eoi_payload, b"\x7f" + app1 + b"\xff\xd9", 1)


def with_unexpected_header_marker(jpeg: bytes) -> bytes:
    dqt = jpeg.find(b"\xff\xdb")
    if dqt < 0:
        raise AssertionError("DQT marker not found")
    app1 = bytes([0xFF, 0xE1, 0x00, 0x02])
    return jpeg[:dqt] + app1 + jpeg[dqt:]


def with_non_jfif_app0(jpeg: bytes) -> bytes:
    app0 = jpeg.find(b"\xff\xe0")
    if app0 < 0:
        raise AssertionError("APP0 marker not found")
    mutated = bytearray(jpeg)
    mutated[app0 + 4 : app0 + 9] = b"BAD!\x00"
    return bytes(mutated)


def with_short_jfif_app0(jpeg: bytes) -> bytes:
    app0 = jpeg.find(b"\xff\xe0")
    if app0 < 0:
        raise AssertionError("APP0 marker not found")
    length = (jpeg[app0 + 2] << 8) | jpeg[app0 + 3]
    return (
        jpeg[: app0 + 2]
        + bytes([0x00, 0x07])
        + b"JFIF\x00"
        + jpeg[app0 + 2 + length :]
    )


def with_nonstandard_jfif_app0_fields(jpeg: bytes) -> bytes:
    app0 = jpeg.find(b"\xff\xe0")
    if app0 < 0:
        raise AssertionError("APP0 marker not found")
    mutated = bytearray(jpeg)
    mutated[app0 + 9] = 0x02
    return bytes(mutated)


def with_padded_jfif_app0(jpeg: bytes) -> bytes:
    app0 = jpeg.find(b"\xff\xe0")
    if app0 < 0:
        raise AssertionError("APP0 marker not found")
    length = (jpeg[app0 + 2] << 8) | jpeg[app0 + 3]
    padded_length = length + 1
    return (
        jpeg[: app0 + 2]
        + bytes([(padded_length >> 8) & 0xFF, padded_length & 0xFF])
        + jpeg[app0 + 4 : app0 + 2 + length]
        + b"\x00"
        + jpeg[app0 + 2 + length :]
    )


def with_duplicate_app0(jpeg: bytes) -> bytes:
    app0_start, app0_end = segment_bounds(jpeg, b"\xff\xe0")
    segment = jpeg[app0_start:app0_end]
    return jpeg[:app0_start] + segment + jpeg[app0_start:]


def without_segment(jpeg: bytes, marker: bytes) -> bytes:
    marker_offset = jpeg.find(marker)
    if marker_offset < 0:
        raise AssertionError(f"marker {marker.hex()} not found")
    length_offset = marker_offset + 2
    segment_length = (jpeg[length_offset] << 8) | jpeg[length_offset + 1]
    return jpeg[:marker_offset] + jpeg[length_offset + segment_length :]


def without_all_segments(jpeg: bytes, marker: bytes) -> bytes:
    stripped = jpeg
    while stripped.find(marker) >= 0:
        stripped = without_segment(stripped, marker)
    return stripped


def segment_bounds(jpeg: bytes, marker: bytes) -> tuple[int, int]:
    marker_offset = jpeg.find(marker)
    if marker_offset < 0:
        raise AssertionError(f"marker {marker.hex()} not found")
    segment_length = (jpeg[marker_offset + 2] << 8) | jpeg[marker_offset + 3]
    return marker_offset, marker_offset + 2 + segment_length


def with_segment_moved_before(
    jpeg: bytes, segment_marker: bytes, before_marker: bytes
) -> bytes:
    segment_start, segment_end = segment_bounds(jpeg, segment_marker)
    segment = jpeg[segment_start:segment_end]
    without_segment_bytes = jpeg[:segment_start] + jpeg[segment_end:]
    before_offset = without_segment_bytes.find(before_marker)
    if before_offset < 0:
        raise AssertionError(f"marker {before_marker.hex()} not found")
    return (
        without_segment_bytes[:before_offset]
        + segment
        + without_segment_bytes[before_offset:]
    )


def with_duplicate_sos_component(jpeg: bytes) -> bytes:
    sos = jpeg.find(b"\xff\xda\x00\x0c\x03")
    if sos < 0:
        raise AssertionError("SOS marker not found")
    duplicate_component_id_offset = sos + 9
    return (
        jpeg[:duplicate_component_id_offset]
        + bytes([jpeg[sos + 5]])
        + jpeg[duplicate_component_id_offset + 1 :]
    )


def with_two_component_sos(jpeg: bytes) -> bytes:
    sos = jpeg.find(b"\xff\xda\x00\x0c\x03")
    if sos < 0:
        raise AssertionError("SOS marker not found")
    sos_end = sos + 14
    shortened_sos = bytes(
        [
            0xFF,
            0xDA,
            0x00,
            0x0A,
            0x02,
            jpeg[sos + 5],
            jpeg[sos + 6],
            jpeg[sos + 7],
            jpeg[sos + 8],
            0x00,
            0x3F,
            0x00,
        ]
    )
    return jpeg[:sos] + shortened_sos + jpeg[sos_end:]


def with_nonstandard_sof0_component_ids(jpeg: bytes) -> bytes:
    sof0 = jpeg.find(b"\xff\xc0\x00\x11")
    if sof0 < 0:
        raise AssertionError("SOF0 marker not found")
    ids = (4, 5, 6)
    mutated = bytearray(jpeg)
    for index, component_id in enumerate(ids):
        mutated[sof0 + 10 + index * 3] = component_id
    sos = jpeg.find(b"\xff\xda\x00\x0c\x03")
    if sos < 0:
        raise AssertionError("SOS marker not found")
    for index, component_id in enumerate(ids):
        mutated[sos + 5 + index * 2] = component_id
    return bytes(mutated)


def with_reordered_sos_components(jpeg: bytes) -> bytes:
    sos = jpeg.find(b"\xff\xda\x00\x0c\x03")
    if sos < 0:
        raise AssertionError("SOS marker not found")
    mutated = bytearray(jpeg)
    first_pair = jpeg[sos + 5 : sos + 7]
    second_pair = jpeg[sos + 7 : sos + 9]
    mutated[sos + 5 : sos + 7] = second_pair
    mutated[sos + 7 : sos + 9] = first_pair
    return bytes(mutated)


def with_sos_table_selectors(jpeg: bytes, selectors: tuple[int, int, int]) -> bytes:
    sos = jpeg.find(b"\xff\xda\x00\x0c\x03")
    if sos < 0:
        raise AssertionError("SOS marker not found")
    mutated = bytearray(jpeg)
    for index, selector in enumerate(selectors):
        mutated[sos + 6 + index * 2] = selector
    return bytes(mutated)


def with_sos_spectral_fields(jpeg: bytes, start: int, end: int, successive: int) -> bytes:
    sos = jpeg.find(b"\xff\xda\x00\x0c\x03")
    if sos < 0:
        raise AssertionError("SOS marker not found")
    mutated = bytearray(jpeg)
    mutated[sos + 11] = start
    mutated[sos + 12] = end
    mutated[sos + 13] = successive
    return bytes(mutated)


def with_sof0_dimensions(jpeg: bytes, width: int, height: int) -> bytes:
    sof0 = jpeg.find(b"\xff\xc0\x00\x11")
    if sof0 < 0:
        raise AssertionError("SOF0 marker not found")
    mutated = bytearray(jpeg)
    mutated[sof0 + 5] = (height >> 8) & 0xFF
    mutated[sof0 + 6] = height & 0xFF
    mutated[sof0 + 7] = (width >> 8) & 0xFF
    mutated[sof0 + 8] = width & 0xFF
    return bytes(mutated)


def with_sof0_sampling_factors(jpeg: bytes, factors: tuple[int, int, int]) -> bytes:
    sof0 = jpeg.find(b"\xff\xc0\x00\x11")
    if sof0 < 0:
        raise AssertionError("SOF0 marker not found")
    mutated = bytearray(jpeg)
    for index, factor in enumerate(factors):
        mutated[sof0 + 11 + index * 3] = factor
    return bytes(mutated)


def with_sof0_quantization_tables(jpeg: bytes, tables: tuple[int, int, int]) -> bytes:
    sof0 = jpeg.find(b"\xff\xc0\x00\x11")
    if sof0 < 0:
        raise AssertionError("SOF0 marker not found")
    mutated = bytearray(jpeg)
    for index, table in enumerate(tables):
        mutated[sof0 + 12 + index * 3] = table
    return bytes(mutated)


def with_16bit_dqt(jpeg: bytes) -> bytes:
    dqt = jpeg.find(b"\xff\xdb\x00\x43\x00")
    if dqt < 0:
        raise AssertionError("DQT marker not found")
    original_payload = jpeg[dqt + 5 : dqt + 69]
    expanded_payload = bytearray()
    for value in original_payload:
        expanded_payload.extend([0x00, value])
    replacement = bytes([0xFF, 0xDB, 0x00, 0x83, 0x10]) + bytes(expanded_payload)
    return jpeg[:dqt] + replacement + jpeg[dqt + 69 :]


def with_zero_dqt_value(jpeg: bytes) -> bytes:
    dqt = jpeg.find(b"\xff\xdb\x00\x43\x00")
    if dqt < 0:
        raise AssertionError("DQT marker not found")
    mutated = bytearray(jpeg)
    mutated[dqt + 5] = 0
    return bytes(mutated)


def with_extra_dqt(jpeg: bytes) -> bytes:
    sof0 = jpeg.find(b"\xff\xc0")
    if sof0 < 0:
        raise AssertionError("SOF0 marker not found")
    extra_dqt = bytes([0xFF, 0xDB, 0x00, 0x43, 0x02, *([0x12] * 64)])
    return jpeg[:sof0] + extra_dqt + jpeg[sof0:]


def with_duplicate_dqt(jpeg: bytes) -> bytes:
    dqt = jpeg.find(b"\xff\xdb")
    if dqt < 0:
        raise AssertionError("DQT marker not found")
    length = (jpeg[dqt + 2] << 8) | jpeg[dqt + 3]
    segment = jpeg[dqt : dqt + 2 + length]
    return jpeg[:dqt] + segment + jpeg[dqt:]


def with_duplicate_dqt_table_in_segment(jpeg: bytes) -> bytes:
    dqt = jpeg.find(b"\xff\xdb")
    if dqt < 0:
        raise AssertionError("DQT marker not found")
    length = (jpeg[dqt + 2] << 8) | jpeg[dqt + 3]
    payload = jpeg[dqt + 4 : dqt + 2 + length]
    replacement_length = length + len(payload)
    replacement = (
        bytes([0xFF, 0xDB, (replacement_length >> 8) & 0xFF, replacement_length & 0xFF])
        + payload
        + payload
    )
    return jpeg[:dqt] + replacement + jpeg[dqt + 2 + length :]


def with_dqt_segments_swapped(jpeg: bytes) -> bytes:
    first_start, first_end = segment_bounds(jpeg, b"\xff\xdb")
    second_start = jpeg.find(b"\xff\xdb", first_end)
    if second_start < 0:
        raise AssertionError("second DQT marker not found")
    second_length = (jpeg[second_start + 2] << 8) | jpeg[second_start + 3]
    second_end = second_start + 2 + second_length
    first_segment = jpeg[first_start:first_end]
    second_segment = jpeg[second_start:second_end]
    return (
        jpeg[:first_start]
        + second_segment
        + jpeg[first_end:second_start]
        + first_segment
        + jpeg[second_end:]
    )


def with_duplicate_sof0(jpeg: bytes) -> bytes:
    sof0 = jpeg.find(b"\xff\xc0")
    if sof0 < 0:
        raise AssertionError("SOF0 marker not found")
    length = (jpeg[sof0 + 2] << 8) | jpeg[sof0 + 3]
    segment = jpeg[sof0 : sof0 + 2 + length]
    return jpeg[:sof0] + segment + jpeg[sof0:]


def with_duplicate_sos(jpeg: bytes) -> bytes:
    sos = jpeg.find(b"\xff\xda")
    if sos < 0:
        raise AssertionError("SOS marker not found")
    length = (jpeg[sos + 2] << 8) | jpeg[sos + 3]
    segment = jpeg[sos : sos + 2 + length]
    eoi = jpeg.rfind(b"\xff\xd9")
    if eoi < 0:
        raise AssertionError("EOI marker not found")
    return jpeg[:eoi] + segment + b"\x7f" + jpeg[eoi:]


def with_extra_dht(jpeg: bytes) -> bytes:
    sos = jpeg.find(b"\xff\xda")
    if sos < 0:
        raise AssertionError("SOS marker not found")
    extra_dht = bytes(
        [
            0xFF,
            0xC4,
            0x00,
            0x14,
            0x02,
            *([0x00] * 15),
            0x01,
            0x00,
        ]
    )
    return jpeg[:sos] + extra_dht + jpeg[sos:]


def with_duplicate_dht(jpeg: bytes) -> bytes:
    dht = jpeg.find(b"\xff\xc4")
    if dht < 0:
        raise AssertionError("DHT marker not found")
    length = (jpeg[dht + 2] << 8) | jpeg[dht + 3]
    segment = jpeg[dht : dht + 2 + length]
    return jpeg[:dht] + segment + jpeg[dht:]


def with_duplicate_dht_table_in_segment(jpeg: bytes) -> bytes:
    dht = jpeg.find(b"\xff\xc4")
    if dht < 0:
        raise AssertionError("DHT marker not found")
    length = (jpeg[dht + 2] << 8) | jpeg[dht + 3]
    payload = jpeg[dht + 4 : dht + 2 + length]
    replacement_length = length + len(payload)
    replacement = (
        bytes([0xFF, 0xC4, (replacement_length >> 8) & 0xFF, replacement_length & 0xFF])
        + payload
        + payload
    )
    return jpeg[:dht] + replacement + jpeg[dht + 2 + length :]


def with_first_two_dht_segments_swapped(jpeg: bytes) -> bytes:
    first_start, first_end = segment_bounds(jpeg, b"\xff\xc4")
    second_start = jpeg.find(b"\xff\xc4", first_end)
    if second_start < 0:
        raise AssertionError("second DHT marker not found")
    second_length = (jpeg[second_start + 2] << 8) | jpeg[second_start + 3]
    second_end = second_start + 2 + second_length
    first_segment = jpeg[first_start:first_end]
    second_segment = jpeg[second_start:second_end]
    return (
        jpeg[:first_start]
        + second_segment
        + jpeg[first_end:second_start]
        + first_segment
        + jpeg[second_end:]
    )


def with_empty_first_dht_table(jpeg: bytes) -> bytes:
    dht = jpeg.find(b"\xff\xc4")
    if dht < 0:
        raise AssertionError("DHT marker not found")
    length = (jpeg[dht + 2] << 8) | jpeg[dht + 3]
    table_info = jpeg[dht + 4]
    replacement = bytes([0xFF, 0xC4, 0x00, 0x13, table_info, *([0x00] * 16)])
    return jpeg[:dht] + replacement + jpeg[dht + 2 + length :]


def with_oversized_first_dht_table(jpeg: bytes) -> bytes:
    dht = jpeg.find(b"\xff\xc4")
    if dht < 0:
        raise AssertionError("DHT marker not found")
    length = (jpeg[dht + 2] << 8) | jpeg[dht + 3]
    table_info = jpeg[dht + 4]
    counts = bytes([0xFF, 0x02, *([0x00] * 14)])
    symbols = bytes([index & 0xFF for index in range(257)])
    replacement_length = 2 + 1 + len(counts) + len(symbols)
    replacement = (
        bytes([0xFF, 0xC4, (replacement_length >> 8) & 0xFF, replacement_length & 0xFF])
        + bytes([table_info])
        + counts
        + symbols
    )
    return jpeg[:dht] + replacement + jpeg[dht + 2 + length :]


def with_oversubscribed_first_dht_table(jpeg: bytes) -> bytes:
    dht = jpeg.find(b"\xff\xc4")
    if dht < 0:
        raise AssertionError("DHT marker not found")
    length = (jpeg[dht + 2] << 8) | jpeg[dht + 3]
    table_info = jpeg[dht + 4]
    counts = bytes([0x03, *([0x00] * 15)])
    symbols = bytes([0x00, 0x01, 0x02])
    replacement_length = 2 + 1 + len(counts) + len(symbols)
    replacement = (
        bytes([0xFF, 0xC4, (replacement_length >> 8) & 0xFF, replacement_length & 0xFF])
        + bytes([table_info])
        + counts
        + symbols
    )
    return jpeg[:dht] + replacement + jpeg[dht + 2 + length :]


def with_mutated_first_dqt_payload(jpeg: bytes) -> bytes:
    dqt = jpeg.find(b"\xff\xdb")
    if dqt < 0:
        raise AssertionError("DQT marker not found")
    mutated = bytearray(jpeg)
    mutated[dqt + 5] ^= 0x01
    return bytes(mutated)


def with_mutated_first_dht_payload(jpeg: bytes) -> bytes:
    dht = jpeg.find(b"\xff\xc4")
    if dht < 0:
        raise AssertionError("DHT marker not found")
    mutated = bytearray(jpeg)
    mutated[dht + 21] ^= 0x01
    return bytes(mutated)


def with_invalid_dc_dht_symbol(jpeg: bytes) -> bytes:
    dht = jpeg.find(b"\xff\xc4")
    if dht < 0:
        raise AssertionError("DHT marker not found")
    mutated = bytearray(jpeg)
    mutated[dht + 21] = 0x0C
    return bytes(mutated)


def with_invalid_ac_dht_symbol(jpeg: bytes) -> bytes:
    dht = jpeg.find(b"\xff\xc4\x00\xb5\x10")
    if dht < 0:
        raise AssertionError("AC DHT marker not found")
    mutated = bytearray(jpeg)
    mutated[dht + 21] = 0x10
    return bytes(mutated)


def standard_dht_payload_sha256(table_class: int, table_id: int) -> str:
    return hjpeg_host.expected_huffman_payload_hashes()[(table_class, table_id)][1]


def standard_dqt_payload_sha256(table_id: int, quality: int = 50) -> str:
    return hjpeg_host.expected_quantization_payload_hashes(quality)[table_id]


def minimal_jpeg_info(width: int, height: int) -> hjpeg_host.JpegInfo:
    data = minimal_jpeg(width, height)
    return hjpeg_host.JpegInfo(
        width=width,
        height=height,
        mcu_count=((width + 7) // 8) * ((height + 7) // 8),
        sample_precision=8,
        components=(
            hjpeg_host.JpegComponent(1, 1, 1, 0),
            hjpeg_host.JpegComponent(2, 1, 1, 1),
            hjpeg_host.JpegComponent(3, 1, 1, 1),
        ),
        scan_components=(
            hjpeg_host.JpegScanComponent(1, 0, 0),
            hjpeg_host.JpegScanComponent(2, 1, 1),
            hjpeg_host.JpegScanComponent(3, 1, 1),
        ),
        spectral_start=0,
        spectral_end=63,
        successive_approximation=0,
        quantization_tables=(0, 1),
        quantization_table_order=(0, 1),
        quantization_table_details=(
            hjpeg_host.JpegQuantizationTable(
                0,
                0,
                64,
                standard_dqt_payload_sha256(0),
            ),
            hjpeg_host.JpegQuantizationTable(
                1,
                0,
                64,
                standard_dqt_payload_sha256(1),
            ),
        ),
        huffman_tables=(
            hjpeg_host.JpegHuffmanTable(0, 0, 12, standard_dht_payload_sha256(0, 0)),
            hjpeg_host.JpegHuffmanTable(0, 1, 12, standard_dht_payload_sha256(0, 1)),
            hjpeg_host.JpegHuffmanTable(1, 0, 162, standard_dht_payload_sha256(1, 0)),
            hjpeg_host.JpegHuffmanTable(1, 1, 162, standard_dht_payload_sha256(1, 1)),
        ),
        huffman_table_order=((0, 0), (0, 1), (1, 0), (1, 1)),
        scan_data_bytes=1,
        scan_data_sha256=hashlib.sha256(b"\x7f").hexdigest(),
        stuffed_ff_bytes=0,
        byte_length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        app0_segments=1,
        jfif_app0_segments=1,
        jfif_app0=hjpeg_host.JfifApp0Info(
            version_major=1,
            version_minor=1,
            density_units=0,
            x_density=1,
            y_density=1,
            thumbnail_width=0,
            thumbnail_height=0,
        ),
        dqt_segments=2,
        sof0_segments=1,
        dht_segments=4,
        sos_segments=1,
        dri_segments=0,
        restart_interval=None,
        restart_markers=0,
        restart_marker_sequence=(),
        marker_sequence=(
            "SOI",
            "APP0",
            "DQT",
            "DQT",
            "SOF0",
            "DHT",
            "DHT",
            "DHT",
            "DHT",
            "SOS",
            "EOI",
        ),
    )


class HjpegHostTest(unittest.TestCase):
    def test_make_test_image_writes_non_flat_ppm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ppm = Path(tmp) / "pattern.ppm"
            image = hjpeg_host.make_test_image(width=17, height=9)

            self.assertEqual(image.width, 17)
            self.assertEqual(image.height, 9)
            self.assertEqual(len(image.rgb), 17 * 9 * 3)
            self.assertGreater(len(set(image.rgb)), 8)

            hjpeg_host.write_ppm(image, ppm)
            decoded = hjpeg_host.read_ppm(ppm)
            self.assertEqual(decoded, image)

    def test_make_test_ppm_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ppm = Path(tmp) / "cli.ppm"

            self.assertEqual(
                hjpeg_host.main(["make-test-ppm", str(ppm), "--width", "3", "--height", "2"]),
                0,
            )

            image = hjpeg_host.read_ppm(ppm)
            self.assertEqual((image.width, image.height), (3, 2))

    def test_make_test_ppm_cli_rejects_default_oversize_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ppm = Path(tmp) / "too-wide.ppm"

            with self.assertRaisesRegex(ValueError, "width must be in 1..1920"):
                hjpeg_host.main(
                    [
                        "make-test-ppm",
                        str(ppm),
                        "--width",
                        "1921",
                        "--height",
                        "1",
                    ]
                )

    def test_make_test_ppm_cli_allows_custom_frame_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ppm = Path(tmp) / "custom.ppm"

            self.assertEqual(
                hjpeg_host.main(
                    [
                        "make-test-ppm",
                        str(ppm),
                        "--width",
                        "4",
                        "--height",
                        "2",
                        "--max-width",
                        "4",
                        "--max-height",
                        "2",
                    ]
                ),
                0,
            )
            self.assertEqual((hjpeg_host.read_ppm(ppm).width, hjpeg_host.read_ppm(ppm).height), (4, 2))

    def test_make_test_ppm_cli_can_print_json_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ppm = Path(tmp) / "pattern.ppm"

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    hjpeg_host.main(
                        [
                            "make-test-ppm",
                            str(ppm),
                            "--width",
                            "3",
                            "--height",
                            "2",
                            "--json",
                        ]
                    ),
                    0,
                )

            record = json.loads(stdout.getvalue())
            ppm_bytes = ppm.read_bytes()
            self.assertTrue(record["deterministic_pattern"])
            self.assertEqual(record["max_width"], 1920)
            self.assertEqual(record["max_height"], 1080)
            self.assertEqual(record["output_ppm"]["path"], str(ppm))
            self.assertEqual(record["output_ppm"]["width"], 3)
            self.assertEqual(record["output_ppm"]["height"], 2)
            self.assertEqual(record["output_ppm"]["rgb_bytes"], 18)
            self.assertEqual(
                record["output_ppm"]["image_stats"],
                {
                    "channel_min": {"r": 0, "g": 0, "b": 0},
                    "channel_max": {"r": 255, "g": 255, "b": 127},
                    "non_flat": True,
                    "has_color_pixels": True,
                },
            )
            self.assertEqual(record["output_ppm"]["byte_length"], len(ppm_bytes))
            self.assertEqual(
                record["output_ppm"]["sha256"],
                hashlib.sha256(ppm_bytes).hexdigest(),
            )

    def test_read_ppm_with_comment_and_pack_rgb_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ppm = root / "input.ppm"
            rgb = root / "input.rgb"
            ppm.write_bytes(b"P6\n# comment\n2 1\n255\n" + bytes([1, 2, 3, 4, 5, 6]))

            image = hjpeg_host.read_ppm(ppm)
            self.assertEqual(image.width, 2)
            self.assertEqual(image.height, 1)
            self.assertEqual(image.rgb, bytes([1, 2, 3, 4, 5, 6]))

            hjpeg_host.write_rgb_stream(image, rgb)
            self.assertEqual(rgb.read_bytes(), bytes([1, 2, 3, 0, 4, 5, 6, 0]))

    def test_pack_ppm_cli_can_print_json_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ppm = root / "input.ppm"
            rgb = root / "input.rgb"
            ppm.write_bytes(b"P6\n2 1\n255\n" + bytes([1, 2, 3, 4, 5, 6]))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    hjpeg_host.main(["pack-ppm", str(ppm), str(rgb), "--json"]),
                    0,
                )

            record = json.loads(stdout.getvalue())
            ppm_bytes = ppm.read_bytes()
            rgb_bytes = rgb.read_bytes()
            self.assertEqual(record["width"], 2)
            self.assertEqual(record["height"], 1)
            self.assertEqual(record["max_width"], 1920)
            self.assertEqual(record["max_height"], 1080)
            self.assertEqual(record["expected_rgb_stream_bytes"], 8)
            self.assertEqual(record["input_ppm"]["path"], str(ppm))
            self.assertEqual(record["input_ppm"]["byte_length"], len(ppm_bytes))
            self.assertEqual(
                record["input_ppm"]["image_stats"],
                {
                    "channel_min": {"r": 1, "g": 2, "b": 3},
                    "channel_max": {"r": 4, "g": 5, "b": 6},
                    "non_flat": True,
                    "has_color_pixels": True,
                },
            )
            self.assertEqual(
                record["input_ppm"]["sha256"],
                hashlib.sha256(ppm_bytes).hexdigest(),
            )
            self.assertEqual(record["output_rgb"]["path"], str(rgb))
            self.assertEqual(record["output_rgb"]["byte_length"], len(rgb_bytes))
            self.assertEqual(
                record["output_rgb"]["sha256"],
                hashlib.sha256(rgb_bytes).hexdigest(),
            )

    def test_pack_ppm_cli_rejects_default_oversize_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ppm = root / "input.ppm"
            rgb = root / "input.rgb"
            ppm.write_bytes(b"P6\n1921 1\n255\n")

            with self.assertRaisesRegex(ValueError, "width must be in 1..1920"):
                hjpeg_host.main(["pack-ppm", str(ppm), str(rgb)])

    def test_read_ppm_rejects_oversize_frame_before_payload_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ppm = Path(tmp) / "huge.ppm"
            ppm.write_bytes(b"P6\n999999999 1\n255\n")

            with self.assertRaisesRegex(ValueError, "width must be in 1..1920"):
                hjpeg_host.read_ppm(ppm, max_width=1920, max_height=1080)

    def test_pack_ppm_cli_allows_custom_frame_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ppm = root / "input.ppm"
            rgb = root / "input.rgb"
            ppm.write_bytes(b"P6\n3 1\n255\n" + bytes([1, 2, 3, 4, 5, 6, 7, 8, 9]))

            self.assertEqual(
                hjpeg_host.main(
                    [
                        "pack-ppm",
                        str(ppm),
                        str(rgb),
                        "--max-width",
                        "3",
                        "--max-height",
                        "1",
                    ]
                ),
                0,
            )
            self.assertEqual(len(rgb.read_bytes()), 12)

    def test_validate_jpeg_checks_sof0_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13))

            self.assertEqual(hjpeg_host.jpeg_dimensions(jpeg.read_bytes()), (17, 13))
            parsed = hjpeg_host.jpeg_info(jpeg.read_bytes())
            self.assertEqual(parsed.mcu_count, 6)
            self.assertEqual(parsed.sample_precision, 8)
            self.assertEqual(len(parsed.components), 3)
            self.assertEqual(parsed.components[0], hjpeg_host.JpegComponent(1, 1, 1, 0))
            self.assertEqual(parsed.components[1], hjpeg_host.JpegComponent(2, 1, 1, 1))
            self.assertEqual(parsed.components[2], hjpeg_host.JpegComponent(3, 1, 1, 1))
            self.assertEqual(hjpeg_host.jpeg_chroma_mode(parsed), "4:4:4")
            self.assertEqual(
                parsed.scan_components,
                (
                    hjpeg_host.JpegScanComponent(1, 0, 0),
                    hjpeg_host.JpegScanComponent(2, 1, 1),
                    hjpeg_host.JpegScanComponent(3, 1, 1),
                ),
            )
            self.assertEqual(parsed.spectral_start, 0)
            self.assertEqual(parsed.spectral_end, 63)
            self.assertEqual(parsed.successive_approximation, 0)
            self.assertEqual(parsed.quantization_tables, (0, 1))
            self.assertEqual(
                parsed.quantization_table_details,
                (
                    hjpeg_host.JpegQuantizationTable(
                        0,
                        0,
                        64,
                        standard_dqt_payload_sha256(0),
                    ),
                    hjpeg_host.JpegQuantizationTable(
                        1,
                        0,
                        64,
                        standard_dqt_payload_sha256(1),
                    ),
                ),
            )
            self.assertEqual(
                parsed.huffman_tables,
                (
                    hjpeg_host.JpegHuffmanTable(0, 0, 12, standard_dht_payload_sha256(0, 0)),
                    hjpeg_host.JpegHuffmanTable(0, 1, 12, standard_dht_payload_sha256(0, 1)),
                    hjpeg_host.JpegHuffmanTable(1, 0, 162, standard_dht_payload_sha256(1, 0)),
                    hjpeg_host.JpegHuffmanTable(1, 1, 162, standard_dht_payload_sha256(1, 1)),
                ),
            )
            self.assertEqual(parsed.scan_data_bytes, 1)
            self.assertEqual(parsed.scan_data_sha256, hashlib.sha256(b"\x7f").hexdigest())
            self.assertEqual(parsed.stuffed_ff_bytes, 0)
            self.assertEqual(parsed.app0_segments, 1)
            self.assertEqual(parsed.jfif_app0_segments, 1)
            self.assertEqual(
                parsed.jfif_app0,
                hjpeg_host.JfifApp0Info(1, 1, 0, 1, 1, 0, 0),
            )
            self.assertEqual(parsed.dqt_segments, 2)
            self.assertEqual(parsed.sof0_segments, 1)
            self.assertEqual(parsed.dht_segments, 4)
            self.assertEqual(parsed.sos_segments, 1)
            self.assertEqual(parsed.dri_segments, 0)
            self.assertIsNone(parsed.restart_interval)
            self.assertEqual(parsed.restart_markers, 0)
            self.assertEqual(parsed.restart_marker_sequence, ())
            self.assertEqual(
                parsed.marker_sequence,
                (
                    "SOI",
                    "APP0",
                    "DQT",
                    "DQT",
                    "SOF0",
                    "DHT",
                    "DHT",
                    "DHT",
                    "DHT",
                    "SOS",
                    "EOI",
                ),
            )
            self.assertEqual(
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13),
                minimal_jpeg_info(width=17, height=13),
            )
            hjpeg_host.run_decoder_command(
                jpeg,
                f'"{sys.executable}" -c "import sys; assert open(sys.argv[1], \'rb\').read(2) == bytes([0xff, 0xd8])"',
            )
            with self.assertRaisesRegex(ValueError, "expected 16x13"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=16, expected_height=13)

    def test_validate_jpeg_rejects_zero_sof0_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            zero_width = root / "zero-width.jpg"
            zero_height = root / "zero-height.jpg"
            zero_width.write_bytes(
                with_sof0_dimensions(minimal_jpeg(width=17, height=13), width=0, height=13)
            )
            zero_height.write_bytes(
                with_sof0_dimensions(minimal_jpeg(width=17, height=13), width=17, height=0)
            )

            with self.assertRaisesRegex(ValueError, "expected nonzero dimensions"):
                hjpeg_host.validate_jpeg(zero_width, expected_width=17, expected_height=13)
            with self.assertRaisesRegex(ValueError, "expected nonzero dimensions"):
                hjpeg_host.validate_jpeg(zero_height, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_non_eight_bit_sof0_precision(self) -> None:
        data = minimal_jpeg(width=17, height=13).replace(
            b"\xff\xc0\x00\x11\x08", b"\xff\xc0\x00\x11\x0c", 1
        )
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "bad-precision.jpg"
            jpeg.write_bytes(data)

            with self.assertRaisesRegex(ValueError, "sample precision"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_non_three_component_sof0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "single-component.jpg"
            jpeg.write_bytes(single_component_jpeg(width=17, height=13))

            with self.assertRaisesRegex(ValueError, "component count"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_cli_can_run_decoder_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            marker = Path(tmp) / "decoder-ran.txt"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13))

            command = (
                f'"{sys.executable}" -c "import pathlib, sys; '
                f'pathlib.Path(r\'{marker}\').write_text(pathlib.Path(sys.argv[1]).read_bytes()[:2].hex())"'
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    hjpeg_host.main(
                        [
                            "validate-jpeg",
                            str(jpeg),
                            "--width",
                            "17",
                            "--height",
                            "13",
                            "--decoder-command",
                            command,
                        ]
                    ),
                    0,
                )
            self.assertEqual(marker.read_text(), "ffd8")
            self.assertIn("scan_data_bytes=1", stdout.getvalue())
            self.assertIn(
                "scan_data_sha256=" + hashlib.sha256(b"\x7f").hexdigest(),
                stdout.getvalue(),
            )
            self.assertIn("stuffed_ff_bytes=0", stdout.getvalue())
            self.assertIn("byte_length=", stdout.getvalue())
            self.assertIn("sha256=", stdout.getvalue())
            self.assertIn("decoder=pass", stdout.getvalue())

    def test_validate_jpeg_cli_can_print_json_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    hjpeg_host.main(
                        [
                            "validate-jpeg",
                            str(jpeg),
                            "--width",
                            "17",
                            "--height",
                            "13",
                            "--quality",
                            "50",
                            "--require-standard-huffman",
                            "--json",
                        ]
                    ),
                    0,
                )

            record = json.loads(stdout.getvalue())
            self.assertEqual(record["jpeg"], str(jpeg))
            self.assertEqual(record["width"], 17)
            self.assertEqual(record["height"], 13)
            self.assertEqual(
                record["validation_expectations"],
                {
                    "width": 17,
                    "height": 13,
                    "expected_sample_precision": 8,
                    "expected_component_count": 3,
                    "restart_interval": None,
                    "expected_restart_markers": None,
                    "expected_restart_marker_sequence": None,
                    "expected_scan_data_min_bytes": 1,
                    "expected_marker_counts": {
                        "APP0": None,
                        "JFIF_APP0": None,
                        "DQT": 2,
                        "SOF0": 1,
                        "DHT": 4,
                        "SOS": 1,
                        "DRI": None,
                        "RST": None,
                    },
                    "expected_marker_order": {
                        "through_sos": [
                            "SOI",
                            "DQT",
                            "DQT",
                            "SOF0",
                            "DHT",
                            "DHT",
                            "DHT",
                            "DHT",
                            "SOS",
                        ],
                        "app0_policy": "optional",
                        "dri_policy": "optional",
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
                    "expected_sof0_components": [
                        {"component_id": 1, "quantization_table": 0},
                        {"component_id": 2, "quantization_table": 1},
                        {"component_id": 3, "quantization_table": 1},
                    ],
                    "expected_sos_components": [
                        {"component_id": 1, "dc_table": 0, "ac_table": 0},
                        {"component_id": 2, "dc_table": 1, "ac_table": 1},
                        {"component_id": 3, "dc_table": 1, "ac_table": 1},
                    ],
                    "expected_sos_spectral": {
                        "spectral_start": 0,
                        "spectral_end": 63,
                        "successive_approximation": 0,
                    },
                    "check_chroma_mode": False,
                    "chroma_subsample": None,
                    "expected_chroma_mode": None,
                    "expect_jfif": None,
                    "expected_jfif_app0": None,
                    "quality": 50,
                    "require_standard_huffman": True,
                    "expected_quantization_payload_sha256": {
                        "0": standard_dqt_payload_sha256(0),
                        "1": standard_dqt_payload_sha256(1),
                    },
                    "expected_huffman_tables": [
                        {
                            "table_class": 0,
                            "table_id": 0,
                            "symbol_count": 12,
                            "payload_sha256": standard_dht_payload_sha256(0, 0),
                        },
                        {
                            "table_class": 0,
                            "table_id": 1,
                            "symbol_count": 12,
                            "payload_sha256": standard_dht_payload_sha256(0, 1),
                        },
                        {
                            "table_class": 1,
                            "table_id": 0,
                            "symbol_count": 162,
                            "payload_sha256": standard_dht_payload_sha256(1, 0),
                        },
                        {
                            "table_class": 1,
                            "table_id": 1,
                            "symbol_count": 162,
                            "payload_sha256": standard_dht_payload_sha256(1, 1),
                        },
                    ],
                },
            )
            self.assertEqual(record["mcu_count"], 6)
            self.assertEqual(record["sample_precision"], 8)
            self.assertEqual(record["component_count"], 3)
            self.assertEqual(record["chroma_mode"], "4:4:4")
            self.assertEqual(record["components"][0]["component_id"], 1)
            self.assertEqual(record["components"][0]["horizontal_sampling"], 1)
            self.assertEqual(record["components"][0]["vertical_sampling"], 1)
            self.assertEqual(record["components"][0]["quantization_table"], 0)
            self.assertEqual(record["scan_components"][1]["component_id"], 2)
            self.assertEqual(record["scan_components"][1]["dc_table"], 1)
            self.assertEqual(record["scan_components"][1]["ac_table"], 1)
            self.assertEqual(record["spectral_start"], 0)
            self.assertEqual(record["spectral_end"], 63)
            self.assertEqual(record["successive_approximation"], 0)
            self.assertEqual(record["quantization_tables"], [0, 1])
            self.assertEqual(record["quantization_table_order"], [0, 1])
            self.assertEqual(
                record["quantization_table_details"],
                [
                    {
                        "table_id": 0,
                        "precision": 0,
                        "byte_length": 64,
                        "payload_sha256": standard_dqt_payload_sha256(0),
                    },
                    {
                        "table_id": 1,
                        "precision": 0,
                        "byte_length": 64,
                        "payload_sha256": standard_dqt_payload_sha256(1),
                    },
                ],
            )
            self.assertEqual(
                record["huffman_tables"],
                [
                    {
                        "table_class": 0,
                        "table_id": 0,
                        "symbol_count": 12,
                        "payload_sha256": standard_dht_payload_sha256(0, 0),
                    },
                    {
                        "table_class": 0,
                        "table_id": 1,
                        "symbol_count": 12,
                        "payload_sha256": standard_dht_payload_sha256(0, 1),
                    },
                    {
                        "table_class": 1,
                        "table_id": 0,
                        "symbol_count": 162,
                        "payload_sha256": standard_dht_payload_sha256(1, 0),
                    },
                    {
                        "table_class": 1,
                        "table_id": 1,
                        "symbol_count": 162,
                        "payload_sha256": standard_dht_payload_sha256(1, 1),
                    },
                ],
            )
            self.assertEqual(
                record["huffman_table_order"],
                [
                    {"table_class": 0, "table_id": 0},
                    {"table_class": 0, "table_id": 1},
                    {"table_class": 1, "table_id": 0},
                    {"table_class": 1, "table_id": 1},
                ],
            )
            self.assertEqual(record["scan_data_bytes"], 1)
            self.assertEqual(record["scan_data_sha256"], hashlib.sha256(b"\x7f").hexdigest())
            self.assertEqual(record["stuffed_ff_bytes"], 0)
            self.assertEqual(record["app0_segments"], 1)
            self.assertEqual(record["jfif_app0_segments"], 1)
            self.assertEqual(
                record["jfif_app0"],
                {
                    "version_major": 1,
                    "version_minor": 1,
                    "density_units": 0,
                    "x_density": 1,
                    "y_density": 1,
                    "thumbnail_width": 0,
                    "thumbnail_height": 0,
                },
            )
            self.assertEqual(record["dqt_segments"], 2)
            self.assertEqual(record["sof0_segments"], 1)
            self.assertEqual(record["dht_segments"], 4)
            self.assertEqual(record["sos_segments"], 1)
            self.assertEqual(record["dri_segments"], 0)
            self.assertIsNone(record["restart_interval"])
            self.assertEqual(record["restart_markers"], 0)
            self.assertEqual(
                record["marker_counts"],
                {
                    "APP0": 1,
                    "JFIF_APP0": 1,
                    "DQT": 2,
                    "SOF0": 1,
                    "DHT": 4,
                    "SOS": 1,
                    "DRI": 0,
                    "RST": 0,
                },
            )
            self.assertEqual(record["restart_marker_sequence"], [])
            self.assertEqual(
                record["marker_sequence"],
                [
                    "SOI",
                    "APP0",
                    "DQT",
                    "DQT",
                    "SOF0",
                    "DHT",
                    "DHT",
                    "DHT",
                    "DHT",
                    "SOS",
                    "EOI",
                ],
            )
            self.assertEqual(record["byte_length"], len(minimal_jpeg(width=17, height=13)))
            self.assertEqual(
                record["sha256"],
                hashlib.sha256(minimal_jpeg(width=17, height=13)).hexdigest(),
            )
            self.assertNotIn("decoder_passed", record)
            self.assertNotIn("decoder_command", record)
            self.assertNotIn("decoder_timeout_seconds", record)
            self.assertNotIn("decoder_returncode", record)
            self.assertNotIn("decoder_stdout", record)
            self.assertNotIn("decoder_stderr", record)

    def test_run_evidence_record_omits_transfer_rates_for_zero_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jpeg = root / "output.jpg"
            input_rgb = root / "input.rgb"
            jpeg.write_bytes(minimal_jpeg(width=2, height=1))
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 6, 0]))

            record = hjpeg_host.run_evidence_record(
                jpeg,
                minimal_jpeg_info(width=2, height=1),
                input_info=hjpeg_host.file_info(input_rgb, input_rgb.read_bytes()),
                transfer_elapsed_seconds=0.0,
            )

            self.assertEqual(record["transfer_elapsed_seconds"], 0.0)
            self.assertNotIn("host_transfer_rates", record)
            self.assertFalse(
                record["hardware_run_summary"]["evidence_present"]["transfer_timing"]
            )
            self.assertFalse(
                record["hardware_run_summary"]["checks"][
                    "transfer_elapsed_seconds_positive"
                ]
            )
            self.assertFalse(
                record["hardware_run_summary"]["checks"]["host_transfer_rates_present"]
            )
            self.assertFalse(record["hardware_run_summary"]["all_recorded_checks_passed"])

    def test_run_evidence_record_reports_input_length_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jpeg = root / "output.jpg"
            input_rgb = root / "input.rgb"
            jpeg.write_bytes(minimal_jpeg(width=2, height=1))
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 6, 0]))
            input_info = hjpeg_host.file_info(input_rgb, input_rgb.read_bytes())

            matched = hjpeg_host.run_evidence_record(
                jpeg,
                minimal_jpeg_info(width=2, height=1),
                input_info=input_info,
                expected_input_rgb_bytes=8,
            )
            mismatched = hjpeg_host.run_evidence_record(
                jpeg,
                minimal_jpeg_info(width=2, height=1),
                input_info=input_info,
                expected_input_rgb_bytes=12,
            )

            self.assertTrue(matched["input_rgb"]["byte_length_matches_expected"])
            self.assertFalse(mismatched["input_rgb"]["byte_length_matches_expected"])
            self.assertTrue(
                matched["hardware_run_summary"]["checks"][
                    "input_rgb_length_matches_expected"
                ]
            )
            self.assertFalse(
                mismatched["hardware_run_summary"]["checks"][
                    "input_rgb_length_matches_expected"
                ]
            )
            self.assertTrue(matched["hardware_run_summary"]["all_recorded_checks_passed"])
            self.assertFalse(mismatched["hardware_run_summary"]["all_recorded_checks_passed"])
            self.assertFalse(
                matched["hardware_run_summary"]["evidence_present"]["decoder"]
            )
            self.assertTrue(matched["hardware_run_summary"]["evidence_present"]["input_rgb"])
            self.assertFalse(matched["hardware_run_summary"]["evidence_present"]["input_ppm"])
            self.assertFalse(
                matched["hardware_run_summary"]["complete_hardware_run_evidence"]
            )

    def test_hardware_summary_requires_complete_input_rgb_evidence(self) -> None:
        incomplete = {
            "input_rgb": {
                "byte_length": 8,
                "sha256": "",
                "expected_byte_length": 8,
                "byte_length_matches_expected": True,
            }
        }
        complete = {
            "input_rgb": {
                "byte_length": 8,
                "sha256": "0" * 64,
                "expected_byte_length": 8,
                "byte_length_matches_expected": True,
            }
        }

        incomplete_summary = hjpeg_host.hardware_run_summary_record(incomplete)
        complete_summary = hjpeg_host.hardware_run_summary_record(complete)

        self.assertFalse(incomplete_summary["evidence_present"]["input_rgb"])
        self.assertFalse(incomplete_summary["checks"]["input_rgb_sha256_present"])
        self.assertTrue(complete_summary["evidence_present"]["input_rgb"])
        self.assertTrue(complete_summary["checks"]["input_rgb_byte_length_positive"])
        self.assertTrue(complete_summary["checks"]["input_rgb_sha256_present"])
        self.assertTrue(
            complete_summary["checks"]["input_rgb_expected_byte_length_positive"]
        )
        self.assertTrue(complete_summary["checks"]["input_rgb_length_matches_expected"])

    def test_hardware_summary_requires_complete_input_ppm_evidence(self) -> None:
        incomplete = {
            "input_ppm": {
                "byte_length": 16,
                "sha256": "",
                "width": 2,
                "height": 1,
                "rgb_bytes": 6,
                "packed_rgb_byte_length": 8,
                "packed_rgb_sha256": "1" * 64,
                "packed_rgb_matches_input": True,
                "image_stats": {"non_flat": True, "has_color_pixels": False},
            }
        }
        complete = {
            "input_ppm": {
                "byte_length": 16,
                "sha256": "0" * 64,
                "width": 2,
                "height": 1,
                "rgb_bytes": 6,
                "packed_rgb_byte_length": 8,
                "packed_rgb_sha256": "1" * 64,
                "packed_rgb_matches_input": True,
                "image_stats": {"non_flat": True, "has_color_pixels": True},
            }
        }

        incomplete_summary = hjpeg_host.hardware_run_summary_record(incomplete)
        complete_summary = hjpeg_host.hardware_run_summary_record(complete)

        self.assertFalse(incomplete_summary["evidence_present"]["input_ppm"])
        self.assertFalse(incomplete_summary["checks"]["input_ppm_sha256_present"])
        self.assertFalse(incomplete_summary["checks"]["input_ppm_has_color_pixels"])
        self.assertTrue(complete_summary["evidence_present"]["input_ppm"])
        self.assertTrue(complete_summary["checks"]["input_ppm_byte_length_positive"])
        self.assertTrue(complete_summary["checks"]["input_ppm_sha256_present"])
        self.assertTrue(complete_summary["checks"]["input_ppm_dimensions_positive"])
        self.assertTrue(
            complete_summary["checks"]["input_ppm_rgb_byte_length_matches_dimensions"]
        )
        self.assertTrue(
            complete_summary["checks"][
                "input_ppm_packed_rgb_length_matches_dimensions"
            ]
        )
        self.assertTrue(
            complete_summary["checks"]["input_ppm_packed_rgb_sha256_present"]
        )
        self.assertTrue(complete_summary["checks"]["input_ppm_non_flat"])
        self.assertTrue(complete_summary["checks"]["input_ppm_has_color_pixels"])

    def test_hardware_summary_requires_valid_capture_config(self) -> None:
        invalid = {
            "capture_config": {
                "max_output_bytes": 0,
                "timeout_seconds": 30.0,
            }
        }
        valid = {
            "capture_config": {
                "max_output_bytes": 1024,
                "timeout_seconds": None,
            }
        }

        invalid_summary = hjpeg_host.hardware_run_summary_record(invalid)
        valid_summary = hjpeg_host.hardware_run_summary_record(valid)

        self.assertFalse(invalid_summary["evidence_present"]["capture_config"])
        self.assertFalse(
            invalid_summary["checks"]["capture_max_output_bytes_positive"]
        )
        self.assertTrue(invalid_summary["checks"]["capture_timeout_valid"])
        self.assertTrue(valid_summary["evidence_present"]["capture_config"])
        self.assertTrue(valid_summary["checks"]["capture_max_output_bytes_positive"])
        self.assertTrue(valid_summary["checks"]["capture_timeout_valid"])

    def test_hardware_summary_requires_valid_axi_lite_target(self) -> None:
        invalid = {
            "axi_lite": {
                "device": "",
                "base_addr": 16,
                "base_addr_hex": "0x20",
            }
        }
        valid = {
            "axi_lite": {
                "device": "/dev/mem",
                "base_addr": 16,
                "base_addr_hex": "0x10",
            }
        }

        invalid_summary = hjpeg_host.hardware_run_summary_record(invalid)
        valid_summary = hjpeg_host.hardware_run_summary_record(valid)

        self.assertFalse(invalid_summary["evidence_present"]["axi_lite"])
        self.assertFalse(invalid_summary["checks"]["axi_lite_device_present"])
        self.assertTrue(invalid_summary["checks"]["axi_lite_base_addr_nonnegative"])
        self.assertFalse(invalid_summary["checks"]["axi_lite_base_addr_hex_matches"])
        self.assertTrue(valid_summary["evidence_present"]["axi_lite"])
        self.assertTrue(valid_summary["checks"]["axi_lite_device_present"])
        self.assertTrue(valid_summary["checks"]["axi_lite_base_addr_nonnegative"])
        self.assertTrue(valid_summary["checks"]["axi_lite_base_addr_hex_matches"])

    def test_hardware_summary_requires_valid_encoder_config(self) -> None:
        invalid = {
            "encoder_config": {
                "width": 2,
                "height": 1,
                "max_width": 1,
                "max_height": 1,
                "quality": 101,
                "restart_interval": 0,
                "chroma_subsample": True,
                "emit_jfif": True,
                "clear_error": False,
                "control": 0,
                "control_hex": "0x00000000",
            }
        }
        valid = {
            "encoder_config": hjpeg_host.encoder_config_record(
                width=2,
                height=1,
                quality=80,
                restart_interval=2,
                chroma_subsample=True,
                emit_jfif=True,
                clear_error=False,
            )
        }

        invalid_summary = hjpeg_host.hardware_run_summary_record(invalid)
        valid_summary = hjpeg_host.hardware_run_summary_record(valid)

        self.assertFalse(invalid_summary["evidence_present"]["encoder_config"])
        self.assertFalse(invalid_summary["checks"]["encoder_dimensions_supported"])
        self.assertFalse(invalid_summary["checks"]["encoder_quality_valid"])
        self.assertTrue(invalid_summary["checks"]["encoder_restart_interval_valid"])
        self.assertTrue(invalid_summary["checks"]["encoder_flags_valid"])
        self.assertFalse(invalid_summary["checks"]["encoder_control_matches_flags"])
        self.assertTrue(valid_summary["evidence_present"]["encoder_config"])
        self.assertTrue(valid_summary["checks"]["encoder_dimensions_supported"])
        self.assertTrue(valid_summary["checks"]["encoder_quality_valid"])
        self.assertTrue(valid_summary["checks"]["encoder_restart_interval_valid"])
        self.assertTrue(valid_summary["checks"]["encoder_flags_valid"])
        self.assertTrue(valid_summary["checks"]["encoder_control_matches_flags"])

    def test_hardware_summary_requires_valid_validation_expectations(self) -> None:
        invalid = {
            "validation_expectations": {
                "width": 2,
                "height": 1,
                "expected_sample_precision": 12,
                "expected_component_count": 3,
                "expected_scan_data_min_bytes": 0,
                "expected_marker_order": {"through_sos": [], "terminal_marker": "SOS"},
                "expected_quantization_tables": [0],
                "expected_quantization_table_order": [0],
                "expected_huffman_table_order": [],
                "expected_sos_spectral": {
                    "spectral_start": 1,
                    "spectral_end": 63,
                    "successive_approximation": 0,
                },
                "require_standard_huffman": False,
            }
        }
        valid = {
            "validation_expectations": hjpeg_host.validation_expectations_record(
                minimal_jpeg_info(width=2, height=1),
                width=2,
                height=1,
                restart_interval=0,
                check_chroma_mode=True,
                chroma_subsample=False,
                expect_jfif="present",
                quality=80,
                require_standard_huffman=True,
            )
        }

        invalid_summary = hjpeg_host.hardware_run_summary_record(invalid)
        valid_summary = hjpeg_host.hardware_run_summary_record(valid)

        self.assertFalse(
            invalid_summary["evidence_present"]["validation_expectations"]
        )
        self.assertFalse(invalid_summary["checks"]["validation_baseline_shape"])
        self.assertFalse(invalid_summary["checks"]["validation_marker_order_present"])
        self.assertFalse(invalid_summary["checks"]["validation_table_order_present"])
        self.assertFalse(
            invalid_summary["checks"]["validation_sos_spectral_baseline"]
        )
        self.assertFalse(
            invalid_summary["checks"]["validation_requires_standard_huffman"]
        )
        self.assertTrue(valid_summary["evidence_present"]["validation_expectations"])
        self.assertTrue(valid_summary["checks"]["validation_baseline_shape"])
        self.assertTrue(valid_summary["checks"]["validation_marker_order_present"])
        self.assertTrue(valid_summary["checks"]["validation_table_order_present"])
        self.assertTrue(valid_summary["checks"]["validation_sos_spectral_baseline"])
        self.assertTrue(valid_summary["checks"]["validation_requires_standard_huffman"])

    def test_run_evidence_record_summarizes_status_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jpeg = root / "output.jpg"
            jpeg.write_bytes(minimal_jpeg(width=2, height=1))

            status_checks = [
                {
                    "context": "after configuration",
                    "status": 0,
                    "text": "idle",
                    "busy": False,
                    "protocol_error": False,
                },
                {
                    "context": "before transfer",
                    "status": 0,
                    "text": "idle",
                    "busy": False,
                    "protocol_error": False,
                },
                {
                    "context": "after transfer",
                    "status": 0,
                    "text": "idle",
                    "busy": False,
                    "protocol_error": False,
                },
            ]
            record = hjpeg_host.run_evidence_record(
                jpeg,
                minimal_jpeg_info(width=2, height=1),
                status_checks=status_checks,
            )

            self.assertEqual(record["status_checks"], status_checks)
            self.assertEqual(record["status_check_count"], 3)
            self.assertEqual(
                record["status_check_contexts"],
                ["after configuration", "before transfer", "after transfer"],
            )
            self.assertEqual(
                record["expected_status_check_contexts"],
                ["after configuration", "before transfer", "after transfer"],
            )
            self.assertTrue(record["status_check_contexts_match_expected"])
            self.assertTrue(record["status_checks_all_idle"])
            self.assertFalse(record["status_checks_any_protocol_error"])
            self.assertFalse(record["status_checks_any_busy"])
            self.assertEqual(
                record["hardware_run_summary"]["checks"],
                {
                    "jpeg_validation_passed": True,
                    "jpeg_byte_length_positive": True,
                    "jpeg_scan_data_bytes_positive": True,
                    "jpeg_sha256_present": True,
                    "jpeg_scan_data_sha256_present": True,
                    "jpeg_marker_sequence_starts_with_soi": True,
                    "jpeg_marker_sequence_ends_with_eoi": True,
                    "status_check_contexts_match_expected": True,
                    "status_checks_all_idle": True,
                    "status_checks_no_protocol_error": True,
                    "status_checks_no_busy": True,
                },
            )
            self.assertTrue(record["hardware_run_summary"]["all_recorded_checks_passed"])
            self.assertTrue(
                record["hardware_run_summary"]["evidence_present"]["status_checks"]
            )
            self.assertFalse(record["hardware_run_summary"]["evidence_present"]["input_ppm"])
            self.assertFalse(record["hardware_run_summary"]["evidence_present"]["decoder"])
            self.assertFalse(record["hardware_run_summary"]["complete_hardware_run_evidence"])

            faulted = hjpeg_host.run_evidence_record(
                jpeg,
                minimal_jpeg_info(width=2, height=1),
                status_checks=[
                    {"context": "before transfer", "text": "idle", "busy": False},
                    {
                        "context": "after transfer",
                        "text": "protocol_error",
                        "busy": True,
                        "protocol_error": True,
                    },
                ],
            )
            self.assertFalse(faulted["status_checks_all_idle"])
            self.assertTrue(faulted["status_checks_any_protocol_error"])
            self.assertTrue(faulted["status_checks_any_busy"])
            self.assertFalse(faulted["status_check_contexts_match_expected"])
            self.assertFalse(faulted["hardware_run_summary"]["all_recorded_checks_passed"])
            self.assertFalse(
                faulted["hardware_run_summary"]["evidence_present"]["status_checks"]
            )
            self.assertFalse(
                faulted["hardware_run_summary"]["checks"][
                    "status_check_contexts_match_expected"
                ]
            )
            self.assertFalse(
                faulted["hardware_run_summary"]["checks"]["status_checks_all_idle"]
            )
            self.assertFalse(
                faulted["hardware_run_summary"]["checks"][
                    "status_checks_no_protocol_error"
                ]
            )
            self.assertFalse(
                faulted["hardware_run_summary"]["checks"]["status_checks_no_busy"]
            )

    def test_run_evidence_record_reports_transfer_rates_for_positive_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jpeg = root / "output.jpg"
            input_rgb = root / "input.rgb"
            jpeg.write_bytes(minimal_jpeg(width=2, height=1))
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 6, 0]))

            record = hjpeg_host.run_evidence_record(
                jpeg,
                minimal_jpeg_info(width=2, height=1),
                input_info=hjpeg_host.file_info(input_rgb, input_rgb.read_bytes()),
                transfer_elapsed_seconds=2.0,
            )

            self.assertEqual(record["transfer_elapsed_seconds"], 2.0)
            self.assertEqual(record["host_transfer_rates"]["input_rgb_bytes_per_second"], 4.0)
            self.assertEqual(
                record["host_transfer_rates"]["output_jpeg_bytes_per_second"],
                len(minimal_jpeg(width=2, height=1)) / 2.0,
            )
            self.assertTrue(
                record["hardware_run_summary"]["evidence_present"]["transfer_timing"]
            )
            self.assertTrue(
                record["hardware_run_summary"]["checks"][
                    "transfer_elapsed_seconds_positive"
                ]
            )
            self.assertTrue(
                record["hardware_run_summary"]["checks"]["host_transfer_rates_present"]
            )

    def test_run_evidence_record_requires_decoder_result_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "output.jpg"
            jpeg.write_bytes(minimal_jpeg(width=2, height=1))

            record = hjpeg_host.run_evidence_record(
                jpeg,
                minimal_jpeg_info(width=2, height=1),
                decoder_passed=True,
            )

            self.assertFalse(record["hardware_run_summary"]["evidence_present"]["decoder"])
            self.assertFalse(record["hardware_run_summary"]["all_recorded_checks_passed"])
            self.assertTrue(record["hardware_run_summary"]["checks"]["decoder_passed"])
            self.assertFalse(
                record["hardware_run_summary"]["checks"]["decoder_returncode_zero"]
            )
            self.assertFalse(
                record["hardware_run_summary"]["checks"]["decoder_argv_present"]
            )
            self.assertFalse(
                record["hardware_run_summary"]["checks"]["decoder_output_not_truncated"]
            )

    def test_hardware_summary_requires_output_hash_and_scan_evidence(self) -> None:
        record = {
            "byte_length": 0,
            "sha256": "",
            "scan_data_bytes": 0,
            "scan_data_sha256": "",
        }

        summary = hjpeg_host.hardware_run_summary_record(record)

        self.assertFalse(summary["evidence_present"]["jpeg_output"])
        self.assertFalse(summary["all_recorded_checks_passed"])
        self.assertFalse(summary["checks"]["jpeg_byte_length_positive"])
        self.assertFalse(summary["checks"]["jpeg_scan_data_bytes_positive"])
        self.assertFalse(summary["checks"]["jpeg_sha256_present"])
        self.assertFalse(summary["checks"]["jpeg_scan_data_sha256_present"])
        self.assertFalse(summary["checks"]["jpeg_marker_sequence_starts_with_soi"])
        self.assertFalse(summary["checks"]["jpeg_marker_sequence_ends_with_eoi"])

    def test_hardware_summary_checks_frame_consistency(self) -> None:
        record = {
            "width": 2,
            "height": 1,
            "byte_length": 16,
            "sha256": "0" * 64,
            "scan_data_bytes": 1,
            "scan_data_sha256": "1" * 64,
            "marker_sequence": ["SOI", "SOS", "EOI"],
            "encoder_config": {"width": 3, "height": 1},
            "validation_expectations": {"width": 2, "height": 2},
            "input_ppm": {"width": 2, "height": 3},
            "input_rgb": {"expected_byte_length": 12},
        }

        summary = hjpeg_host.hardware_run_summary_record(record)

        self.assertFalse(summary["all_recorded_checks_passed"])
        self.assertFalse(
            summary["checks"]["encoder_config_matches_jpeg_dimensions"]
        )
        self.assertFalse(
            summary["checks"]["validation_expectations_match_jpeg_dimensions"]
        )
        self.assertFalse(summary["checks"]["input_ppm_dimensions_match_jpeg"])
        self.assertFalse(
            summary["checks"]["input_rgb_expected_length_matches_dimensions"]
        )

    def test_hardware_summary_requires_soi_eoi_marker_sequence(self) -> None:
        record = {
            "byte_length": 16,
            "sha256": "0" * 64,
            "scan_data_bytes": 1,
            "scan_data_sha256": "1" * 64,
            "marker_sequence": ["APP0", "SOS"],
        }

        summary = hjpeg_host.hardware_run_summary_record(record)

        self.assertFalse(summary["evidence_present"]["jpeg_output"])
        self.assertFalse(summary["all_recorded_checks_passed"])
        self.assertFalse(summary["checks"]["jpeg_marker_sequence_starts_with_soi"])
        self.assertFalse(summary["checks"]["jpeg_marker_sequence_ends_with_eoi"])

    def test_run_evidence_record_rejects_invalid_elapsed_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "output.jpg"
            jpeg.write_bytes(minimal_jpeg(width=2, height=1))

            for elapsed in (-0.001, float("nan"), float("inf"), float("-inf")):
                with self.subTest(elapsed=elapsed):
                    with self.assertRaisesRegex(ValueError, "transfer elapsed seconds"):
                        hjpeg_host.run_evidence_record(
                            jpeg,
                            minimal_jpeg_info(width=2, height=1),
                            transfer_elapsed_seconds=elapsed,
                        )

    def test_capture_config_record_rejects_invalid_limits(self) -> None:
        for max_output_bytes in (0, -1):
            with self.subTest(max_output_bytes=max_output_bytes):
                with self.assertRaisesRegex(ValueError, "max output bytes"):
                    hjpeg_host.capture_config_record(max_output_bytes, 1.0)

        for timeout_seconds in (0, -1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(timeout_seconds=timeout_seconds):
                with self.assertRaisesRegex(ValueError, "timeout seconds"):
                    hjpeg_host.capture_config_record(1024, timeout_seconds)

    def test_capture_config_record_allows_unbounded_timeout(self) -> None:
        self.assertEqual(
            hjpeg_host.capture_config_record(1024, None),
            {"max_output_bytes": 1024, "timeout_seconds": None},
        )

    def test_validate_jpeg_cli_rejects_invalid_decoder_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            jpeg.write_bytes(minimal_jpeg(width=2, height=1))

            for timeout_seconds in ("0", "-1", "nan", "inf", "-inf"):
                with self.subTest(timeout_seconds=timeout_seconds):
                    with self.assertRaises(SystemExit):
                        hjpeg_host.main(
                            [
                                "validate-jpeg",
                                str(jpeg),
                                "--width",
                                "2",
                                "--height",
                                "1",
                                f"--decoder-timeout-seconds={timeout_seconds}",
                            ]
                        )

    def test_run_stream_devices_cli_rejects_invalid_capture_args(self) -> None:
        common_args = [
            "run-stream-devices",
            "--base-addr",
            "0",
            "--tx-device",
            "tx.dev",
            "--rx-device",
            "rx.dev",
            "--input-rgb",
            "input.rgb",
            "--output-jpeg",
            "output.jpg",
            "--width",
            "2",
            "--height",
            "1",
        ]
        invalid_cases = [
            ("--max-output-bytes", "0"),
            ("--max-output-bytes", "-1"),
            ("--timeout-seconds", "0"),
            ("--timeout-seconds", "-1"),
            ("--timeout-seconds", "nan"),
            ("--timeout-seconds", "inf"),
            ("--timeout-seconds", "-inf"),
            ("--decoder-timeout-seconds", "0"),
            ("--decoder-timeout-seconds", "-1"),
            ("--decoder-timeout-seconds", "nan"),
            ("--decoder-timeout-seconds", "inf"),
            ("--decoder-timeout-seconds", "-inf"),
            ("--quality", "0"),
            ("--quality", "101"),
            ("--restart-interval", "-1"),
            ("--restart-interval", "65536"),
        ]

        for option, value in invalid_cases:
            with self.subTest(option=option, value=value):
                with self.assertRaises(SystemExit):
                    hjpeg_host.main([*common_args, f"{option}={value}"])

    def test_validate_jpeg_cli_rejects_invalid_restart_interval(self) -> None:
        for restart_interval in ("-1", "65536"):
            with self.subTest(restart_interval=restart_interval):
                with self.assertRaises(SystemExit):
                    hjpeg_host.main(
                        [
                            "validate-jpeg",
                            "missing.jpg",
                            "--width",
                            "2",
                            "--height",
                            "1",
                            f"--restart-interval={restart_interval}",
                        ]
                    )

    def test_validate_jpeg_cli_rejects_invalid_quality(self) -> None:
        for quality in ("0", "101"):
            with self.subTest(quality=quality):
                with self.assertRaises(SystemExit):
                    hjpeg_host.main(
                        [
                            "validate-jpeg",
                            "missing.jpg",
                            "--width",
                            "2",
                            "--height",
                            "1",
                            f"--quality={quality}",
                        ]
                    )

    def test_validate_jpeg_rejects_invalid_expected_arguments_before_io(self) -> None:
        missing = Path("missing.jpg")
        cases = (
            {
                "expected_width": 0,
                "expected_height": 1,
                "message": "dimensions",
            },
            {
                "expected_width": 1,
                "expected_height": 0,
                "message": "dimensions",
            },
            {
                "expected_width": 1,
                "expected_height": 1,
                "expected_restart_interval": -1,
                "message": "restart interval",
            },
            {
                "expected_width": 1,
                "expected_height": 1,
                "expected_restart_interval": 0x10000,
                "message": "restart interval",
            },
            {
                "expected_width": 1,
                "expected_height": 1,
                "expected_quality": 0,
                "message": "quality",
            },
            {
                "expected_width": 1,
                "expected_height": 1,
                "expected_quality": 101,
                "message": "quality",
            },
        )

        for case in cases:
            with self.subTest(case=case):
                message = str(case["message"])
                kwargs = {key: value for key, value in case.items() if key != "message"}
                with self.assertRaisesRegex(ValueError, message):
                    hjpeg_host.validate_jpeg(missing, **kwargs)

    def test_cli_rejects_invalid_frame_dimensions_and_limits(self) -> None:
        invalid_commands = [
            ["make-test-ppm", "out.ppm", "--width=0", "--height=1"],
            ["make-test-ppm", "out.ppm", "--width=1", "--height=-1"],
            ["make-test-ppm", "out.ppm", "--width=1", "--height=1", "--max-width=0"],
            ["make-test-ppm", "out.ppm", "--width=1", "--height=1", "--max-height=-1"],
            ["pack-ppm", "missing.ppm", "out.rgb", "--max-width=0"],
            ["pack-ppm", "missing.ppm", "out.rgb", "--max-height=-1"],
            ["validate-jpeg", "missing.jpg", "--width=0", "--height=1"],
            ["validate-jpeg", "missing.jpg", "--width=1", "--height=-1"],
            ["config", "--base-addr=0", "--width=0", "--height=1"],
            ["config", "--base-addr=0", "--width=1", "--height=-1"],
            ["config", "--base-addr=0", "--width=1", "--height=1", "--max-width=0"],
            ["config", "--base-addr=0", "--width=1", "--height=1", "--max-height=-1"],
            [
                "run-stream-devices",
                "--base-addr=0",
                "--tx-device=tx.dev",
                "--rx-device=rx.dev",
                "--input-rgb=input.rgb",
                "--output-jpeg=output.jpg",
                "--width=0",
                "--height=1",
            ],
            [
                "run-stream-devices",
                "--base-addr=0",
                "--tx-device=tx.dev",
                "--rx-device=rx.dev",
                "--input-rgb=input.rgb",
                "--output-jpeg=output.jpg",
                "--width=1",
                "--height=-1",
            ],
            [
                "run-stream-devices",
                "--base-addr=0",
                "--tx-device=tx.dev",
                "--rx-device=rx.dev",
                "--input-rgb=input.rgb",
                "--output-jpeg=output.jpg",
                "--width=1",
                "--height=1",
                "--max-width=0",
            ],
            [
                "run-stream-devices",
                "--base-addr=0",
                "--tx-device=tx.dev",
                "--rx-device=rx.dev",
                "--input-rgb=input.rgb",
                "--output-jpeg=output.jpg",
                "--width=1",
                "--height=1",
                "--max-height=-1",
            ],
        ]

        for argv in invalid_commands:
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    hjpeg_host.main(argv)

    def test_cli_rejects_negative_axi_lite_base_address(self) -> None:
        invalid_commands = [
            ["config", "--base-addr=-1", "--width=1", "--height=1"],
            ["status", "--base-addr=-1"],
            ["clear-error", "--base-addr=-1"],
            [
                "run-stream-devices",
                "--base-addr=-1",
                "--tx-device=tx.dev",
                "--rx-device=rx.dev",
                "--input-rgb=input.rgb",
                "--output-jpeg=output.jpg",
                "--width=1",
                "--height=1",
            ],
        ]

        for argv in invalid_commands:
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    hjpeg_host.main(argv)

    def test_validate_jpeg_json_records_decoder_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13))
            command = (
                f'"{sys.executable}" -c "import sys; '
                f'print(\'decoded 17x13\'); '
                f'print(\'decoder warning\', file=sys.stderr); '
                f'open(sys.argv[1], \'rb\').read(2)"'
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    hjpeg_host.main(
                        [
                            "validate-jpeg",
                            str(jpeg),
                            "--width",
                            "17",
                            "--height",
                            "13",
                            "--decoder-command",
                            command,
                            "--decoder-timeout-seconds",
                            "2.5",
                            "--json",
                        ]
                    ),
                    0,
                )

            record = json.loads(stdout.getvalue())
            self.assertTrue(record["decoder_passed"])
            self.assertEqual(record["decoder_command"], command)
            self.assertEqual(
                record["decoder_argv"],
                hjpeg_host.decoder_command_argv(jpeg, command),
            )
            self.assertEqual(record["decoder_timeout_seconds"], 2.5)
            self.assertEqual(record["decoder_returncode"], 0)
            self.assertEqual(record["decoder_stdout"], "decoded 17x13\n")
            self.assertEqual(record["decoder_stderr"], "decoder warning\n")
            self.assertGreaterEqual(record["decoder_elapsed_seconds"], 0.0)
            self.assertEqual(record["decoder_stdout_chars"], len("decoded 17x13\n"))
            self.assertEqual(record["decoder_stderr_chars"], len("decoder warning\n"))
            self.assertEqual(
                record["decoder_output_capture_chars"],
                hjpeg_host.DECODER_OUTPUT_CAPTURE_CHARS,
            )
            self.assertFalse(record["decoder_stdout_truncated"])
            self.assertFalse(record["decoder_stderr_truncated"])

    def test_validate_jpeg_can_check_expected_restart_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jpeg = root / "restart.jpg"
            missing_rst = root / "missing-rst.jpg"
            no_restart = root / "no-restart.jpg"
            jpeg.write_bytes(
                with_dri_segment(
                    with_scan_restart_marker(minimal_jpeg(width=17, height=13)),
                    4,
                )
            )
            missing_rst.write_bytes(with_dri_segment(minimal_jpeg(width=17, height=13), 4))
            no_restart.write_bytes(minimal_jpeg(width=17, height=13))

            info = hjpeg_host.validate_jpeg(
                jpeg,
                expected_width=17,
                expected_height=13,
                expected_restart_interval=4,
            )
            self.assertEqual(info.restart_interval, 4)
            self.assertEqual(info.restart_markers, 1)
            self.assertEqual(info.restart_marker_sequence, (0,))
            self.assertEqual(hjpeg_host.expected_restart_marker_count(info, 4), 1)

            with self.assertRaisesRegex(ValueError, "expected 3"):
                hjpeg_host.validate_jpeg(
                    jpeg,
                    expected_width=17,
                    expected_height=13,
                    expected_restart_interval=3,
                )
            with self.assertRaisesRegex(ValueError, "restart interval 0"):
                hjpeg_host.validate_jpeg(
                    jpeg,
                    expected_width=17,
                    expected_height=13,
                    expected_restart_interval=0,
                )
            with self.assertRaisesRegex(ValueError, "restart marker count"):
                hjpeg_host.validate_jpeg(
                    missing_rst,
                    expected_width=17,
                    expected_height=13,
                    expected_restart_interval=4,
                )
            with self.assertRaisesRegex(ValueError, "DRI segment count"):
                hjpeg_host.validate_jpeg(
                    no_restart,
                    expected_width=17,
                    expected_height=13,
                    expected_restart_interval=4,
                )

    def test_validate_jpeg_cli_can_check_restart_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "restart.jpg"
            jpeg.write_bytes(
                with_dri_segment(
                    with_scan_restart_marker(minimal_jpeg(width=17, height=13)),
                    4,
                )
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    hjpeg_host.main(
                        [
                            "validate-jpeg",
                            str(jpeg),
                            "--width",
                            "17",
                            "--height",
                            "13",
                            "--restart-interval",
                            "4",
                            "--json",
                        ]
                    ),
                    0,
                )
            record = json.loads(stdout.getvalue())
            self.assertEqual(record["restart_interval"], 4)
            self.assertEqual(record["validation_expectations"]["expected_restart_markers"], 1)

    def test_validate_jpeg_can_check_expected_chroma_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jpeg_444 = root / "444.jpg"
            jpeg_420 = root / "420.jpg"
            jpeg_444.write_bytes(minimal_jpeg(width=17, height=13))
            jpeg_420.write_bytes(minimal_jpeg(width=17, height=13, chroma_subsample=True))

            info_444 = hjpeg_host.validate_jpeg(
                jpeg_444,
                expected_width=17,
                expected_height=13,
                expected_chroma_subsample=False,
            )
            info_420 = hjpeg_host.validate_jpeg(
                jpeg_420,
                expected_width=17,
                expected_height=13,
                expected_chroma_subsample=True,
            )
            self.assertEqual(hjpeg_host.jpeg_chroma_mode(info_444), "4:4:4")
            self.assertEqual(hjpeg_host.jpeg_chroma_mode(info_420), "4:2:0")
            self.assertEqual(info_444.mcu_count, 6)
            self.assertEqual(info_420.mcu_count, 2)

            with self.assertRaisesRegex(ValueError, "expected 4:2:0"):
                hjpeg_host.validate_jpeg(
                    jpeg_444,
                    expected_width=17,
                    expected_height=13,
                    expected_chroma_subsample=True,
                )
            with self.assertRaisesRegex(ValueError, "expected 4:4:4"):
                hjpeg_host.validate_jpeg(
                    jpeg_420,
                    expected_width=17,
                    expected_height=13,
                    expected_chroma_subsample=False,
                )

    def test_validate_jpeg_cli_can_check_chroma_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "420.jpg"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13, chroma_subsample=True))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    hjpeg_host.main(
                        [
                            "validate-jpeg",
                            str(jpeg),
                            "--width",
                            "17",
                            "--height",
                            "13",
                            "--chroma-subsample",
                            "--check-chroma-mode",
                            "--json",
                        ]
                    ),
                    0,
                )
            self.assertEqual(json.loads(stdout.getvalue())["chroma_mode"], "4:2:0")

    def test_validate_jpeg_can_check_expected_jfif_presence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with_jfif = root / "with-jfif.jpg"
            with_app0 = root / "with-app0.jpg"
            without_jfif = root / "without-jfif.jpg"
            with_jfif.write_bytes(minimal_jpeg(width=17, height=13))
            with_app0.write_bytes(
                minimal_jpeg(width=17, height=13).replace(b"JFIF\x00", b"APP0\x00", 1)
            )
            without_jfif.write_bytes(without_segment(minimal_jpeg(width=17, height=13), b"\xff\xe0"))

            info_with_jfif = hjpeg_host.validate_jpeg(
                with_jfif,
                expected_width=17,
                expected_height=13,
                expected_emit_jfif=True,
            )
            info_without_jfif = hjpeg_host.validate_jpeg(
                without_jfif,
                expected_width=17,
                expected_height=13,
                expected_emit_jfif=False,
            )
            self.assertEqual(
                info_with_jfif.jfif_app0,
                hjpeg_host.JfifApp0Info(1, 1, 0, 1, 1, 0, 0),
            )
            self.assertIsNone(info_without_jfif.jfif_app0)
            with self.assertRaisesRegex(ValueError, "APP0 segment is not a JFIF"):
                hjpeg_host.validate_jpeg(
                    with_app0,
                    expected_width=17,
                    expected_height=13,
                    expected_emit_jfif=False,
                )

            with self.assertRaisesRegex(ValueError, "expected"):
                hjpeg_host.validate_jpeg(
                    without_jfif,
                    expected_width=17,
                    expected_height=13,
                    expected_emit_jfif=True,
                )
            with self.assertRaisesRegex(ValueError, "APP0 segment is not a JFIF"):
                hjpeg_host.validate_jpeg(
                    with_app0,
                    expected_width=17,
                    expected_height=13,
                    expected_emit_jfif=True,
                )
            with self.assertRaisesRegex(ValueError, "disabled"):
                hjpeg_host.validate_jpeg(
                    with_jfif,
                    expected_width=17,
                    expected_height=13,
                    expected_emit_jfif=False,
                )

    def test_validate_jpeg_cli_can_check_jfif_presence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    hjpeg_host.main(
                        [
                            "validate-jpeg",
                            str(jpeg),
                            "--width",
                            "17",
                            "--height",
                            "13",
                            "--expect-jfif",
                            "present",
                            "--json",
                        ]
                    ),
                    0,
                )
            record = json.loads(stdout.getvalue())
            self.assertEqual(record["app0_segments"], 1)
            self.assertEqual(record["jfif_app0_segments"], 1)

    def test_decoder_command_supports_placeholder_and_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            marker = Path(tmp) / "placeholder-ran.txt"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13))

            command = (
                f'"{sys.executable}" -c "import pathlib, sys; '
                f'pathlib.Path(r\'{marker}\').write_text(pathlib.Path(sys.argv[1].split(\'=\', 1)[1]).name)" file={{jpeg}}'
            )
            result = hjpeg_host.run_decoder_command(jpeg, command)
            self.assertEqual(marker.read_text(), "out.jpg")
            self.assertEqual(
                result.argv,
                tuple(hjpeg_host.decoder_command_argv(jpeg, command)),
            )
            self.assertEqual(result.returncode, 0)
            self.assertGreaterEqual(result.elapsed_seconds, 0.0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            self.assertFalse(result.stdout_truncated)
            self.assertFalse(result.stderr_truncated)

            with self.assertRaisesRegex(RuntimeError, "decoder command failed"):
                hjpeg_host.run_decoder_command(
                    jpeg,
                    f'"{sys.executable}" -c "import sys; print(\'bad\', file=sys.stderr); sys.exit(3)"',
                )

    def test_decoder_command_output_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13))
            stdout_text = "o" * (hjpeg_host.DECODER_OUTPUT_CAPTURE_CHARS + 3)
            stderr_text = "e" * (hjpeg_host.DECODER_OUTPUT_CAPTURE_CHARS + 5)

            result = hjpeg_host.run_decoder_command(
                jpeg,
                f'"{sys.executable}" -c "import sys; '
                f'sys.stdout.write(\'{stdout_text}\'); '
                f'sys.stderr.write(\'{stderr_text}\')"',
            )

            self.assertEqual(len(result.stdout), hjpeg_host.DECODER_OUTPUT_CAPTURE_CHARS)
            self.assertEqual(len(result.stderr), hjpeg_host.DECODER_OUTPUT_CAPTURE_CHARS)
            self.assertGreaterEqual(result.elapsed_seconds, 0.0)
            self.assertEqual(result.stdout_chars, hjpeg_host.DECODER_OUTPUT_CAPTURE_CHARS)
            self.assertEqual(result.stderr_chars, hjpeg_host.DECODER_OUTPUT_CAPTURE_CHARS)
            self.assertEqual(
                result.output_capture_chars,
                hjpeg_host.DECODER_OUTPUT_CAPTURE_CHARS,
            )
            self.assertTrue(result.stdout_truncated)
            self.assertTrue(result.stderr_truncated)

    def test_decoder_command_reports_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13))

            with self.assertRaisesRegex(RuntimeError, "timed out after 0.1 seconds"):
                hjpeg_host.run_decoder_command(
                    jpeg,
                    f'"{sys.executable}" -c "import time; time.sleep(5)"',
                    timeout_seconds=0.1,
                )
            for timeout_seconds in (0, -1, float("nan"), float("inf"), float("-inf")):
                with self.subTest(timeout_seconds=timeout_seconds):
                    with self.assertRaisesRegex(ValueError, "decoder timeout"):
                        hjpeg_host.run_decoder_command(
                            jpeg,
                            f'"{sys.executable}" -c "pass"',
                            timeout_seconds=timeout_seconds,
                        )

    def test_decoder_command_argv_appends_or_replaces_jpeg_path(self) -> None:
        jpeg = Path("captured output.jpg")

        self.assertEqual(
            hjpeg_host.decoder_command_argv(jpeg, "decoder --check"),
            ["decoder", "--check", str(jpeg)],
        )
        self.assertEqual(
            hjpeg_host.decoder_command_argv(jpeg, "decoder --input={jpeg}"),
            ["decoder", f"--input={jpeg}"],
        )

    def test_validate_jpeg_requires_scan_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header_only = root / "header-only.jpg"
            empty_scan = root / "empty-scan.jpg"
            header_only.write_bytes(header_only_jpeg(width=17, height=13))
            empty_scan.write_bytes(minimal_jpeg(width=17, height=13).replace(b"\x7f\xff\xd9", b"\xff\xd9"))

            with self.assertRaisesRegex(ValueError, "SOS"):
                hjpeg_host.validate_jpeg(header_only, expected_width=17, expected_height=13)
            with self.assertRaisesRegex(ValueError, "entropy-coded scan data"):
                hjpeg_host.validate_jpeg(empty_scan, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_trailing_data_after_eoi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "trailing.jpg"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13) + b"junk\xff\xd9")

            with self.assertRaisesRegex(ValueError, "trailing data after EOI"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_jpeg_info_records_stuffed_entropy_bytes(self) -> None:
        info = hjpeg_host.jpeg_info(with_stuffed_entropy_ff(minimal_jpeg(17, 13)))

        self.assertEqual(info.scan_data_bytes, 2)
        self.assertEqual(info.scan_data_sha256, hashlib.sha256(b"\x7f\xff").hexdigest())
        self.assertEqual(info.stuffed_ff_bytes, 1)
        self.assertEqual(info.marker_sequence[-2:], ("SOS", "EOI"))

    def test_validate_jpeg_rejects_unexpected_marker_after_scan_starts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "scan-marker.jpg"
            jpeg.write_bytes(with_unexpected_scan_marker(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "unexpected APP1 marker"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_unexpected_header_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "header-marker.jpg"
            jpeg.write_bytes(with_unexpected_header_marker(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "APP1 marker is not supported"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_non_jfif_app0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "non-jfif-app0.jpg"
            jpeg.write_bytes(with_non_jfif_app0(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "APP0 segment is not a JFIF"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_malformed_jfif_app0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            short_jpeg = root / "short-jfif-app0.jpg"
            fields_jpeg = root / "nonstandard-jfif-app0.jpg"
            padded_jpeg = root / "padded-jfif-app0.jpg"
            short_jpeg.write_bytes(
                with_short_jfif_app0(minimal_jpeg(width=17, height=13))
            )
            fields_jpeg.write_bytes(
                with_nonstandard_jfif_app0_fields(minimal_jpeg(width=17, height=13))
            )
            padded_jpeg.write_bytes(
                with_padded_jfif_app0(minimal_jpeg(width=17, height=13))
            )

            with self.assertRaisesRegex(ValueError, "JFIF APP0 segment is too short"):
                hjpeg_host.validate_jpeg(
                    short_jpeg,
                    expected_width=17,
                    expected_height=13,
                )
            with self.assertRaisesRegex(ValueError, "JFIF APP0 fields"):
                hjpeg_host.validate_jpeg(
                    fields_jpeg,
                    expected_width=17,
                    expected_height=13,
                )
            with self.assertRaisesRegex(ValueError, "thumbnail size"):
                hjpeg_host.validate_jpeg(
                    padded_jpeg,
                    expected_width=17,
                    expected_height=13,
                )

    def test_validate_jpeg_rejects_duplicate_app0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "duplicate-app0.jpg"
            jpeg.write_bytes(with_duplicate_app0(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "APP0 segment count is 2"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_duplicate_frame_and_scan_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            duplicate_sof0 = root / "duplicate-sof0.jpg"
            duplicate_sos = root / "duplicate-sos.jpg"
            duplicate_sof0.write_bytes(with_duplicate_sof0(minimal_jpeg(width=17, height=13)))
            duplicate_sos.write_bytes(with_duplicate_sos(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "SOF0 segment count"):
                hjpeg_host.validate_jpeg(duplicate_sof0, expected_width=17, expected_height=13)
            with self.assertRaisesRegex(ValueError, "unexpected SOS marker"):
                hjpeg_host.validate_jpeg(duplicate_sos, expected_width=17, expected_height=13)

    def test_validate_jpeg_requires_quantization_and_huffman_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_dqt = root / "missing-dqt.jpg"
            missing_dht = root / "missing-dht.jpg"
            missing_dqt.write_bytes(without_segment(minimal_jpeg(width=17, height=13), b"\xff\xdb"))
            missing_dht.write_bytes(without_all_segments(minimal_jpeg(width=17, height=13), b"\xff\xc4"))

            with self.assertRaisesRegex(ValueError, "DQT"):
                hjpeg_host.validate_jpeg(missing_dqt, expected_width=17, expected_height=13)
            with self.assertRaisesRegex(ValueError, "DHT"):
                hjpeg_host.validate_jpeg(missing_dht, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_non_baseline_dqt_precision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "dqt16.jpg"
            jpeg.write_bytes(with_16bit_dqt(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "DQT table 0 has precision 1"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_zero_quantization_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "zero-dqt.jpg"
            jpeg.write_bytes(with_zero_dqt_value(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "DQT table 0 contains zero"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_nonstandard_quantization_table_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "extra-dqt.jpg"
            jpeg.write_bytes(with_extra_dqt(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "DQT table set"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_duplicate_quantization_table_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "duplicate-dqt.jpg"
            jpeg.write_bytes(with_duplicate_dqt(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "DQT table 0 is defined more than once"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_duplicate_quantization_table_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "duplicate-dqt-table.jpg"
            jpeg.write_bytes(
                with_duplicate_dqt_table_in_segment(minimal_jpeg(width=17, height=13))
            )

            with self.assertRaisesRegex(ValueError, "DQT table 0 is defined more than once"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_swapped_quantization_table_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "swapped-dqt.jpg"
            jpeg.write_bytes(with_dqt_segments_swapped(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "DQT table order"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_nonstandard_huffman_table_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "extra-dht.jpg"
            jpeg.write_bytes(with_extra_dht(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "DHT table set"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_duplicate_huffman_table_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "duplicate-dht.jpg"
            jpeg.write_bytes(with_duplicate_dht(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "DC DHT table 0 is defined more than once"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_duplicate_huffman_table_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "duplicate-dht-table.jpg"
            jpeg.write_bytes(
                with_duplicate_dht_table_in_segment(minimal_jpeg(width=17, height=13))
            )

            with self.assertRaisesRegex(ValueError, "DC DHT table 0 is defined more than once"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_swapped_huffman_table_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "swapped-dht.jpg"
            jpeg.write_bytes(
                with_first_two_dht_segments_swapped(minimal_jpeg(width=17, height=13))
            )

            with self.assertRaisesRegex(ValueError, "DHT table order"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_empty_huffman_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "empty-dht-table.jpg"
            jpeg.write_bytes(with_empty_first_dht_table(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "DC DHT table 0 has no symbols"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_oversized_huffman_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "oversized-dht-table.jpg"
            jpeg.write_bytes(
                with_oversized_first_dht_table(minimal_jpeg(width=17, height=13))
            )

            with self.assertRaisesRegex(ValueError, "DC DHT table 0 has 257 symbols"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_oversubscribed_huffman_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "oversubscribed-dht-table.jpg"
            jpeg.write_bytes(
                with_oversubscribed_first_dht_table(minimal_jpeg(width=17, height=13))
            )

            with self.assertRaisesRegex(ValueError, "DC DHT table 0 oversubscribes"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_invalid_dc_huffman_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "invalid-dc-dht-symbol.jpg"
            jpeg.write_bytes(with_invalid_dc_dht_symbol(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "DC DHT table 0 contains invalid category 12"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_invalid_ac_huffman_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "invalid-ac-dht-symbol.jpg"
            jpeg.write_bytes(with_invalid_ac_dht_symbol(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "AC DHT table 0 contains invalid zero-size run"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_can_require_standard_table_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            standard = root / "standard.jpg"
            bad_dqt = root / "bad-dqt-payload.jpg"
            bad_dht = root / "bad-dht-payload.jpg"
            standard.write_bytes(minimal_jpeg(width=17, height=13, quality=80))
            bad_dqt.write_bytes(
                with_mutated_first_dqt_payload(
                    minimal_jpeg(width=17, height=13, quality=80)
                )
            )
            bad_dht.write_bytes(
                with_mutated_first_dht_payload(
                    minimal_jpeg(width=17, height=13, quality=80)
                )
            )

            hjpeg_host.validate_jpeg(
                standard,
                expected_width=17,
                expected_height=13,
                expected_quality=80,
                require_standard_huffman=True,
            )
            with self.assertRaisesRegex(ValueError, "DQT table 0 payload"):
                hjpeg_host.validate_jpeg(
                    bad_dqt,
                    expected_width=17,
                    expected_height=13,
                    expected_quality=80,
                )
            with self.assertRaisesRegex(ValueError, "DC DHT table 0 payload"):
                hjpeg_host.validate_jpeg(
                    bad_dht,
                    expected_width=17,
                    expected_height=13,
                    require_standard_huffman=True,
                )

    def test_validate_jpeg_rejects_out_of_order_header_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            late_dqt = root / "late-dqt.jpg"
            late_sof0 = root / "late-sof0.jpg"
            late_dht = root / "late-dht.jpg"
            late_dqt.write_bytes(
                with_segment_moved_before(
                    minimal_jpeg(width=17, height=13),
                    b"\xff\xdb",
                    b"\xff\xc4",
                )
            )
            late_sof0.write_bytes(
                with_segment_moved_before(
                    minimal_jpeg(width=17, height=13),
                    b"\xff\xc0",
                    b"\xff\xda",
                )
            )
            late_dht.write_bytes(
                with_segment_moved_before(
                    with_dri_segment(minimal_jpeg(width=17, height=13), 4),
                    b"\xff\xdd",
                    b"\xff\xc4",
                )
            )

            with self.assertRaisesRegex(ValueError, "DQT marker appears out"):
                hjpeg_host.validate_jpeg(late_dqt, expected_width=17, expected_height=13)
            with self.assertRaisesRegex(ValueError, "SOF0 marker appears out"):
                hjpeg_host.validate_jpeg(late_sof0, expected_width=17, expected_height=13)
            with self.assertRaisesRegex(ValueError, "DHT marker appears out"):
                hjpeg_host.validate_jpeg(late_dht, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_restart_marker_before_scan_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "early-rst.jpg"
            jpeg.write_bytes(
                b"\xff\xd8\xff\xd0" + minimal_jpeg(width=17, height=13)[2:]
            )

            with self.assertRaisesRegex(ValueError, "restart marker appears before"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_missing_referenced_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing_referenced_dqt = root / "missing-referenced-dqt.jpg"
            missing_referenced_dht = root / "missing-referenced-dht.jpg"
            missing_referenced_dqt.write_bytes(
                minimal_jpeg(width=17, height=13).replace(
                    b"\x02\x11\x01\x03\x11\x01",
                    b"\x02\x11\x02\x03\x11\x02",
                )
            )
            missing_referenced_dht.write_bytes(
                minimal_jpeg(width=17, height=13).replace(
                    b"\x02\x11\x03\x11\x00\x3f",
                    b"\x02\x22\x03\x22\x00\x3f",
                )
            )

            with self.assertRaisesRegex(ValueError, "missing DQT table 2"):
                hjpeg_host.validate_jpeg(
                    missing_referenced_dqt,
                    expected_width=17,
                    expected_height=13,
                )
            with self.assertRaisesRegex(ValueError, "missing DC DHT table 2"):
                hjpeg_host.validate_jpeg(
                    missing_referenced_dht,
                    expected_width=17,
                    expected_height=13,
                )

    def test_validate_jpeg_rejects_nonstandard_sof0_quantization_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "sof0-quant-selectors.jpg"
            jpeg.write_bytes(
                with_sof0_quantization_tables(
                    minimal_jpeg(width=17, height=13),
                    (1, 1, 1),
                )
            )

            with self.assertRaisesRegex(ValueError, "SOF0 quantization table selectors"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_sos_component_shape_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            two_component_sos = root / "two-component-sos.jpg"
            duplicate_component_sos = root / "duplicate-component-sos.jpg"
            two_component_sos.write_bytes(with_two_component_sos(minimal_jpeg(width=17, height=13)))
            duplicate_component_sos.write_bytes(
                with_duplicate_sos_component(minimal_jpeg(width=17, height=13))
            )

            with self.assertRaisesRegex(ValueError, "SOS component count"):
                hjpeg_host.validate_jpeg(
                    two_component_sos,
                    expected_width=17,
                    expected_height=13,
                )
            with self.assertRaisesRegex(ValueError, "SOS component IDs must be unique"):
                hjpeg_host.validate_jpeg(
                    duplicate_component_sos,
                    expected_width=17,
                    expected_height=13,
                )

    def test_validate_jpeg_rejects_nonstandard_component_id_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nonstandard_sof0_ids = root / "nonstandard-sof0-ids.jpg"
            reordered_sos_ids = root / "reordered-sos-ids.jpg"
            nonstandard_sof0_ids.write_bytes(
                with_nonstandard_sof0_component_ids(minimal_jpeg(width=17, height=13))
            )
            reordered_sos_ids.write_bytes(
                with_reordered_sos_components(minimal_jpeg(width=17, height=13))
            )

            with self.assertRaisesRegex(ValueError, "SOF0 component IDs"):
                hjpeg_host.validate_jpeg(
                    nonstandard_sof0_ids,
                    expected_width=17,
                    expected_height=13,
                )
            with self.assertRaisesRegex(ValueError, "SOS component IDs"):
                hjpeg_host.validate_jpeg(
                    reordered_sos_ids,
                    expected_width=17,
                    expected_height=13,
                )

    def test_validate_jpeg_rejects_nonstandard_sos_table_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "sos-selectors.jpg"
            jpeg.write_bytes(
                with_sos_table_selectors(
                    minimal_jpeg(width=17, height=13),
                    (0x11, 0x11, 0x11),
                )
            )

            with self.assertRaisesRegex(ValueError, "SOS table selectors"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_unsupported_sampling_factors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "unsupported-sampling.jpg"
            jpeg.write_bytes(
                with_sof0_sampling_factors(
                    minimal_jpeg(width=17, height=13),
                    (0x21, 0x11, 0x11),
                )
            )

            with self.assertRaisesRegex(ValueError, "sampling factors"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_zero_sampling_factors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "zero-sampling.jpg"
            jpeg.write_bytes(
                with_sof0_sampling_factors(
                    minimal_jpeg(width=17, height=13),
                    (0x10, 0x11, 0x11),
                )
            )

            with self.assertRaisesRegex(ValueError, "zero sampling factor"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_non_baseline_sos_spectral_fields(self) -> None:
        cases = (
            ("bad-start.jpg", 1, 63, 0),
            ("bad-end.jpg", 0, 62, 0),
            ("bad-successive.jpg", 0, 63, 1),
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, start, end, successive in cases:
                jpeg = root / name
                jpeg.write_bytes(
                    with_sos_spectral_fields(
                        minimal_jpeg(width=17, height=13),
                        start,
                        end,
                        successive,
                    )
                )

                with self.assertRaisesRegex(ValueError, "spectral selection"):
                    hjpeg_host.validate_jpeg(
                        jpeg,
                        expected_width=17,
                        expected_height=13,
                    )

    def test_jpeg_info_counts_restart_markers_in_scan_data(self) -> None:
        jpeg = with_dri_segment(
            with_scan_restart_markers(minimal_jpeg(width=17, height=13), [0, 1]),
            restart_interval=2,
        )

        info = hjpeg_host.jpeg_info(jpeg)

        self.assertEqual(info.dri_segments, 1)
        self.assertEqual(info.restart_interval, 2)
        self.assertEqual(info.scan_data_bytes, 3)
        self.assertEqual(info.scan_data_sha256, hashlib.sha256(b"\x7f\x40\x41").hexdigest())
        self.assertEqual(info.restart_markers, 2)
        self.assertEqual(info.restart_marker_sequence, (0, 1))
        self.assertEqual(info.marker_sequence[-4:], ("SOS", "RST0", "RST1", "EOI"))

    def test_validate_jpeg_rejects_bad_restart_marker_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "bad-rst-sequence.jpg"
            jpeg.write_bytes(
                with_dri_segment(
                    with_scan_restart_markers(minimal_jpeg(width=17, height=13), [0, 2]),
                    restart_interval=1,
                )
            )

            with self.assertRaisesRegex(ValueError, "restart marker sequence"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_restart_markers_without_dri(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "rst-without-dri.jpg"
            jpeg.write_bytes(with_scan_restart_marker(minimal_jpeg(width=17, height=13)))

            with self.assertRaisesRegex(ValueError, "without a DRI segment"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)

    def test_validate_jpeg_rejects_malformed_dri_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jpeg = root / "bad-dri.jpg"
            zero_dri = root / "zero-dri.jpg"
            duplicate_dri = root / "duplicate-dri.jpg"
            malformed = with_dri_segment(minimal_jpeg(width=17, height=13), 2).replace(
                b"\xff\xdd\x00\x04",
                b"\xff\xdd\x00\x03",
            )
            jpeg.write_bytes(malformed)
            zero_dri.write_bytes(
                with_dri_segment(minimal_jpeg(width=17, height=13), 0)
            )
            duplicate_dri.write_bytes(
                with_dri_segment(
                    with_dri_segment(minimal_jpeg(width=17, height=13), 2),
                    2,
                )
            )

            with self.assertRaisesRegex(ValueError, "DRI"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)
            with self.assertRaisesRegex(ValueError, "restart interval is 0"):
                hjpeg_host.validate_jpeg(zero_dri, expected_width=17, expected_height=13)
            with self.assertRaisesRegex(ValueError, "DRI segment count"):
                hjpeg_host.validate_jpeg(
                    duplicate_dri,
                    expected_width=17,
                    expected_height=13,
                )

    def test_configure_registers_writes_axi_lite_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp) / "mem.bin"
            mem.write_bytes(bytes(hjpeg_host.AXI_LITE_APERTURE_BYTES))

            with hjpeg_host.AxiLiteWindow(mem, 0) as regs:
                hjpeg_host.configure_registers(
                    regs=regs,
                    width=320,
                    height=240,
                    quality=75,
                    restart_interval=4,
                    chroma_subsample=True,
                    emit_jfif=False,
                    clear_error=True,
                )
                self.assertEqual(regs.read32(hjpeg_host.REG_XSIZE), 320)
                self.assertEqual(regs.read32(hjpeg_host.REG_YSIZE), 240)
                self.assertEqual(regs.read32(hjpeg_host.REG_QUALITY), 75)
                self.assertEqual(regs.read32(hjpeg_host.REG_RESTART_INTERVAL), 4)
                self.assertEqual(
                    regs.read32(hjpeg_host.REG_CONTROL),
                    hjpeg_host.CONTROL_CLEAR_PROTOCOL_ERROR
                    | hjpeg_host.CONTROL_ENABLE_CHROMA_SUBSAMPLE,
                )

    def test_control_value_encodes_axi_lite_control_bits(self) -> None:
        cases = [
            (False, False, False, 0),
            (True, False, False, hjpeg_host.CONTROL_ENABLE_CHROMA_SUBSAMPLE),
            (False, True, False, hjpeg_host.CONTROL_EMIT_JFIF),
            (
                True,
                True,
                False,
                hjpeg_host.CONTROL_ENABLE_CHROMA_SUBSAMPLE
                | hjpeg_host.CONTROL_EMIT_JFIF,
            ),
            (False, False, True, hjpeg_host.CONTROL_CLEAR_PROTOCOL_ERROR),
            (
                True,
                False,
                True,
                hjpeg_host.CONTROL_ENABLE_CHROMA_SUBSAMPLE
                | hjpeg_host.CONTROL_CLEAR_PROTOCOL_ERROR,
            ),
            (
                False,
                True,
                True,
                hjpeg_host.CONTROL_EMIT_JFIF
                | hjpeg_host.CONTROL_CLEAR_PROTOCOL_ERROR,
            ),
            (
                True,
                True,
                True,
                hjpeg_host.CONTROL_ENABLE_CHROMA_SUBSAMPLE
                | hjpeg_host.CONTROL_EMIT_JFIF
                | hjpeg_host.CONTROL_CLEAR_PROTOCOL_ERROR,
            ),
        ]

        for chroma_subsample, emit_jfif, clear_error, expected in cases:
            self.assertEqual(
                hjpeg_host.control_value(chroma_subsample, emit_jfif, clear_error),
                expected,
            )

    def test_configure_registers_rejects_default_oversize_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp) / "mem.bin"
            mem.write_bytes(bytes(hjpeg_host.AXI_LITE_APERTURE_BYTES))

            with hjpeg_host.AxiLiteWindow(mem, 0) as regs:
                with self.assertRaisesRegex(ValueError, "height must be in 1..1080"):
                    hjpeg_host.configure_registers(
                        regs=regs,
                        width=1920,
                        height=1081,
                        quality=75,
                        restart_interval=0,
                        chroma_subsample=False,
                        emit_jfif=True,
                        clear_error=False,
                    )

    def test_configure_registers_allows_custom_frame_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp) / "mem.bin"
            mem.write_bytes(bytes(hjpeg_host.AXI_LITE_APERTURE_BYTES))

            with hjpeg_host.AxiLiteWindow(mem, 0) as regs:
                hjpeg_host.configure_registers(
                    regs=regs,
                    width=2048,
                    height=1200,
                    quality=75,
                    restart_interval=0,
                    chroma_subsample=False,
                    emit_jfif=True,
                    clear_error=False,
                    max_width=2048,
                    max_height=1200,
                )
                self.assertEqual(regs.read32(hjpeg_host.REG_XSIZE), 2048)
                self.assertEqual(regs.read32(hjpeg_host.REG_YSIZE), 1200)

    def test_config_cli_rejects_invalid_quality_and_restart_before_io(self) -> None:
        common_args = [
            "config",
            "--dev",
            "missing-mem.bin",
            "--base-addr",
            "0",
            "--width",
            "2",
            "--height",
            "1",
        ]
        invalid_cases = [
            ("--quality", "0"),
            ("--quality", "101"),
            ("--restart-interval", "-1"),
            ("--restart-interval", "65536"),
        ]

        for option, value in invalid_cases:
            with self.subTest(option=option, value=value):
                with self.assertRaises(SystemExit):
                    hjpeg_host.main([*common_args, f"{option}={value}"])

    def test_config_cli_can_print_json_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp) / "mem.bin"
            mem.write_bytes(bytes(hjpeg_host.AXI_LITE_APERTURE_BYTES))

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    hjpeg_host.main(
                        [
                            "config",
                            "--dev",
                            str(mem),
                            "--base-addr",
                            "0",
                            "--width",
                            "320",
                            "--height",
                            "240",
                            "--quality",
                            "75",
                            "--restart-interval",
                            "4",
                            "--chroma-subsample",
                            "--no-jfif",
                            "--clear-error",
                            "--json",
                        ]
                    ),
                    0,
                )

            record = json.loads(stdout.getvalue())
            self.assertEqual(record["axi_lite"]["device"], str(mem))
            self.assertEqual(record["axi_lite"]["base_addr"], 0)
            self.assertEqual(record["axi_lite"]["base_addr_hex"], "0x0")
            self.assertEqual(record["encoder_config"]["width"], 320)
            self.assertEqual(record["encoder_config"]["height"], 240)
            self.assertEqual(record["encoder_config"]["max_width"], 1920)
            self.assertEqual(record["encoder_config"]["max_height"], 1080)
            self.assertEqual(record["encoder_config"]["quality"], 75)
            self.assertEqual(record["encoder_config"]["restart_interval"], 4)
            self.assertTrue(record["encoder_config"]["chroma_subsample"])
            self.assertFalse(record["encoder_config"]["emit_jfif"])
            self.assertTrue(record["encoder_config"]["clear_error"])
            self.assertEqual(
                record["encoder_config"]["control"],
                hjpeg_host.CONTROL_CLEAR_PROTOCOL_ERROR
                | hjpeg_host.CONTROL_ENABLE_CHROMA_SUBSAMPLE,
            )
            self.assertEqual(record["encoder_config"]["control_hex"], "0x00000003")

    def test_encoder_config_record_rejects_invalid_values(self) -> None:
        cases = [
            {"width": 0, "message": "width"},
            {"height": 0, "message": "height"},
            {"width": 4, "max_width": 3, "message": "width"},
            {"height": 4, "max_height": 3, "message": "height"},
            {"max_width": 0, "message": "maximum frame dimensions"},
            {"max_height": 0, "message": "maximum frame dimensions"},
            {"quality": 0, "message": "quality"},
            {"quality": 101, "message": "quality"},
            {"restart_interval": -1, "message": "restart interval"},
            {"restart_interval": 0x10000, "message": "restart interval"},
        ]

        for case in cases:
            with self.subTest(case=case):
                kwargs = {
                    "width": 2,
                    "height": 1,
                    "quality": 75,
                    "restart_interval": 0,
                    "chroma_subsample": False,
                    "emit_jfif": True,
                    "clear_error": False,
                }
                message = str(case["message"])
                kwargs.update({key: value for key, value in case.items() if key != "message"})
                with self.assertRaisesRegex(ValueError, message):
                    hjpeg_host.encoder_config_record(**kwargs)

    def test_axi_lite_target_record_rejects_negative_base_address(self) -> None:
        with self.assertRaisesRegex(ValueError, "base address"):
            hjpeg_host.axi_lite_target_record(Path("/dev/mem"), -1)

    def test_status_text(self) -> None:
        self.assertEqual(hjpeg_host.status_text(0), "idle")
        self.assertEqual(hjpeg_host.status_text(hjpeg_host.STATUS_BUSY), "busy")
        self.assertEqual(
            hjpeg_host.status_text(hjpeg_host.STATUS_BUSY | hjpeg_host.STATUS_PROTOCOL_ERROR),
            "busy,protocol_error",
        )
        self.assertEqual(
            hjpeg_host.status_record(hjpeg_host.STATUS_PROTOCOL_ERROR),
            {
                "status": hjpeg_host.STATUS_PROTOCOL_ERROR,
                "status_hex": "0x00000002",
                "busy": False,
                "protocol_error": True,
                "text": "protocol_error",
            },
        )

    def test_status_cli_can_print_json_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp) / "mem.bin"
            mem.write_bytes(bytes(hjpeg_host.AXI_LITE_APERTURE_BYTES))

            with hjpeg_host.AxiLiteWindow(mem, 0) as regs:
                regs.write32(
                    hjpeg_host.REG_STATUS,
                    hjpeg_host.STATUS_BUSY | hjpeg_host.STATUS_PROTOCOL_ERROR,
                )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    hjpeg_host.main(
                        [
                            "status",
                            "--dev",
                            str(mem),
                            "--base-addr",
                            "0",
                            "--json",
                        ]
                    ),
                    0,
                )

            record = json.loads(stdout.getvalue())
            self.assertEqual(record["axi_lite"]["device"], str(mem))
            self.assertEqual(record["axi_lite"]["base_addr"], 0)
            self.assertEqual(record["axi_lite"]["base_addr_hex"], "0x0")
            self.assertEqual(record["status"], 3)
            self.assertEqual(record["status_hex"], "0x00000003")
            self.assertTrue(record["busy"])
            self.assertTrue(record["protocol_error"])
            self.assertEqual(record["text"], "busy,protocol_error")

    def test_clear_error_preserves_persistent_control_bits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp) / "mem.bin"
            mem.write_bytes(bytes(hjpeg_host.AXI_LITE_APERTURE_BYTES))

            with hjpeg_host.AxiLiteWindow(mem, 0) as regs:
                regs.write32(
                    hjpeg_host.REG_CONTROL,
                    hjpeg_host.CONTROL_ENABLE_CHROMA_SUBSAMPLE,
                )
                hjpeg_host.clear_protocol_error(regs)
                self.assertEqual(
                    regs.read32(hjpeg_host.REG_CONTROL),
                    hjpeg_host.CONTROL_ENABLE_CHROMA_SUBSAMPLE
                    | hjpeg_host.CONTROL_CLEAR_PROTOCOL_ERROR,
                )

    def test_clear_error_cli_can_print_json_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp) / "mem.bin"
            mem.write_bytes(bytes(hjpeg_host.AXI_LITE_APERTURE_BYTES))

            with hjpeg_host.AxiLiteWindow(mem, 0) as regs:
                regs.write32(
                    hjpeg_host.REG_CONTROL,
                    hjpeg_host.CONTROL_ENABLE_CHROMA_SUBSAMPLE,
                )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    hjpeg_host.main(
                        [
                            "clear-error",
                            "--dev",
                            str(mem),
                            "--base-addr",
                            "0",
                            "--json",
                        ]
                    ),
                    0,
                )

            record = json.loads(stdout.getvalue())
            expected_control = (
                hjpeg_host.CONTROL_ENABLE_CHROMA_SUBSAMPLE
                | hjpeg_host.CONTROL_CLEAR_PROTOCOL_ERROR
            )
            self.assertEqual(record["axi_lite"]["device"], str(mem))
            self.assertEqual(record["axi_lite"]["base_addr"], 0)
            self.assertEqual(record["axi_lite"]["base_addr_hex"], "0x0")
            self.assertTrue(record["clear_protocol_error"])
            self.assertEqual(record["control"], expected_control)
            self.assertEqual(record["control_hex"], f"0x{expected_control:08x}")

            with hjpeg_host.AxiLiteWindow(mem, 0) as regs:
                self.assertEqual(regs.read32(hjpeg_host.REG_CONTROL), expected_control)

    def test_read_until_jpeg_eoi_stops_at_marker(self) -> None:
        with tempfile.TemporaryFile() as stream:
            stream.write(b"\xff\xd8payload\xff\xd9")
            stream.seek(0)
            self.assertEqual(
                hjpeg_host.read_until_jpeg_eoi(stream, max_bytes=64),
                b"\xff\xd8payload\xff\xd9",
            )

    def test_read_until_jpeg_eoi_rejects_trailing_data(self) -> None:
        with tempfile.TemporaryFile() as stream:
            stream.write(b"\xff\xd8payload\xff\xd9trailing")
            stream.seek(0)

            with self.assertRaisesRegex(ValueError, "trailing data after JPEG EOI"):
                hjpeg_host.read_until_jpeg_eoi(stream, max_bytes=64)

    def test_run_stream_devices_writes_rgb_and_validates_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_rgb = root / "input.rgb"
            output_jpeg = root / "output.jpg"
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 6, 0]))
            rx_device.write_bytes(minimal_jpeg(width=2, height=1))

            configured = []
            status_checks = []
            transfer_elapsed_seconds = []
            info, input_info = hjpeg_host.run_stream_devices(
                input_rgb=input_rgb,
                output_jpeg=output_jpeg,
                tx_device=tx_device,
                rx_device=rx_device,
                max_output_bytes=1024,
                expected_width=2,
                expected_height=1,
                timeout_seconds=1.0,
                configure=lambda: configured.append(True),
                check_status=lambda context: status_checks.append(context),
                decoder_command=(
                    f'"{sys.executable}" -c "import pathlib, sys; '
                    f"pathlib.Path(r'{root / 'decoder.txt'}').write_text(pathlib.Path(sys.argv[1]).read_bytes()[:2].hex()); "
                    f'print(\'ok\')"'
                ),
                transfer_elapsed_seconds=transfer_elapsed_seconds,
            )

            self.assertEqual(configured, [True])
            self.assertEqual(len(transfer_elapsed_seconds), 1)
            self.assertGreaterEqual(transfer_elapsed_seconds[0], 0.0)
            self.assertEqual(info, minimal_jpeg_info(width=2, height=1))
            self.assertEqual(input_info.path, str(input_rgb))
            self.assertEqual(input_info.byte_length, 8)
            self.assertEqual(input_info.sha256, hashlib.sha256(input_rgb.read_bytes()).hexdigest())
            self.assertEqual(status_checks, ["before transfer", "after transfer"])
            self.assertEqual(tx_device.read_bytes(), bytes([1, 2, 3, 0, 4, 5, 6, 0]))
            self.assertEqual(output_jpeg.read_bytes(), minimal_jpeg(width=2, height=1))
            self.assertEqual((root / "decoder.txt").read_text(), "ffd8")

    def test_run_stream_devices_cli_can_print_json_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_rgb = root / "input.rgb"
            output_jpeg = root / "output.jpg"
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            mem = root / "mem.bin"
            decoder_marker = root / "decoder.txt"
            input_ppm = root / "input.ppm"
            input_ppm.write_bytes(b"P6\n2 1\n255\n" + bytes([1, 2, 3, 4, 5, 6]))
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 6, 0]))
            captured_jpeg = with_dri_segment(
                minimal_jpeg(width=2, height=1, chroma_subsample=True, quality=80),
                2,
            )
            rx_device.write_bytes(captured_jpeg)
            mem.write_bytes(bytes(hjpeg_host.AXI_LITE_APERTURE_BYTES))
            decoder_command = (
                f'"{sys.executable}" -c "import pathlib, sys; '
                f'pathlib.Path(r\'{decoder_marker}\').write_text(pathlib.Path(sys.argv[1]).read_bytes()[:2].hex()); '
                f'print(\'decoded 2x1\')"'
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    hjpeg_host.main(
                        [
                            "run-stream-devices",
                            "--dev",
                            str(mem),
                            "--base-addr",
                            "0",
                            "--tx-device",
                            str(tx_device),
                            "--rx-device",
                            str(rx_device),
                            "--input-rgb",
                            str(input_rgb),
                            "--input-ppm",
                            str(input_ppm),
                            "--output-jpeg",
                            str(output_jpeg),
                            "--width",
                            "2",
                            "--height",
                            "1",
                            "--quality",
                            "80",
                            "--restart-interval",
                            "2",
                            "--chroma-subsample",
                            "--decoder-command",
                            decoder_command,
                            "--decoder-timeout-seconds",
                            "2.5",
                            "--json",
                        ]
                    ),
                    0,
                )

            record = json.loads(stdout.getvalue())
            self.assertEqual(record["jpeg"], str(output_jpeg))
            self.assertEqual(record["width"], 2)
            self.assertEqual(record["height"], 1)
            self.assertEqual(
                record["validation_expectations"],
                {
                    "width": 2,
                    "height": 1,
                    "expected_sample_precision": 8,
                    "expected_component_count": 3,
                    "restart_interval": 2,
                    "expected_restart_markers": 0,
                    "expected_restart_marker_sequence": [],
                    "expected_scan_data_min_bytes": 1,
                    "expected_marker_counts": {
                        "APP0": 1,
                        "JFIF_APP0": 1,
                        "DQT": 2,
                        "SOF0": 1,
                        "DHT": 4,
                        "SOS": 1,
                        "DRI": 1,
                        "RST": 0,
                    },
                    "expected_marker_order": {
                        "through_sos": [
                            "SOI",
                            "APP0",
                            "DQT",
                            "DQT",
                            "SOF0",
                            "DHT",
                            "DHT",
                            "DHT",
                            "DHT",
                            "DRI",
                            "SOS",
                        ],
                        "app0_policy": "present",
                        "dri_policy": "present",
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
                    "expected_sof0_components": [
                        {
                            "component_id": 1,
                            "quantization_table": 0,
                            "horizontal_sampling": 2,
                            "vertical_sampling": 2,
                        },
                        {
                            "component_id": 2,
                            "quantization_table": 1,
                            "horizontal_sampling": 1,
                            "vertical_sampling": 1,
                        },
                        {
                            "component_id": 3,
                            "quantization_table": 1,
                            "horizontal_sampling": 1,
                            "vertical_sampling": 1,
                        },
                    ],
                    "expected_sos_components": [
                        {"component_id": 1, "dc_table": 0, "ac_table": 0},
                        {"component_id": 2, "dc_table": 1, "ac_table": 1},
                        {"component_id": 3, "dc_table": 1, "ac_table": 1},
                    ],
                    "expected_sos_spectral": {
                        "spectral_start": 0,
                        "spectral_end": 63,
                        "successive_approximation": 0,
                    },
                    "check_chroma_mode": True,
                    "chroma_subsample": True,
                    "expected_chroma_mode": "4:2:0",
                    "expect_jfif": "present",
                    "expected_jfif_app0": {
                        "version_major": 1,
                        "version_minor": 1,
                        "density_units": 0,
                        "x_density": 1,
                        "y_density": 1,
                        "thumbnail_width": 0,
                        "thumbnail_height": 0,
                    },
                    "quality": 80,
                    "require_standard_huffman": True,
                    "expected_quantization_payload_sha256": {
                        "0": standard_dqt_payload_sha256(0, quality=80),
                        "1": standard_dqt_payload_sha256(1, quality=80),
                    },
                    "expected_huffman_tables": [
                        {
                            "table_class": 0,
                            "table_id": 0,
                            "symbol_count": 12,
                            "payload_sha256": standard_dht_payload_sha256(0, 0),
                        },
                        {
                            "table_class": 0,
                            "table_id": 1,
                            "symbol_count": 12,
                            "payload_sha256": standard_dht_payload_sha256(0, 1),
                        },
                        {
                            "table_class": 1,
                            "table_id": 0,
                            "symbol_count": 162,
                            "payload_sha256": standard_dht_payload_sha256(1, 0),
                        },
                        {
                            "table_class": 1,
                            "table_id": 1,
                            "symbol_count": 162,
                            "payload_sha256": standard_dht_payload_sha256(1, 1),
                        },
                    ],
                },
            )
            self.assertEqual(record["chroma_mode"], "4:2:0")
            self.assertEqual(record["dri_segments"], 1)
            self.assertEqual(record["restart_interval"], 2)
            self.assertEqual(record["restart_marker_sequence"], [])
            self.assertEqual(record["scan_data_bytes"], 1)
            self.assertEqual(record["scan_data_sha256"], hashlib.sha256(b"\x7f").hexdigest())
            self.assertEqual(record["byte_length"], len(captured_jpeg))
            self.assertEqual(
                record["sha256"],
                hashlib.sha256(captured_jpeg).hexdigest(),
            )
            self.assertEqual(record["input_rgb"]["path"], str(input_rgb))
            self.assertEqual(record["input_rgb"]["byte_length"], 8)
            self.assertEqual(record["input_rgb"]["expected_byte_length"], 8)
            self.assertTrue(record["input_rgb"]["byte_length_matches_expected"])
            self.assertEqual(
                record["input_rgb"]["sha256"],
                hashlib.sha256(input_rgb.read_bytes()).hexdigest(),
            )
            self.assertEqual(record["input_ppm"]["path"], str(input_ppm))
            self.assertEqual(record["input_ppm"]["width"], 2)
            self.assertEqual(record["input_ppm"]["height"], 1)
            self.assertEqual(record["input_ppm"]["packed_rgb_byte_length"], 8)
            self.assertEqual(
                record["input_ppm"]["packed_rgb_sha256"],
                hashlib.sha256(input_rgb.read_bytes()).hexdigest(),
            )
            self.assertTrue(record["input_ppm"]["packed_rgb_matches_input"])
            self.assertEqual(
                record["input_ppm"]["image_stats"],
                {
                    "channel_min": {"r": 1, "g": 2, "b": 3},
                    "channel_max": {"r": 4, "g": 5, "b": 6},
                    "non_flat": True,
                    "has_color_pixels": True,
                },
            )
            self.assertEqual(record["axi_lite"]["device"], str(mem))
            self.assertEqual(record["axi_lite"]["base_addr"], 0)
            self.assertEqual(record["encoder_config"]["width"], 2)
            self.assertEqual(record["encoder_config"]["height"], 1)
            self.assertEqual(record["encoder_config"]["max_width"], 1920)
            self.assertEqual(record["encoder_config"]["max_height"], 1080)
            self.assertEqual(record["encoder_config"]["quality"], 80)
            self.assertEqual(record["encoder_config"]["restart_interval"], 2)
            self.assertTrue(record["encoder_config"]["chroma_subsample"])
            self.assertTrue(record["encoder_config"]["emit_jfif"])
            self.assertFalse(record["encoder_config"]["clear_error"])
            self.assertEqual(
                record["encoder_config"]["control"],
                hjpeg_host.CONTROL_ENABLE_CHROMA_SUBSAMPLE | hjpeg_host.CONTROL_EMIT_JFIF,
            )
            self.assertEqual(record["encoder_config"]["control_hex"], "0x00000006")
            self.assertEqual(record["capture_config"]["max_output_bytes"], 16777216)
            self.assertEqual(record["capture_config"]["timeout_seconds"], 30.0)
            self.assertGreaterEqual(record["transfer_elapsed_seconds"], 0.0)
            self.assertGreater(record["host_transfer_rates"]["input_rgb_bytes_per_second"], 0.0)
            self.assertGreater(record["host_transfer_rates"]["output_jpeg_bytes_per_second"], 0.0)
            self.assertTrue(record["decoder_passed"])
            self.assertEqual(record["decoder_command"], decoder_command)
            self.assertEqual(
                record["decoder_argv"],
                hjpeg_host.decoder_command_argv(output_jpeg, decoder_command),
            )
            self.assertEqual(record["decoder_timeout_seconds"], 2.5)
            self.assertEqual(record["decoder_returncode"], 0)
            self.assertEqual(record["decoder_stdout"], "decoded 2x1\n")
            self.assertEqual(record["decoder_stderr"], "")
            self.assertGreaterEqual(record["decoder_elapsed_seconds"], 0.0)
            self.assertEqual(record["decoder_stdout_chars"], len("decoded 2x1\n"))
            self.assertEqual(record["decoder_stderr_chars"], 0)
            self.assertEqual(
                record["decoder_output_capture_chars"],
                hjpeg_host.DECODER_OUTPUT_CAPTURE_CHARS,
            )
            self.assertFalse(record["decoder_stdout_truncated"])
            self.assertFalse(record["decoder_stderr_truncated"])
            self.assertEqual(decoder_marker.read_text(), "ffd8")
            self.assertEqual(
                [status["context"] for status in record["status_checks"]],
                ["after configuration", "before transfer", "after transfer"],
            )
            self.assertEqual(record["status_check_count"], 3)
            self.assertEqual(
                record["status_check_contexts"],
                ["after configuration", "before transfer", "after transfer"],
            )
            self.assertEqual(
                record["expected_status_check_contexts"],
                ["after configuration", "before transfer", "after transfer"],
            )
            self.assertTrue(record["status_check_contexts_match_expected"])
            self.assertTrue(record["status_checks_all_idle"])
            self.assertFalse(record["status_checks_any_protocol_error"])
            self.assertFalse(record["status_checks_any_busy"])
            self.assertEqual(
                record["hardware_run_summary"],
                {
                    "evidence_present": {
                        "jpeg_output": True,
                        "input_rgb": True,
                        "axi_lite": True,
                        "encoder_config": True,
                        "capture_config": True,
                        "status_checks": True,
                        "validation_expectations": True,
                        "input_ppm": True,
                        "transfer_timing": True,
                        "decoder": True,
                    },
                    "checks": {
                        "jpeg_validation_passed": True,
                        "jpeg_byte_length_positive": True,
                        "jpeg_scan_data_bytes_positive": True,
                        "jpeg_sha256_present": True,
                        "jpeg_scan_data_sha256_present": True,
                        "jpeg_marker_sequence_starts_with_soi": True,
                        "jpeg_marker_sequence_ends_with_eoi": True,
                        "encoder_config_matches_jpeg_dimensions": True,
                        "encoder_dimensions_supported": True,
                        "encoder_quality_valid": True,
                        "encoder_restart_interval_valid": True,
                        "encoder_flags_valid": True,
                        "encoder_control_matches_flags": True,
                        "validation_expectations_match_jpeg_dimensions": True,
                        "validation_baseline_shape": True,
                        "validation_marker_order_present": True,
                        "validation_table_order_present": True,
                        "validation_sos_spectral_baseline": True,
                        "validation_requires_standard_huffman": True,
                        "input_ppm_dimensions_match_jpeg": True,
                        "input_rgb_expected_length_matches_dimensions": True,
                        "input_rgb_byte_length_positive": True,
                        "input_rgb_sha256_present": True,
                        "input_rgb_expected_byte_length_positive": True,
                        "input_rgb_length_matches_expected": True,
                        "capture_max_output_bytes_positive": True,
                        "capture_timeout_valid": True,
                        "axi_lite_device_present": True,
                        "axi_lite_base_addr_nonnegative": True,
                        "axi_lite_base_addr_hex_matches": True,
                        "input_ppm_matches_input": True,
                        "input_ppm_byte_length_positive": True,
                        "input_ppm_sha256_present": True,
                        "input_ppm_dimensions_positive": True,
                        "input_ppm_rgb_byte_length_matches_dimensions": True,
                        "input_ppm_packed_rgb_length_matches_dimensions": True,
                        "input_ppm_packed_rgb_sha256_present": True,
                        "input_ppm_non_flat": True,
                        "input_ppm_has_color_pixels": True,
                        "status_check_contexts_match_expected": True,
                        "status_checks_all_idle": True,
                        "status_checks_no_protocol_error": True,
                        "status_checks_no_busy": True,
                        "decoder_passed": True,
                        "decoder_returncode_zero": True,
                        "decoder_argv_present": True,
                        "decoder_output_not_truncated": True,
                        "transfer_elapsed_seconds_positive": True,
                        "host_transfer_rates_present": True,
                    },
                    "all_recorded_checks_passed": True,
                    "complete_hardware_run_evidence": True,
                },
            )
            for status in record["status_checks"]:
                self.assertEqual(status["axi_lite"]["device"], str(mem))
                self.assertEqual(status["axi_lite"]["base_addr"], 0)
                self.assertEqual(status["axi_lite"]["base_addr_hex"], "0x0")
                self.assertEqual(status["status"], 0)
                self.assertEqual(status["status_hex"], "0x00000000")
                self.assertFalse(status["busy"])
                self.assertFalse(status["protocol_error"])
                self.assertEqual(status["text"], "idle")

    def test_run_stream_devices_cli_rejects_input_ppm_mismatch_before_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_ppm = root / "input.ppm"
            input_rgb = root / "input.rgb"
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            output_jpeg = root / "output.jpg"
            input_ppm.write_bytes(b"P6\n2 1\n255\n" + bytes([1, 2, 3, 4, 5, 6]))
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 7, 0]))
            rx_device.write_bytes(minimal_jpeg(width=2, height=1))

            with self.assertRaisesRegex(ValueError, "packed PPM bytes do not match"):
                hjpeg_host.main(
                    [
                        "run-stream-devices",
                        "--dev",
                        str(root / "mem.bin"),
                        "--base-addr",
                        "0",
                        "--tx-device",
                        str(tx_device),
                        "--rx-device",
                        str(rx_device),
                        "--input-rgb",
                        str(input_rgb),
                        "--input-ppm",
                        str(input_ppm),
                        "--output-jpeg",
                        str(output_jpeg),
                        "--width",
                        "2",
                        "--height",
                        "1",
                    ]
                )

            self.assertFalse(tx_device.exists())
            self.assertFalse(output_jpeg.exists())

    def test_run_stream_devices_rejects_wrong_input_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_rgb = root / "input.rgb"
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            input_rgb.write_bytes(bytes([1, 2, 3]))
            rx_device.write_bytes(minimal_jpeg(width=2, height=1))

            with self.assertRaisesRegex(ValueError, "expected 8 RGB stream bytes"):
                hjpeg_host.run_stream_devices(
                    input_rgb=input_rgb,
                    output_jpeg=root / "output.jpg",
                    tx_device=tx_device,
                    rx_device=rx_device,
                    max_output_bytes=1024,
                    expected_width=2,
                    expected_height=1,
                    timeout_seconds=1.0,
                )

    def test_run_stream_devices_rejects_nonpositive_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_rgb = root / "input.rgb"
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 6, 0]))
            rx_device.write_bytes(minimal_jpeg(width=2, height=1))

            for timeout_seconds in (0, -1, float("nan"), float("inf"), float("-inf")):
                with self.subTest(timeout_seconds=timeout_seconds):
                    with self.assertRaisesRegex(ValueError, "timeout seconds"):
                        hjpeg_host.run_stream_devices(
                            input_rgb=input_rgb,
                            output_jpeg=root / "output.jpg",
                            tx_device=tx_device,
                            rx_device=rx_device,
                            max_output_bytes=1024,
                            expected_width=2,
                            expected_height=1,
                            timeout_seconds=timeout_seconds,
                        )

    def test_run_stream_devices_rejects_nonpositive_max_output_before_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_rgb = root / "input.rgb"
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 6, 0]))
            rx_device.write_bytes(minimal_jpeg(width=2, height=1))
            configured = []

            for max_output_bytes in (0, -1):
                with self.subTest(max_output_bytes=max_output_bytes):
                    with self.assertRaisesRegex(ValueError, "max output bytes"):
                        hjpeg_host.run_stream_devices(
                            input_rgb=input_rgb,
                            output_jpeg=root / "output.jpg",
                            tx_device=tx_device,
                            rx_device=rx_device,
                            max_output_bytes=max_output_bytes,
                            expected_width=2,
                            expected_height=1,
                            timeout_seconds=1.0,
                            configure=lambda: configured.append(True),
                        )

            self.assertEqual(configured, [])
            self.assertFalse(tx_device.exists())

    def test_run_stream_devices_rejects_invalid_quality_and_restart_before_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            configured = []
            cases = (
                {"quality": 0, "message": "quality"},
                {"quality": 101, "message": "quality"},
                {"expected_restart_interval": -1, "message": "restart interval"},
                {"expected_restart_interval": 0x10000, "message": "restart interval"},
            )

            for case in cases:
                with self.subTest(case=case):
                    message = str(case["message"])
                    kwargs = {key: value for key, value in case.items() if key != "message"}
                    with self.assertRaisesRegex(ValueError, message):
                        hjpeg_host.run_stream_devices(
                            input_rgb=root / "missing.rgb",
                            output_jpeg=root / "output.jpg",
                            tx_device=tx_device,
                            rx_device=rx_device,
                            max_output_bytes=1024,
                            expected_width=2,
                            expected_height=1,
                            timeout_seconds=1.0,
                            configure=lambda: configured.append(True),
                            **kwargs,
                        )

            self.assertEqual(configured, [])
            self.assertFalse(tx_device.exists())

    def test_run_stream_devices_rejects_trailing_rx_data_after_eoi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_rgb = root / "input.rgb"
            output_jpeg = root / "output.jpg"
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 6, 0]))
            rx_device.write_bytes(minimal_jpeg(width=2, height=1) + b"trailing")

            with self.assertRaisesRegex(ValueError, "trailing data after JPEG EOI"):
                hjpeg_host.run_stream_devices(
                    input_rgb=input_rgb,
                    output_jpeg=output_jpeg,
                    tx_device=tx_device,
                    rx_device=rx_device,
                    max_output_bytes=1024,
                    expected_width=2,
                    expected_height=1,
                    timeout_seconds=1.0,
                )
            self.assertFalse(output_jpeg.exists())

    def test_run_stream_devices_rejects_restart_interval_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_rgb = root / "input.rgb"
            output_jpeg = root / "output.jpg"
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 6, 0]))
            rx_device.write_bytes(minimal_jpeg(width=2, height=1))

            with self.assertRaisesRegex(ValueError, "DRI segment count"):
                hjpeg_host.run_stream_devices(
                    input_rgb=input_rgb,
                    output_jpeg=output_jpeg,
                    tx_device=tx_device,
                    rx_device=rx_device,
                    max_output_bytes=1024,
                    expected_width=2,
                    expected_height=1,
                    expected_restart_interval=2,
                    timeout_seconds=1.0,
                )

    def test_run_stream_devices_rejects_chroma_mode_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_rgb = root / "input.rgb"
            output_jpeg = root / "output.jpg"
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 6, 0]))
            rx_device.write_bytes(minimal_jpeg(width=2, height=1))

            with self.assertRaisesRegex(ValueError, "expected 4:2:0"):
                hjpeg_host.run_stream_devices(
                    input_rgb=input_rgb,
                    output_jpeg=output_jpeg,
                    tx_device=tx_device,
                    rx_device=rx_device,
                    max_output_bytes=1024,
                    expected_width=2,
                    expected_height=1,
                    expected_chroma_subsample=True,
                    timeout_seconds=1.0,
                )

    def test_run_stream_devices_rejects_jfif_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_rgb = root / "input.rgb"
            output_jpeg = root / "output.jpg"
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 6, 0]))
            rx_device.write_bytes(minimal_jpeg(width=2, height=1))

            with self.assertRaisesRegex(ValueError, "disabled"):
                hjpeg_host.run_stream_devices(
                    input_rgb=input_rgb,
                    output_jpeg=output_jpeg,
                    tx_device=tx_device,
                    rx_device=rx_device,
                    max_output_bytes=1024,
                    expected_width=2,
                    expected_height=1,
                    expected_emit_jfif=False,
                    timeout_seconds=1.0,
                )

    def test_run_stream_devices_rejects_default_oversize_frame_before_io(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_rgb = root / "input.rgb"
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            input_rgb.write_bytes(b"")
            rx_device.write_bytes(minimal_jpeg(width=1921, height=1))
            configured = []

            with self.assertRaisesRegex(ValueError, "width must be in 1..1920"):
                hjpeg_host.run_stream_devices(
                    input_rgb=input_rgb,
                    output_jpeg=root / "output.jpg",
                    tx_device=tx_device,
                    rx_device=rx_device,
                    max_output_bytes=1024,
                    expected_width=1921,
                    expected_height=1,
                    timeout_seconds=1.0,
                    configure=lambda: configured.append(True),
                )
            self.assertEqual(configured, [])
            self.assertFalse(tx_device.exists())

    def test_require_idle_status_rejects_busy_and_protocol_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp) / "mem.bin"
            mem.write_bytes(bytes(hjpeg_host.AXI_LITE_APERTURE_BYTES))

            with hjpeg_host.AxiLiteWindow(mem, 0) as regs:
                hjpeg_host.require_idle_status(regs, "initial")

                regs.write32(hjpeg_host.REG_STATUS, hjpeg_host.STATUS_BUSY)
                with self.assertRaisesRegex(RuntimeError, "busy"):
                    hjpeg_host.require_idle_status(regs, "before transfer")

                regs.write32(hjpeg_host.REG_STATUS, hjpeg_host.STATUS_PROTOCOL_ERROR)
                with self.assertRaisesRegex(RuntimeError, "protocol_error"):
                    hjpeg_host.require_idle_status(regs, "after transfer")


if __name__ == "__main__":
    unittest.main()
