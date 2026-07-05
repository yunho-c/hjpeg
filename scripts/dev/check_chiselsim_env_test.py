#!/usr/bin/env python3

import contextlib
import io
import json
import unittest

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

    def test_rejects_missing_required_tools(self) -> None:
        report = check_chiselsim_env.evaluate_environment(
            check_chiselsim_env.ToolPaths(make=None, sh="/usr/bin/sh", verilator=None),
            os_name="posix",
        )

        self.assertFalse(report["compatible"])
        self.assertIn("make was not found on PATH", report["problems"])
        self.assertIn("verilator was not found on PATH", report["problems"])

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
                exit_code = check_chiselsim_env.main(["--json"])
        finally:
            check_chiselsim_env.find_tools = original_find_tools

        self.assertEqual(exit_code, 1)
        report = json.loads(stdout.getvalue())
        self.assertFalse(report["compatible"])
        self.assertTrue(report["checks"]["windows_host"])


if __name__ == "__main__":
    unittest.main()
