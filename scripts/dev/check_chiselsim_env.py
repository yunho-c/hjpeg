#!/usr/bin/env python3

"""Preflight the local ChiselSim/Verilator toolchain.

ChiselSim's svsim backend emits different helper Makefile fragments depending
on the host platform. On Windows, a mixed MSYS `make`/`sh`/Verilator setup can
select Windows cleanup rules but execute them under `/bin/sh`, failing before
RTL simulation starts. This script detects that class of environment issue
before running a long simulator-backed test command.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ToolPaths:
    make: str | None
    sh: str | None
    verilator: str | None


def _normal_path(path: str | None) -> str:
    if path is None:
        return ""
    return path.replace("\\", "/").lower()


def _is_msys_usr_tool(path: str | None, name: str) -> bool:
    normalized = _normal_path(path)
    return normalized.endswith(f"/usr/bin/{name}") and "/msys" in normalized


def _is_msys_toolchain_tool(path: str | None, name: str) -> bool:
    normalized = _normal_path(path)
    if not normalized.endswith(f"/bin/{name}"):
        return False
    return any(
        marker in normalized
        for marker in (
            "/ucrt64/bin/",
            "/mingw64/bin/",
            "/clang64/bin/",
            "/clangarm64/bin/",
        )
    )


def find_tools() -> ToolPaths:
    return ToolPaths(
        make=_which("make"),
        sh=_which("sh"),
        verilator=_which("verilator"),
    )


def collect_tool_versions(tools: ToolPaths) -> dict[str, str | None]:
    return {
        "make": _tool_version(tools.make),
        "sh": _tool_version(tools.sh),
        "verilator": _tool_version(tools.verilator),
    }


def _tool_version(path: str | None) -> str | None:
    if path is None:
        return None
    commands = [[path, "--version"]]
    shebang_command = _shebang_version_command(path)
    if shebang_command is not None:
        commands.append(shebang_command)
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
            continue
        output = completed.stdout.strip() or completed.stderr.strip()
        if output:
            return output.splitlines()[0]
    return None


def _shebang_version_command(path: str) -> list[str] | None:
    try:
        first_line = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first_line.startswith("#!"):
        return None
    try:
        parts = shlex.split(first_line[2:].strip())
    except ValueError:
        return None
    if not parts:
        return None
    interpreter = parts[0]
    if _basename(interpreter) == "env" and len(parts) >= 2:
        interpreter = parts[1]
    name = _basename(interpreter)
    if name in {"bash", "perl", "python", "python3", "sh"}:
        return [name, path, "--version"]
    return None


def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").split("/")[-1]


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found is not None:
        return found

    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    return None


def evaluate_environment(
    tools: ToolPaths,
    os_name: str = os.name,
    tool_versions: dict[str, str | None] | None = None,
) -> dict[str, object]:
    problems: list[str] = []
    warnings: list[str] = []

    if tools.make is None:
        problems.append("make was not found on PATH")
    if tools.verilator is None:
        problems.append("verilator was not found on PATH")

    windows = os_name == "nt"
    msys_make = _is_msys_usr_tool(tools.make, "make.exe") or _is_msys_usr_tool(tools.make, "make")
    msys_sh = _is_msys_usr_tool(tools.sh, "sh.exe") or _is_msys_usr_tool(tools.sh, "sh")
    msys_verilator = _is_msys_toolchain_tool(tools.verilator, "verilator.exe") or _is_msys_toolchain_tool(
        tools.verilator,
        "verilator",
    )

    if windows and msys_make and msys_sh:
        problems.append(
            "Windows host with MSYS make/sh is likely incompatible with svsim-generated clean rules; "
            'the generated Makefile can run `for /f "delims=" ...` under /bin/sh before simulation starts'
        )
    if windows and msys_verilator:
        warnings.append(
            "MSYS/MinGW Verilator on Windows may also expose svsim path-normalization and C++ harness issues"
        )

    compatible = not problems
    return {
        "compatible": compatible,
        "tools": {
            "make": tools.make,
            "sh": tools.sh,
            "verilator": tools.verilator,
        },
        "tool_versions": tool_versions
        if tool_versions is not None
        else {
            "make": None,
            "sh": None,
            "verilator": None,
        },
        "checks": {
            "windows_host": windows,
            "msys_make": msys_make,
            "msys_sh": msys_sh,
            "msys_verilator": msys_verilator,
        },
        "problems": problems,
        "warnings": warnings,
        "recommendations": recommendations(compatible, windows),
    }


def recommendations(compatible: bool, windows: bool) -> list[str]:
    if compatible:
        return ["Run simulator-backed tests with `sbt test` or a focused `sbt \"testOnly ...\"` command."]
    if windows:
        return [
            "Run ChiselSim tests from a Linux or WSL environment with Linux make, sh, and Verilator on PATH.",
            "Use `sbt Test/compile` for a source-level Scala gate until the simulator toolchain is compatible.",
            "Keep Vivado validation separate; Vivado batch scripts do not depend on this ChiselSim preflight.",
        ]
    return [
        "Install make and Verilator, then rerun this preflight before launching simulator-backed tests.",
    ]


def format_text(report: dict[str, object]) -> str:
    lines: list[str] = []
    compatible = bool(report["compatible"])
    lines.append("ChiselSim environment: compatible" if compatible else "ChiselSim environment: incompatible")
    tools = report["tools"]
    tool_versions = report["tool_versions"]
    assert isinstance(tools, dict)
    assert isinstance(tool_versions, dict)
    for name in ("make", "sh", "verilator"):
        version = tool_versions.get(name)
        version_text = f" ({version})" if version else ""
        lines.append(f"  {name}: {tools.get(name) or 'not found'}{version_text}")
    for problem in _as_strings(report["problems"]):
        lines.append(f"ERROR: {problem}")
    for warning in _as_strings(report["warnings"]):
        lines.append(f"WARNING: {warning}")
    lines.append("Recommendations:")
    for item in _as_strings(report["recommendations"]):
        lines.append(f"  - {item}")
    return "\n".join(lines)


def _as_strings(values: object) -> Iterable[str]:
    if isinstance(values, list):
        for value in values:
            yield str(value)


def main(argv: list[str] | None = None, os_name: str = os.name) -> int:
    parser = argparse.ArgumentParser(description="Check whether the local ChiselSim toolchain is usable.")
    parser.add_argument("--json", action="store_true", help="print the preflight report as JSON")
    args = parser.parse_args(argv)

    tools = find_tools()
    report = evaluate_environment(
        tools,
        os_name=os_name,
        tool_versions=collect_tool_versions(tools),
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_text(report))
    return 0 if report["compatible"] else 1


if __name__ == "__main__":
    sys.exit(main())
