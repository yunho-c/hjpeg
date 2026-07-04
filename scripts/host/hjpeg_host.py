#!/usr/bin/env python3
"""Host-side helpers for the hjpeg KV260 AXI-Lite/DMA integration.

This utility keeps the software contract close to the RTL register map:

* P6 PPM input is packed as one RGB pixel per AXI-stream beat, with byte order
  R, G, B matching `HjpegAxiStreamCore`.
* AXI-Lite register writes configure `HjpegKv260AxiLiteTop`.
* JPEG output validation checks SOI/EOI and SOF0 dimensions after a hardware run.

DMA buffer allocation and transfer submission are intentionally board-image
specific, so this script prepares and validates the payloads around that layer.
"""

from __future__ import annotations

import argparse
import mmap
import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


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


@dataclass(frozen=True)
class PpmImage:
    width: int
    height: int
    rgb: bytes


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
    output.write_bytes(image.rgb)


def _read_be16(data: bytes, offset: int) -> int:
    return (data[offset] << 8) | data[offset + 1]


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("JPEG output does not start with SOI")

    offset = 2
    while offset + 4 <= len(data):
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break

        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            break
        if 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break

        segment_length = _read_be16(data, offset)
        if segment_length < 2 or offset + segment_length > len(data):
            raise ValueError(f"invalid JPEG segment length at byte {offset - 1}")

        if marker == 0xC0:
            if segment_length < 8:
                raise ValueError("SOF0 segment is too short")
            height = _read_be16(data, offset + 3)
            width = _read_be16(data, offset + 5)
            return width, height

        offset += segment_length

    raise ValueError("JPEG output does not contain a baseline SOF0 segment")


def validate_jpeg(path: Path, expected_width: int, expected_height: int) -> None:
    data = path.read_bytes()
    if len(data) < 4:
        raise ValueError("JPEG output is too short")
    if data[:2] != b"\xff\xd8":
        raise ValueError("JPEG output does not start with SOI")
    if data[-2:] != b"\xff\xd9":
        raise ValueError("JPEG output does not end with EOI")

    width, height = jpeg_dimensions(data)
    if width != expected_width or height != expected_height:
        raise ValueError(
            f"JPEG dimensions are {width}x{height}, expected {expected_width}x{expected_height}"
        )


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
        self.fd = os.open(self.device, os.O_RDWR | os.O_SYNC)
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
) -> None:
    if not 1 <= width <= 0xFFFF:
        raise ValueError("width must be in 1..65535")
    if not 1 <= height <= 0xFFFF:
        raise ValueError("height must be in 1..65535")
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


def status_text(status: int) -> str:
    flags = []
    if status & STATUS_BUSY:
        flags.append("busy")
    if status & STATUS_PROTOCOL_ERROR:
        flags.append("protocol_error")
    return ",".join(flags) if flags else "idle"


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
    pack.add_argument("--max-width", type=int, default=4096)
    pack.add_argument("--max-height", type=int, default=4096)

    validate = subparsers.add_parser("validate-jpeg", help="validate JPEG markers and dimensions")
    validate.add_argument("jpeg", type=Path)
    validate.add_argument("--width", type=int, required=True)
    validate.add_argument("--height", type=int, required=True)

    config = subparsers.add_parser("config", help="write encoder AXI-Lite configuration registers")
    config.add_argument("--dev", type=Path, default=Path("/dev/mem"))
    config.add_argument("--base-addr", type=_parse_int, required=True)
    config.add_argument("--width", type=int, required=True)
    config.add_argument("--height", type=int, required=True)
    config.add_argument("--quality", type=int, default=50)
    config.add_argument("--restart-interval", type=int, default=0)
    config.add_argument("--chroma-subsample", action="store_true")
    config.add_argument("--no-jfif", action="store_true")
    config.add_argument("--clear-error", action="store_true")

    status = subparsers.add_parser("status", help="read encoder AXI-Lite status register")
    status.add_argument("--dev", type=Path, default=Path("/dev/mem"))
    status.add_argument("--base-addr", type=_parse_int, required=True)

    clear = subparsers.add_parser("clear-error", help="pulse the protocol-error clear bit")
    clear.add_argument("--dev", type=Path, default=Path("/dev/mem"))
    clear.add_argument("--base-addr", type=_parse_int, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "pack-ppm":
        image = read_ppm(args.input)
        if image.width > args.max_width or image.height > args.max_height:
            raise ValueError(
                f"image {image.width}x{image.height} exceeds configured maximum "
                f"{args.max_width}x{args.max_height}"
            )
        write_rgb_stream(image, args.output)
        print(f"wrote {len(image.rgb)} RGB stream bytes for {image.width}x{image.height}")
        return 0

    if args.command == "validate-jpeg":
        validate_jpeg(args.jpeg, args.width, args.height)
        print(f"{args.jpeg}: valid baseline JPEG dimensions {args.width}x{args.height}")
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
            )
        print(f"configured hjpeg at 0x{args.base_addr:x} for {args.width}x{args.height}")
        return 0

    if args.command == "status":
        with AxiLiteWindow(args.dev, args.base_addr) as regs:
            status = regs.read32(REG_STATUS)
        print(f"0x{status:08x} {status_text(status)}")
        return 0

    if args.command == "clear-error":
        with AxiLiteWindow(args.dev, args.base_addr) as regs:
            clear_protocol_error(regs)
        print(f"cleared hjpeg protocol error at 0x{args.base_addr:x}")
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
