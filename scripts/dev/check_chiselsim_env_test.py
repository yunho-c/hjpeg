#!/usr/bin/env python3

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import check_chiselsim_env


class CheckChiselSimEnvTest(unittest.TestCase):
    def test_rejects_windows_msys_make_shell_combo(self) -> None:
        report = check_chiselsim_env.evaluate_environment(
            check_chiselsim_env.ToolPaths(
                make=r"C:\msys64\usr\bin\make.exe",
                sh=r"C:\msys64\usr\bin\sh.exe",
                verilator=r"C:\msys64\ucrt64\bin\verilator",
            ),
            os_name="nt",
        )

        self.assertFalse(report["compatible"])
        self.assertTrue(report["checks"]["msys_make"])
        self.assertTrue(report["checks"]["msys_sh"])
        self.assertTrue(report["checks"]["msys_verilator"])
        self.assertEqual(
            report["tool_versions"],
            {"make": None, "sh": None, "verilator": None},
        )
        self.assertTrue(any("for /f" in problem for problem in report["problems"]))

    def test_accepts_linux_toolchain_paths(self) -> None:
        report = check_chiselsim_env.evaluate_environment(
            check_chiselsim_env.ToolPaths(
                make="/usr/bin/make",
                sh="/usr/bin/sh",
                verilator="/usr/bin/verilator",
            ),
            os_name="posix",
        )

        self.assertTrue(report["compatible"])
        self.assertEqual(report["problems"], [])

    def test_records_provided_tool_versions(self) -> None:
        report = check_chiselsim_env.evaluate_environment(
            check_chiselsim_env.ToolPaths(
                make="/usr/bin/make",
                sh="/usr/bin/sh",
                verilator="/usr/bin/verilator",
            ),
            os_name="posix",
            tool_versions={
                "make": "GNU Make 4.4.1",
                "sh": "GNU bash, version 5.2.21",
                "verilator": "Verilator 5.034",
            },
        )

        self.assertEqual(report["tool_versions"]["make"], "GNU Make 4.4.1")
        self.assertEqual(report["tool_versions"]["verilator"], "Verilator 5.034")

    def test_collect_tool_versions_reads_first_version_line(self) -> None:
        versions = check_chiselsim_env.collect_tool_versions(
            check_chiselsim_env.ToolPaths(
                make=sys.executable,
                sh=None,
                verilator=sys.executable,
            )
        )

        self.assertIn("Python", versions["make"] or "")
        self.assertIsNone(versions["sh"])
        self.assertIn("Python", versions["verilator"] or "")

    def test_shebang_version_command_handles_env_perl_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "verilator"
            script.write_text("#!/usr/bin/env perl\n", encoding="utf-8")

            self.assertEqual(
                check_chiselsim_env._shebang_version_command(str(script)),
                ["perl", str(script), "--version"],
            )

    def test_rejects_missing_required_tools(self) -> None:
        report = check_chiselsim_env.evaluate_environment(
            check_chiselsim_env.ToolPaths(make=None, sh="/usr/bin/sh", verilator=None),
            os_name="posix",
        )

        self.assertFalse(report["compatible"])
        self.assertIn("make was not found on PATH", report["problems"])
        self.assertIn("verilator was not found on PATH", report["problems"])

    def test_tool_lookup_finds_extensionless_programs_on_path(self) -> None:
        original_path = os.environ.get("PATH", "")
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "verilator"
            tool.write_text("", encoding="utf-8")
            os.environ["PATH"] = tmp
            try:
                self.assertEqual(check_chiselsim_env._which("verilator"), str(tool))
            finally:
                os.environ["PATH"] = original_path

    def test_json_cli_reports_incompatibility(self) -> None:
        original_find_tools = check_chiselsim_env.find_tools
        check_chiselsim_env.find_tools = lambda: check_chiselsim_env.ToolPaths(
            make=r"C:\msys64\usr\bin\make.exe",
            sh=r"C:\msys64\usr\bin\sh.exe",
            verilator=r"C:\msys64\ucrt64\bin\verilator",
        )
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                exit_code = check_chiselsim_env.main(["--json"], os_name="nt")
        finally:
            check_chiselsim_env.find_tools = original_find_tools

        self.assertEqual(exit_code, 1)
        report = json.loads(stdout.getvalue())
        self.assertFalse(report["compatible"])
        self.assertTrue(report["checks"]["windows_host"])
        self.assertIn("tool_versions", report)

    def test_strict_json_dumps_rejects_nonfinite_numbers(self) -> None:
        with self.assertRaises(ValueError):
            check_chiselsim_env.strict_json_dumps({"elapsed": float("nan")})


if __name__ == "__main__":
    unittest.main()
