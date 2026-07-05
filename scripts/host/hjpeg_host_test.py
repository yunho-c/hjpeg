#!/usr/bin/env python3

import sys
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

    def test_validate_jpeg_checks_sof0_dimensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13))

            self.assertEqual(hjpeg_host.jpeg_dimensions(jpeg.read_bytes()), (17, 13))
            self.assertEqual(hjpeg_host.jpeg_info(jpeg.read_bytes()).scan_data_bytes, 1)
            hjpeg_host.validate_jpeg(jpeg, expected_width=17, expected_height=13)
            hjpeg_host.run_decoder_command(
                jpeg,
                f'"{sys.executable}" -c "import sys; assert open(sys.argv[1], \'rb\').read(2) == bytes([0xff, 0xd8])"',
            )
            with self.assertRaisesRegex(ValueError, "expected 16x13"):
                hjpeg_host.validate_jpeg(jpeg, expected_width=16, expected_height=13)

    def test_validate_jpeg_cli_can_run_decoder_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            marker = Path(tmp) / "decoder-ran.txt"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13))

            command = (
                f'"{sys.executable}" -c "import pathlib, sys; '
                f'pathlib.Path(r\'{marker}\').write_text(pathlib.Path(sys.argv[1]).read_bytes()[:2].hex())"'
            )
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

    def test_decoder_command_supports_placeholder_and_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jpeg = Path(tmp) / "out.jpg"
            marker = Path(tmp) / "placeholder-ran.txt"
            jpeg.write_bytes(minimal_jpeg(width=17, height=13))

            command = (
                f'"{sys.executable}" -c "import pathlib, sys; '
                f'pathlib.Path(r\'{marker}\').write_text(pathlib.Path(sys.argv[1].split(\'=\', 1)[1]).name)" file={{jpeg}}'
            )
            hjpeg_host.run_decoder_command(jpeg, command)
            self.assertEqual(marker.read_text(), "out.jpg")

            with self.assertRaisesRegex(RuntimeError, "decoder command failed"):
                hjpeg_host.run_decoder_command(
                    jpeg,
                    f'"{sys.executable}" -c "import sys; print(\'bad\', file=sys.stderr); sys.exit(3)"',
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

    def test_read_until_jpeg_eoi_stops_at_marker(self) -> None:
        with tempfile.TemporaryFile() as stream:
            stream.write(b"\xff\xd8payload\xff\xd9trailing")
            stream.seek(0)
            self.assertEqual(
                hjpeg_host.read_until_jpeg_eoi(stream, max_bytes=64),
                b"\xff\xd8payload\xff\xd9",
            )

    def test_run_stream_devices_writes_rgb_and_validates_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_rgb = root / "input.rgb"
            output_jpeg = root / "output.jpg"
            tx_device = root / "tx.dev"
            rx_device = root / "rx.dev"
            input_rgb.write_bytes(bytes([1, 2, 3, 0, 4, 5, 6, 0]))
            rx_device.write_bytes(minimal_jpeg(width=2, height=1) + b"ignored")

            configured = []
            status_checks = []
            hjpeg_host.run_stream_devices(
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
            )

            self.assertEqual(configured, [True])
            self.assertEqual(status_checks, ["before transfer", "after transfer"])
            self.assertEqual(tx_device.read_bytes(), bytes([1, 2, 3, 0, 4, 5, 6, 0]))
            self.assertEqual(output_jpeg.read_bytes(), minimal_jpeg(width=2, height=1))
            self.assertEqual((root / "decoder.txt").read_text(), "ffd8")

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
