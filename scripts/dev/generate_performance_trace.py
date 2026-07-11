#!/usr/bin/env python3

"""Generate transaction-level HJPEG performance traces and summaries."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_OUTPUT_DIR = Path("build/performance-traces")
SUPPORTED_SCENARIOS = ("444", "420", "444-output-stalls")
BOUNDARY_ORDER = (
    "rgb_input",
    "transform_input",
    "dct_input",
    "dct_output",
    "quantize_input",
    "quantize_output",
    "zigzag_input",
    "zigzag_output",
    "transform_output",
    "mcu_output",
    "entropy_block_input",
    "entropy_run_output",
    "packer_output",
    "jpeg_output",
)
STAGE_BOUNDARIES = {
    "dct": ("dct_input", "dct_output"),
    "quantize": ("quantize_input", "quantize_output"),
    "zigzag": ("zigzag_input", "zigzag_output"),
    "block_transform": ("transform_input", "transform_output"),
}


class PerformanceTraceError(RuntimeError):
    """Raised when capture or rendering cannot produce trustworthy output."""


@dataclass(frozen=True)
class Scenario:
    name: str
    width: int
    height: int
    sampling: str
    ready_pattern: str
    clock_hz: int
    first_input_cycle: int
    last_output_cycle: int
    frame_cycles: int
    pixels: int
    bytes: int
    mcus: int
    blocks: int


@dataclass(frozen=True)
class Sample:
    scenario: str
    cycle: int
    boundary: str
    valid: bool
    ready: bool


@dataclass(frozen=True)
class Distribution:
    count: int
    minimum: int | None
    p50: int | None
    p95: int | None
    maximum: int | None
    mean: float | None


def _parse_int(row: dict[str, str], field: str, *, minimum: int = 0) -> int:
    try:
        value = int(row[field])
    except (KeyError, ValueError) as error:
        raise PerformanceTraceError(f"invalid integer field {field!r}: {row.get(field)!r}") from error
    if value < minimum:
        raise PerformanceTraceError(f"field {field!r} must be at least {minimum}, got {value}")
    return value


def _read_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except OSError as error:
        raise PerformanceTraceError(f"could not read {path}: {error}") from error


def read_capture(directory: Path) -> tuple[list[Scenario], list[Sample]]:
    scenario_path = directory / "scenarios.csv"
    sample_path = directory / "samples.csv"
    scenario_rows = _read_rows(scenario_path)
    sample_rows = _read_rows(sample_path)
    if not scenario_rows:
        raise PerformanceTraceError(f"capture contains no scenarios: {scenario_path}")

    scenarios: list[Scenario] = []
    names: set[str] = set()
    for row in scenario_rows:
        name = row.get("scenario", "")
        if not name or name in names:
            raise PerformanceTraceError(f"scenario names must be nonempty and unique: {name!r}")
        scenario = Scenario(
            name=name,
            width=_parse_int(row, "width", minimum=1),
            height=_parse_int(row, "height", minimum=1),
            sampling=row.get("sampling", ""),
            ready_pattern=row.get("ready_pattern", ""),
            clock_hz=_parse_int(row, "clock_hz", minimum=1),
            first_input_cycle=_parse_int(row, "first_input_cycle"),
            last_output_cycle=_parse_int(row, "last_output_cycle"),
            frame_cycles=_parse_int(row, "frame_cycles", minimum=1),
            pixels=_parse_int(row, "pixels", minimum=1),
            bytes=_parse_int(row, "bytes", minimum=1),
            mcus=_parse_int(row, "mcus", minimum=1),
            blocks=_parse_int(row, "blocks", minimum=1),
        )
        if scenario.last_output_cycle - scenario.first_input_cycle + 1 != scenario.frame_cycles:
            raise PerformanceTraceError(f"scenario {name!r} has inconsistent frame cycle bounds")
        scenarios.append(scenario)
        names.add(name)

    samples: list[Sample] = []
    seen: set[tuple[str, int, str]] = set()
    for row in sample_rows:
        name = row.get("scenario", "")
        boundary = row.get("boundary", "")
        if name not in names:
            raise PerformanceTraceError(f"sample references unknown scenario {name!r}")
        if boundary not in BOUNDARY_ORDER:
            raise PerformanceTraceError(f"sample references unknown boundary {boundary!r}")
        valid_text = row.get("valid")
        ready_text = row.get("ready")
        if valid_text not in ("0", "1") or ready_text not in ("0", "1"):
            raise PerformanceTraceError("sample valid and ready fields must be 0 or 1")
        sample = Sample(name, _parse_int(row, "cycle"), boundary, valid_text == "1", ready_text == "1")
        key = (sample.scenario, sample.cycle, sample.boundary)
        if key in seen:
            raise PerformanceTraceError(f"duplicate sample for {key}")
        seen.add(key)
        samples.append(sample)

    grouped = defaultdict(set)
    for sample in samples:
        grouped[sample.scenario].add(sample.boundary)
    for scenario in scenarios:
        missing = set(BOUNDARY_ORDER) - grouped[scenario.name]
        if missing:
            raise PerformanceTraceError(f"scenario {scenario.name!r} is missing boundaries: {', '.join(sorted(missing))}")
    return scenarios, sorted(samples, key=lambda item: (item.scenario, item.cycle, BOUNDARY_ORDER.index(item.boundary)))


def sample_state(sample: Sample) -> str:
    if sample.valid and sample.ready:
        return "transfer"
    if sample.valid:
        return "blocked"
    if sample.ready:
        return "starved"
    return "idle"


def coalesce_states(samples: Sequence[Sample]) -> list[tuple[int, int, str]]:
    if not samples:
        return []
    ordered = sorted(samples, key=lambda item: item.cycle)
    result: list[tuple[int, int, str]] = []
    start = ordered[0].cycle
    previous = start
    state = sample_state(ordered[0])
    for item in ordered[1:]:
        next_state = sample_state(item)
        if item.cycle != previous + 1 or next_state != state:
            result.append((start, previous - start + 1, state))
            start = item.cycle
            state = next_state
        previous = item.cycle
    result.append((start, previous - start + 1, state))
    return result


def distribution(values: Sequence[int]) -> Distribution:
    if not values:
        return Distribution(0, None, None, None, None, None)
    ordered = sorted(values)

    def percentile(fraction: float) -> int:
        return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]

    return Distribution(
        count=len(ordered),
        minimum=ordered[0],
        p50=percentile(0.50),
        p95=percentile(0.95),
        maximum=ordered[-1],
        mean=sum(ordered) / len(ordered),
    )


def _fire_cycles(samples: Sequence[Sample]) -> list[int]:
    return [sample.cycle for sample in samples if sample.valid and sample.ready]


def _longest_state_run(samples: Sequence[Sample], state: str) -> int:
    return max((duration for _, duration, item_state in coalesce_states(samples) if item_state == state), default=0)


def calculate_metrics(scenarios: Sequence[Scenario], samples: Sequence[Sample]) -> dict[str, object]:
    by_scenario_boundary: dict[tuple[str, str], list[Sample]] = defaultdict(list)
    for sample in samples:
        by_scenario_boundary[(sample.scenario, sample.boundary)].append(sample)

    scenario_metrics: list[dict[str, object]] = []
    for scenario in scenarios:
        boundaries: list[dict[str, object]] = []
        fires: dict[str, list[int]] = {}
        for boundary in BOUNDARY_ORDER:
            boundary_samples = by_scenario_boundary[(scenario.name, boundary)]
            states = [sample_state(item) for item in boundary_samples]
            fire_cycles = _fire_cycles(boundary_samples)
            fires[boundary] = fire_cycles
            boundaries.append(
                {
                    "name": boundary,
                    "transfers": len(fire_cycles),
                    "transfer_cycles": states.count("transfer"),
                    "blocked_cycles": states.count("blocked"),
                    "starved_cycles": states.count("starved"),
                    "idle_cycles": states.count("idle"),
                    "longest_blocked_run": _longest_state_run(boundary_samples, "blocked"),
                    "transfers_per_cycle": len(fire_cycles) / scenario.frame_cycles,
                    "transfer_utilization": len(fire_cycles) / scenario.frame_cycles,
                }
            )

        stages: list[dict[str, object]] = []
        for stage, (input_boundary, output_boundary) in STAGE_BOUNDARIES.items():
            inputs = fires[input_boundary]
            outputs = fires[output_boundary]
            if len(inputs) != len(outputs):
                raise PerformanceTraceError(
                    f"scenario {scenario.name!r} stage {stage!r} has {len(inputs)} inputs and {len(outputs)} outputs")
            latencies = [output_cycle - input_cycle for input_cycle, output_cycle in zip(inputs, outputs)]
            initiation = [right - left for left, right in zip(inputs, inputs[1:])]
            stages.append(
                {
                    "name": stage,
                    "transactions": len(latencies),
                    "latency_cycles": asdict(distribution(latencies)),
                    "initiation_interval_cycles": asdict(distribution(initiation)),
                }
            )

        input_fires = fires["rgb_input"]
        mcu_fires = fires["mcu_output"]
        input_span = input_fires[-1] - input_fires[0] + 1
        input_cycles_per_pixel = input_span / scenario.pixels
        mcu_intervals = [right - left for left, right in zip(mcu_fires, mcu_fires[1:])]
        blocks_per_mcu = 3 if scenario.sampling == "4:4:4" else 6
        transform_inputs = fires["transform_input"]
        # Exclude the interval between the final block of one MCU and the first
        # block of the next. That gap measures raster/MCU supply rather than the
        # transform's sustained block acceptance interval.
        transform_intervals = [
            transform_inputs[index] - transform_inputs[index - 1]
            for index in range(1, len(transform_inputs))
            if index % blocks_per_mcu != 0
        ]
        block_budget = 34.3 if scenario.sampling == "4:4:4" else 68.1
        mcu_budget = 102.9 if scenario.sampling == "4:4:4" else 408.5

        def comparison(name: str, actual: float | None, budget: float) -> dict[str, object]:
            ratio = actual / budget if actual is not None else None
            status = "unknown" if ratio is None else "over" if ratio > 1.0 else "near" if ratio > 0.9 else "within"
            return {"name": name, "actual": actual, "budget": budget, "ratio": ratio, "status": status}

        targets = [
            comparison("input_cycles_per_pixel", input_cycles_per_pixel, 1.61),
            comparison("block_transform_max_ii", max(transform_intervals) if transform_intervals else None, block_budget),
            comparison("mcu_max_ii", max(mcu_intervals) if mcu_intervals else None, mcu_budget),
        ]
        scenario_metrics.append(
            {
                "name": scenario.name,
                "sampling": scenario.sampling,
                "width": scenario.width,
                "height": scenario.height,
                "ready_pattern": scenario.ready_pattern,
                "clock_hz": scenario.clock_hz,
                "frame_cycles": scenario.frame_cycles,
                "frame_time_us": scenario.frame_cycles * 1_000_000 / scenario.clock_hz,
                "frames_per_second": scenario.clock_hz / scenario.frame_cycles,
                "pixels": scenario.pixels,
                "bytes": scenario.bytes,
                "mcus": scenario.mcus,
                "blocks": scenario.blocks,
                "pixels_per_cycle": scenario.pixels / scenario.frame_cycles,
                "bytes_per_cycle": scenario.bytes / scenario.frame_cycles,
                "input_acceptance_cycles_per_pixel": input_cycles_per_pixel,
                "boundaries": boundaries,
                "stages": stages,
                "targets": targets,
            }
        )
    return {"schema_version": 1, "scenarios": scenario_metrics}


def _scenario_metric(metrics: dict[str, object], name: str) -> dict[str, object]:
    scenarios = metrics["scenarios"]
    assert isinstance(scenarios, list)
    for scenario in scenarios:
        assert isinstance(scenario, dict)
        if scenario["name"] == name:
            return scenario
    raise PerformanceTraceError(f"missing calculated metrics for scenario {name!r}")


def render_perfetto(scenarios: Sequence[Scenario], samples: Sequence[Sample], metrics: dict[str, object]) -> dict[str, object]:
    by_scenario_boundary: dict[tuple[str, str], list[Sample]] = defaultdict(list)
    for sample in samples:
        by_scenario_boundary[(sample.scenario, sample.boundary)].append(sample)
    colors = {"transfer": "good", "blocked": "bad", "starved": "thread_state_uninterruptible", "idle": "grey"}
    events: list[dict[str, object]] = []
    for process_id, scenario in enumerate(scenarios, start=1):
        events.append({"ph": "M", "pid": process_id, "tid": 0, "name": "process_name", "args": {"name": scenario.name}})
        for thread_id, boundary in enumerate(BOUNDARY_ORDER, start=1):
            events.append({"ph": "M", "pid": process_id, "tid": thread_id, "name": "thread_name", "args": {"name": boundary}})
            for start, duration, state in coalesce_states(by_scenario_boundary[(scenario.name, boundary)]):
                events.append(
                    {
                        "ph": "X",
                        "pid": process_id,
                        "tid": thread_id,
                        "name": state,
                        "cat": "ready_valid",
                        "cname": colors[state],
                        "ts": start * 0.01,
                        "dur": duration * 0.01,
                        "args": {"start_cycle": start, "duration_cycles": duration, "boundary": boundary},
                    }
                )

        scenario_metrics = _scenario_metric(metrics, scenario.name)
        stages = scenario_metrics["stages"]
        assert isinstance(stages, list)
        for stage_offset, stage in enumerate(stages):
            assert isinstance(stage, dict)
            stage_name = str(stage["name"])
            input_boundary, output_boundary = STAGE_BOUNDARIES[stage_name]
            inputs = _fire_cycles(by_scenario_boundary[(scenario.name, input_boundary)])
            outputs = _fire_cycles(by_scenario_boundary[(scenario.name, output_boundary)])
            thread_id = len(BOUNDARY_ORDER) + stage_offset + 1
            events.append({"ph": "M", "pid": process_id, "tid": thread_id, "name": "thread_name", "args": {"name": f"latency/{stage_name}"}})
            for transaction_id, (start, end) in enumerate(zip(inputs, outputs)):
                events.append(
                    {
                        "ph": "X",
                        "pid": process_id,
                        "tid": thread_id,
                        "name": f"{stage_name} #{transaction_id}",
                        "cat": "transaction_latency",
                        "cname": "rail_response",
                        "ts": start * 0.01,
                        "dur": max(1, end - start) * 0.01,
                        "args": {"transaction": transaction_id, "start_cycle": start, "end_cycle": end, "latency_cycles": end - start},
                    }
                )
    return {"displayTimeUnit": "ns", "traceEvents": events}


def _format_number(value: object, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _target(scenario_metrics: dict[str, object], name: str) -> dict[str, object]:
    targets = scenario_metrics["targets"]
    assert isinstance(targets, list)
    return next(item for item in targets if isinstance(item, dict) and item["name"] == name)


def _boundary(scenario_metrics: dict[str, object], name: str) -> dict[str, object]:
    boundaries = scenario_metrics["boundaries"]
    assert isinstance(boundaries, list)
    return next(item for item in boundaries if isinstance(item, dict) and item["name"] == name)


def _stage(scenario_metrics: dict[str, object], name: str) -> dict[str, object]:
    stages = scenario_metrics["stages"]
    assert isinstance(stages, list)
    return next(item for item in stages if isinstance(item, dict) and item["name"] == name)


def graph_nodes(scenario_metrics: dict[str, object]) -> list[tuple[str, str, str]]:
    input_target = _target(scenario_metrics, "input_cycles_per_pixel")
    block_target = _target(scenario_metrics, "block_transform_max_ii")
    mcu_target = _target(scenario_metrics, "mcu_max_ii")
    transform = _stage(scenario_metrics, "block_transform")
    transform_latency = transform["latency_cycles"]
    assert isinstance(transform_latency, dict)
    entropy = _boundary(scenario_metrics, "entropy_block_input")
    packer = _boundary(scenario_metrics, "packer_output")
    output = _boundary(scenario_metrics, "jpeg_output")

    def target_label(item: dict[str, object], unit: str) -> str:
        return f"{_format_number(item['actual'])} / {_format_number(item['budget'])} {unit}"

    return [
        ("input", f"RGB input\n{target_label(input_target, 'cycles/pixel')}", str(input_target["status"])),
        ("raster", "Raster buffering\nstripe/band collection", "neutral"),
        (
            "transform",
            f"Block transform\nlatency mean {_format_number(transform_latency['mean'])} cycles\nsustained II {target_label(block_target, 'cycles/block')}",
            str(block_target["status"]),
        ),
        ("mcu", f"MCU handoff\nII {target_label(mcu_target, 'cycles/MCU')}", str(mcu_target["status"])),
        ("entropy", f"Entropy encoder\n{entropy['transfers']} blocks accepted", "neutral"),
        ("packer", f"Byte packer\n{packer['transfers']} bytes transferred", "neutral"),
        (
            "output",
            f"JPEG output\n{output['transfers']} bytes\n{output['blocked_cycles']} blocked cycles",
            "neutral",
        ),
    ]


def render_graph_mermaid(scenario_metrics: dict[str, object]) -> str:
    lines = ["flowchart LR"]
    nodes = graph_nodes(scenario_metrics)
    for node_id, label, _ in nodes:
        escaped = label.replace("&", "&amp;").replace('"', "&quot;").replace("\n", "<br/>")
        lines.append(f'  {node_id}["{escaped}"]')
    for left, right in zip(nodes, nodes[1:]):
        lines.append(f"  {left[0]} --> {right[0]}")
    lines.extend(
        [
            "  classDef over fill:#fee2e2,stroke:#dc2626,stroke-width:2px",
            "  classDef near fill:#fef3c7,stroke:#d97706,stroke-width:2px",
            "  classDef within fill:#dcfce7,stroke:#16a34a",
            "  classDef neutral fill:#f8fafc,stroke:#64748b",
        ]
    )
    for status in ("over", "near", "within", "neutral"):
        selected = [node_id for node_id, _, node_status in nodes if node_status == status]
        if selected:
            lines.append(f"  class {','.join(selected)} {status}")
    return "\n".join(lines) + "\n"


def render_graph_dot(scenario_metrics: dict[str, object]) -> str:
    colors = {
        "over": ("#fee2e2", "#dc2626"),
        "near": ("#fef3c7", "#d97706"),
        "within": ("#dcfce7", "#16a34a"),
        "neutral": ("#f8fafc", "#64748b"),
    }
    nodes = graph_nodes(scenario_metrics)
    lines = [
        "digraph performance_pipeline {",
        "  rankdir=LR;",
        '  graph [fontname="Helvetica", bgcolor="transparent"];',
        '  node [shape=box, style="rounded,filled", fontname="Helvetica"];',
        '  edge [color="#64748b"];',
    ]
    for node_id, label, status in nodes:
        fill, stroke = colors[status]
        lines.append(f"  {node_id} [label={json.dumps(label)}, fillcolor={json.dumps(fill)}, color={json.dumps(stroke)}];")
    for left, right in zip(nodes, nodes[1:]):
        lines.append(f"  {left[0]} -> {right[0]};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_capture(directory: Path, scenarios: Sequence[Scenario], samples: Sequence[Sample]) -> None:
    with (directory / "scenarios.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(scenarios[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(scenario) for scenario in scenarios)
    with (directory / "samples.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(samples[0]).keys()))
        writer.writeheader()
        writer.writerows({**asdict(sample), "valid": int(sample.valid), "ready": int(sample.ready)} for sample in samples)


def write_metrics_csv(path: Path, metrics: dict[str, object]) -> None:
    rows: list[dict[str, object]] = []
    scenarios = metrics["scenarios"]
    assert isinstance(scenarios, list)
    for scenario in scenarios:
        assert isinstance(scenario, dict)
        name = scenario["name"]
        for metric_name, unit in (
            ("frame_cycles", "cycles"),
            ("frame_time_us", "microseconds"),
            ("frames_per_second", "frames/second"),
            ("pixels_per_cycle", "pixels/cycle"),
            ("bytes_per_cycle", "bytes/cycle"),
            ("input_acceptance_cycles_per_pixel", "cycles/pixel"),
        ):
            rows.append({"scenario": name, "category": "frame", "name": "frame", "metric": metric_name, "value": scenario[metric_name], "unit": unit})
        for boundary in scenario["boundaries"]:
            assert isinstance(boundary, dict)
            for metric_name in ("transfers", "blocked_cycles", "starved_cycles", "idle_cycles", "longest_blocked_run", "transfers_per_cycle"):
                rows.append({"scenario": name, "category": "boundary", "name": boundary["name"], "metric": metric_name, "value": boundary[metric_name], "unit": "cycles" if "cycles" in metric_name or metric_name == "longest_blocked_run" else "transfers/cycle" if metric_name == "transfers_per_cycle" else "transfers"})
        for stage in scenario["stages"]:
            assert isinstance(stage, dict)
            for distribution_name in ("latency_cycles", "initiation_interval_cycles"):
                values = stage[distribution_name]
                assert isinstance(values, dict)
                for statistic, value in values.items():
                    rows.append({"scenario": name, "category": "stage", "name": stage["name"], "metric": f"{distribution_name}.{statistic}", "value": value, "unit": "cycles"})
        for target in scenario["targets"]:
            assert isinstance(target, dict)
            for metric_name in ("actual", "budget", "ratio", "status"):
                rows.append({"scenario": name, "category": "target", "name": target["name"], "metric": metric_name, "value": target[metric_name], "unit": "ratio" if metric_name == "ratio" else ""})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=("scenario", "category", "name", "metric", "value", "unit"))
        writer.writeheader()
        writer.writerows(rows)


def clean_previous_artifacts(output_dir: Path) -> None:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    candidates: list[object] = []
    if isinstance(manifest, dict):
        candidates.extend(manifest.get("files", []))
    resolved_output = output_dir.resolve()
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        path = Path(candidate)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.parent == resolved_output and resolved.is_file():
            resolved.unlink()
    manifest_path.unlink(missing_ok=True)


def _run_simulation(repo_root: Path, capture_dir: Path, scenarios: Sequence[str]) -> None:
    sbt = shutil.which("sbt")
    if sbt is None:
        raise PerformanceTraceError("sbt is required to capture performance scenarios")
    environment = os.environ.copy()
    environment["HJPEG_PERFORMANCE_CAPTURE_DIR"] = str(capture_dir)
    environment["HJPEG_PERFORMANCE_SCENARIOS"] = ",".join(scenarios)
    command = [sbt, "testOnly hjpeg.performance.HjpegPerformanceTraceSpec"]
    print(f"Performance simulation: {' '.join(command)}", file=sys.stderr)
    try:
        completed = subprocess.run(command, cwd=repo_root, env=environment, stdout=sys.stderr, stderr=sys.stderr, check=False)
    except OSError as error:
        raise PerformanceTraceError(f"could not run sbt: {error}") from error
    if completed.returncode != 0:
        raise PerformanceTraceError(f"performance simulation failed with exit code {completed.returncode}")


def _write_index(output_dir: Path, scenarios: Sequence[Scenario], graph_files: dict[str, dict[str, str | None]]) -> Path:
    lines = [
        "# Generated HJPEG Performance Traces",
        "",
        "Open `trace.json` in [Perfetto](https://ui.perfetto.dev/) for the transaction timeline.",
        "Green slices are transfers, red slices are downstream backpressure, gray slices are upstream starvation, and idle slices have neither valid nor ready asserted.",
        "",
        "These are deterministic simulation measurements at an assumed 100 MHz clock. They do not prove Vivado timing closure or KV260 hardware throughput.",
        "",
        "## Artifacts",
        "",
        "- [Perfetto trace](trace.json)",
        "- [Metrics JSON](metrics.json)",
        "- [Metrics CSV](metrics.csv)",
        "- [Scenario capture](scenarios.csv)",
        "- [Ready/valid samples](samples.csv)",
        "",
        "## Scenario graphs",
        "",
    ]
    for scenario in scenarios:
        graph = graph_files[scenario.name]
        links = [f"[Mermaid]({graph['mermaid']})", f"[DOT]({graph['dot']})"]
        if graph["svg"] is not None:
            links.append(f"[SVG]({graph['svg']})")
        lines.append(f"- **{scenario.name}:** {' · '.join(links)}")
    lines.extend(["", "Regenerate with:", "", "```sh", "./scripts/dev/generate-performance-trace", "```", ""])
    path = output_dir / "index.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def generate_artifacts(
    output_dir: Path,
    scenarios: Sequence[Scenario],
    samples: Sequence[Sample],
    *,
    dot_command: str | None,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    clean_previous_artifacts(output_dir)
    write_capture(output_dir, scenarios, samples)
    metrics = calculate_metrics(scenarios, samples)
    metrics_path = output_dir / "metrics.json"
    metrics_path.write_text(json.dumps(metrics, allow_nan=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_metrics_csv(output_dir / "metrics.csv", metrics)
    trace_path = output_dir / "trace.json"
    trace_path.write_text(json.dumps(render_perfetto(scenarios, samples, metrics), allow_nan=False, separators=(",", ":")) + "\n", encoding="utf-8")

    graph_files: dict[str, dict[str, str | None]] = {}
    for scenario in scenarios:
        scenario_metrics = _scenario_metric(metrics, scenario.name)
        stem = f"pipeline-{scenario.name}"
        mermaid_path = output_dir / f"{stem}.mmd"
        dot_path = output_dir / f"{stem}.dot"
        svg_path = output_dir / f"{stem}.svg" if dot_command is not None else None
        mermaid_path.write_text(render_graph_mermaid(scenario_metrics), encoding="utf-8")
        dot_path.write_text(render_graph_dot(scenario_metrics), encoding="utf-8")
        if svg_path is not None:
            completed = subprocess.run([dot_command, "-Tsvg", str(dot_path), "-o", str(svg_path)], check=False)
            if completed.returncode != 0:
                raise PerformanceTraceError(f"Graphviz failed for {scenario.name} with exit code {completed.returncode}")
        graph_files[scenario.name] = {"mermaid": mermaid_path.name, "dot": dot_path.name, "svg": svg_path.name if svg_path else None}

    index_path = _write_index(output_dir, scenarios, graph_files)
    files = [
        output_dir / "scenarios.csv",
        output_dir / "samples.csv",
        metrics_path,
        output_dir / "metrics.csv",
        trace_path,
        index_path,
    ]
    for graph in graph_files.values():
        files.extend(output_dir / value for value in graph.values() if value is not None)
    report: dict[str, object] = {
        "schema_version": 1,
        "output_dir": str(output_dir.resolve()),
        "index": str(index_path.resolve()),
        "trace": str(trace_path.resolve()),
        "metrics": str(metrics_path.resolve()),
        "scenarios": [scenario.name for scenario in scenarios],
        "graphs": graph_files,
        "files": [str(path.resolve()) for path in files],
    }
    manifest_path = output_dir / "manifest.json"
    report["files"].append(str(manifest_path.resolve()))
    manifest_path.write_text(json.dumps(report, allow_nan=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate Perfetto traces and performance graphs from HJPEG simulation.")
    parser.add_argument("--scenario", action="append", choices=SUPPORTED_SCENARIOS, help="scenario to include; repeat to select several")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="generated artifact directory")
    parser.add_argument("--capture-dir", type=Path, help="reuse scenarios.csv and samples.csv instead of running simulation")
    parser.add_argument("--dot", default="dot", help="Graphviz dot executable")
    parser.add_argument("--no-svg", action="store_true", help="emit Mermaid and DOT graphs without SVG rendering")
    parser.add_argument("--json", action="store_true", help="print the generation report as strict JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    output_dir = args.output_dir if args.output_dir.is_absolute() else repo_root / args.output_dir
    requested = tuple(dict.fromkeys(args.scenario or SUPPORTED_SCENARIOS))
    try:
        if args.capture_dir is not None:
            capture_dir = args.capture_dir if args.capture_dir.is_absolute() else Path.cwd() / args.capture_dir
            all_scenarios, all_samples = read_capture(capture_dir)
        else:
            with tempfile.TemporaryDirectory(prefix="hjpeg-performance-capture-") as temporary:
                capture_dir = Path(temporary)
                _run_simulation(repo_root, capture_dir, requested)
                all_scenarios, all_samples = read_capture(capture_dir)

        by_name = {scenario.name: scenario for scenario in all_scenarios}
        missing = [name for name in requested if name not in by_name]
        if missing:
            raise PerformanceTraceError(f"capture is missing requested scenarios: {', '.join(missing)}")
        scenarios = [by_name[name] for name in requested]
        samples = [sample for sample in all_samples if sample.scenario in requested]

        dot_command: str | None = None
        if not args.no_svg:
            dot_command = shutil.which(args.dot)
            if dot_command is None:
                print(f"WARNING: Graphviz executable {args.dot!r} not found; emitting Mermaid and DOT only", file=sys.stderr)
        report = generate_artifacts(output_dir, scenarios, samples, dot_command=dot_command)
        if args.json:
            print(json.dumps(report, allow_nan=False, indent=2, sort_keys=True))
        else:
            print(f"Generated performance trace: {report['index']}")
            print(f"Scenarios: {', '.join(requested)}")
        return 0
    except PerformanceTraceError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
