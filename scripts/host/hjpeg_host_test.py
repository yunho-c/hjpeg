#!/usr/bin/env python3

import tempfile
import unittest
from pathlib import Path

import hjpeg_host


def minimal_jpeg(width: int, height: int) -> bytes:
    return bytes(
        [
            0xFF,
            0xD8,
            0xFF,
            0xE0,
            0x00,
            0x04,
            0x00,
            0x00,
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
            0xD9,
        ]
    )


class HjpegHostTest(unittest.TestCase):
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
            self.assertEqual(rgb.read_bytes(), bytes([1, 2, 3, 4, 5, 6]))

    def test_validate_jpeg_checks_sof0_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13))

            self.assertEqual(hjpeg_host.jpeg_dimensions(jpeg.read_bytes()), (17, 13))
            hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)
            with self.assertRaisesRegex(ValueError, "expected 16x13"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=16, expected_height=13)

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

    def test_status_text(self) -> None:
        self.assertEqual(hjpeg_host.status_text(0), "idle")
        self.assertEqual(hjpeg_host.status_text(hjpeg_host.STATUS_BUSY), "busy")
        self.assertEqual(
            hjpeg_host.status_text(hjpeg_host.STATUS_BUSY | hjpeg_host.STATUS_PROTOCOL_ERROR),
            "busy,protocol_error",
        )

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


if __name__ == "__main__":
    unittest.main()
