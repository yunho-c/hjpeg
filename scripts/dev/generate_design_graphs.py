#!/usr/bin/env python3

"""Generate HJPEG module hierarchy and dependency graphs.

The graph source is Verilator's elaborated SystemVerilog JSON rather than the
Scala source. This captures the hardware modules that Chisel actually emitted,
including parameterized module variants and generated memories.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence


DEFAULT_TOP = "HjpegKv260AxiLiteTop"
DEFAULT_GENERATED_DIR = Path("generated-kv260-axi-lite-top")
DEFAULT_OUTPUT_DIR = Path("build/design-graphs")
DEFAULT_FOCUS_MODULES = (
    "HjpegAxiStreamCore",
    "HjpegCore",
    "JpegBlockTransformStage",
    "JpegMcuStreamEncoderStage",
)


class GraphGenerationError(RuntimeError):
    """Raised when elaboration or graph extraction cannot produce a valid graph."""


@dataclass(frozen=True, order=True)
class Instance:
    name: str
    module_name: str


@dataclass(frozen=True)
class ModuleDefinition:
    name: str
    instances: tuple[Instance, ...]


@dataclass(frozen=True)
class HierarchyNode:
    instance_name: str
    module_name: str
    path: str
    children: tuple["HierarchyNode", ...]
    recursive: bool = False


@dataclass(frozen=True)
class GraphArtifact:
    name: str
    title: str
    mermaid_path: Path
    dot_path: Path
    svg_path: Path | None


def _walk_json(value: object) -> Iterator[dict[str, object]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def parse_verilator_ast(ast: object) -> dict[str, ModuleDefinition]:
    """Extract module definitions and direct instances from Verilator JSON."""

    if not isinstance(ast, dict):
        raise GraphGenerationError("Verilator AST root must be a JSON object")
    raw_modules = ast.get("modulesp")
    if not isinstance(raw_modules, list):
        raise GraphGenerationError("Verilator AST does not contain a modulesp array")

    address_to_name: dict[str, str] = {}
    named_modules: list[dict[str, object]] = []
    for raw_module in raw_modules:
        if not isinstance(raw_module, dict) or raw_module.get("type") != "MODULE":
            continue
        name = raw_module.get("name")
        address = raw_module.get("addr")
        if not isinstance(name, str) or not name:
            raise GraphGenerationError("Verilator MODULE entry has no name")
        if isinstance(address, str):
            address_to_name[address] = name
        named_modules.append(raw_module)

    definitions: dict[str, ModuleDefinition] = {}
    unresolved: list[str] = []
    for raw_module in named_modules:
        module_name = str(raw_module["name"])
        instances: list[Instance] = []
        for node in _walk_json(raw_module.get("stmtsp", [])):
            if node.get("type") != "CELL":
                continue
            instance_name = node.get("name")
            target_name = node.get("moduleName")
            if not isinstance(target_name, str) or not target_name:
                module_reference = node.get("modp")
                target_name = address_to_name.get(module_reference) if isinstance(module_reference, str) else None
            if not isinstance(instance_name, str) or not instance_name:
                unresolved.append(f"{module_name}: unnamed CELL")
            elif not isinstance(target_name, str) or not target_name:
                unresolved.append(f"{module_name}.{instance_name}: unresolved target module")
            else:
                instances.append(Instance(instance_name, target_name))

        if module_name in definitions:
            raise GraphGenerationError(f"duplicate Verilator module definition: {module_name}")
        definitions[module_name] = ModuleDefinition(module_name, tuple(sorted(set(instances))))

    if unresolved:
        joined = "; ".join(sorted(unresolved))
        raise GraphGenerationError(f"could not resolve all module instances: {joined}")
    if not definitions:
        raise GraphGenerationError("Verilator AST contains no module definitions")
    return definitions


def load_verilator_ast(path: Path) -> dict[str, ModuleDefinition]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise GraphGenerationError(f"could not read Verilator AST {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise GraphGenerationError(f"could not parse Verilator AST {path}: {error}") from error
    return parse_verilator_ast(parsed)


def build_hierarchy(
    definitions: dict[str, ModuleDefinition],
    top: str,
    *,
    max_depth: int = 64,
    max_instances: int = 10_000,
) -> HierarchyNode:
    if top not in definitions:
        available = ", ".join(sorted(definitions))
        raise GraphGenerationError(f"top module {top!r} not found; available modules: {available}")
    if max_depth < 0:
        raise GraphGenerationError("max_depth must be nonnegative")
    if max_instances <= 0:
        raise GraphGenerationError("max_instances must be positive")

    node_count = 0

    def visit(module_name: str, instance_name: str, path: str, ancestors: tuple[str, ...]) -> HierarchyNode:
        nonlocal node_count
        node_count += 1
        if node_count > max_instances:
            raise GraphGenerationError(
                f"hierarchy exceeds max instance count {max_instances}; increase --max-instances if intentional"
            )
        recursive = module_name in ancestors
        depth = len(ancestors)
        if recursive or depth >= max_depth:
            return HierarchyNode(instance_name, module_name, path, (), recursive=recursive)

        definition = definitions.get(module_name)
        children: list[HierarchyNode] = []
        if definition is not None:
            for instance in definition.instances:
                child_path = f"{path}.{instance.name}"
                children.append(
                    visit(
                        instance.module_name,
                        instance.name,
                        child_path,
                        ancestors + (module_name,),
                    )
                )
        return HierarchyNode(instance_name, module_name, path, tuple(children), recursive=recursive)

    return visit(top, top, top, ())


def iter_hierarchy(root: HierarchyNode) -> Iterator[HierarchyNode]:
    yield root
    for child in root.children:
        yield from iter_hierarchy(child)


def reachable_modules(definitions: dict[str, ModuleDefinition], root: str) -> tuple[str, ...]:
    if root not in definitions:
        raise GraphGenerationError(f"focus module {root!r} not found")
    visited: set[str] = set()
    pending = [root]
    while pending:
        module_name = pending.pop()
        if module_name in visited:
            continue
        visited.add(module_name)
        definition = definitions.get(module_name)
        if definition is not None:
            pending.extend(instance.module_name for instance in definition.instances)
    return tuple(sorted(visited))


def dependency_edges(
    definitions: dict[str, ModuleDefinition],
    modules: Iterable[str],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    selected = set(modules)
    grouped: dict[tuple[str, str], list[str]] = {}
    for source in sorted(selected):
        definition = definitions.get(source)
        if definition is None:
            continue
        for instance in definition.instances:
            if instance.module_name in selected:
                grouped.setdefault((source, instance.module_name), []).append(instance.name)
    return tuple(
        (source, target, tuple(sorted(names)))
        for (source, target), names in sorted(grouped.items())
    )


def _mermaid_text(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
    )


def _dot_text(value: str) -> str:
    return json.dumps(value)


def render_hierarchy_mermaid(root: HierarchyNode) -> str:
    lines = ["flowchart TD"]
    nodes = list(iter_hierarchy(root))
    node_ids = {node.path: f"n{index}" for index, node in enumerate(nodes)}
    for node in nodes:
        label = (
            _mermaid_text(node.module_name)
            if node.path == root.path
            else f"{_mermaid_text(node.instance_name)}<br/>{_mermaid_text(node.module_name)}"
        )
        if node.recursive:
            label += "<br/>(recursive)"
        lines.append(f'  {node_ids[node.path]}["{label}"]')
    for node in nodes:
        for child in node.children:
            lines.append(f"  {node_ids[node.path]} --> {node_ids[child.path]}")
    lines.append("  classDef root fill:#dbeafe,stroke:#2563eb,stroke-width:2px")
    lines.append(f"  class {node_ids[root.path]} root")
    memory_ids = [node_ids[node.path] for node in nodes if node.module_name.startswith("mem_")]
    if memory_ids:
        lines.append("  classDef memory fill:#fef3c7,stroke:#d97706")
        lines.append(f"  class {','.join(memory_ids)} memory")
    return "\n".join(lines) + "\n"


def render_hierarchy_dot(root: HierarchyNode) -> str:
    lines = [
        "digraph module_hierarchy {",
        "  rankdir=LR;",
        '  graph [fontname="Helvetica", bgcolor="transparent"];',
        '  node [shape=box, style="rounded,filled", fillcolor="#f8fafc", color="#64748b", fontname="Helvetica"];',
        '  edge [color="#64748b"];',
    ]
    nodes = list(iter_hierarchy(root))
    node_ids = {node.path: f"n{index}" for index, node in enumerate(nodes)}
    for node in nodes:
        label = node.module_name if node.path == root.path else f"{node.instance_name}\n{node.module_name}"
        attributes = [f"label={_dot_text(label)}"]
        if node.path == root.path:
            attributes.extend(['fillcolor="#dbeafe"', 'color="#2563eb"', "penwidth=2"])
        elif node.module_name.startswith("mem_"):
            attributes.extend(["shape=cylinder", 'fillcolor="#fef3c7"', 'color="#d97706"'])
        if node.recursive:
            attributes.extend(['style="rounded,dashed"', 'color="#dc2626"'])
        lines.append(f"  {node_ids[node.path]} [{', '.join(attributes)}];")
    for node in nodes:
        for child in node.children:
            lines.append(f"  {node_ids[node.path]} -> {node_ids[child.path]};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_dependencies_mermaid(
    definitions: dict[str, ModuleDefinition],
    root: str,
) -> str:
    modules = reachable_modules(definitions, root)
    module_ids = {module: f"m{index}" for index, module in enumerate(modules)}
    lines = ["flowchart LR"]
    for module in modules:
        lines.append(f'  {module_ids[module]}["{_mermaid_text(module)}"]')
    for source, target, names in dependency_edges(definitions, modules):
        label = ", ".join(names)
        lines.append(
            f"  {module_ids[source]} -->|{_mermaid_text(label)}| {module_ids[target]}"
        )
    lines.append("  classDef root fill:#dbeafe,stroke:#2563eb,stroke-width:2px")
    lines.append(f"  class {module_ids[root]} root")
    memory_ids = [module_ids[module] for module in modules if module.startswith("mem_")]
    if memory_ids:
        lines.append("  classDef memory fill:#fef3c7,stroke:#d97706")
        lines.append(f"  class {','.join(memory_ids)} memory")
    return "\n".join(lines) + "\n"


def render_dependencies_dot(
    definitions: dict[str, ModuleDefinition],
    root: str,
) -> str:
    modules = reachable_modules(definitions, root)
    module_ids = {module: f"m{index}" for index, module in enumerate(modules)}
    lines = [
        "digraph module_dependencies {",
        "  rankdir=LR;",
        '  graph [fontname="Helvetica", bgcolor="transparent"];',
        '  node [shape=box, style="rounded,filled", fillcolor="#f8fafc", color="#64748b", fontname="Helvetica"];',
        '  edge [color="#64748b", fontname="Helvetica", fontsize=10];',
    ]
    for module in modules:
        attributes = [f"label={_dot_text(module)}"]
        if module == root:
            attributes.extend(['fillcolor="#dbeafe"', 'color="#2563eb"', "penwidth=2"])
        elif module.startswith("mem_"):
            attributes.extend(["shape=cylinder", 'fillcolor="#fef3c7"', 'color="#d97706"'])
        lines.append(f"  {module_ids[module]} [{', '.join(attributes)}];")
    for source, target, names in dependency_edges(definitions, modules):
        lines.append(
            f"  {module_ids[source]} -> {module_ids[target]} [label={_dot_text(', '.join(names))}];"
        )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _slug(value: str) -> str:
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", value)
    separated = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "-", separated)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", separated).strip("-").lower()
    return slug or "graph"


def clean_previous_artifacts(output_dir: Path) -> None:
    """Remove only files named by the previous generator manifest."""

    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(manifest, dict):
        return

    candidates: list[object] = [manifest.get("index")]
    artifacts = manifest.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, dict):
                candidates.extend(artifact.get(key) for key in ("mermaid", "dot", "svg"))

    resolved_output = output_dir.resolve()
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        path = Path(candidate)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.parent == resolved_output and resolved.is_file():
            resolved.unlink()
    manifest_path.unlink(missing_ok=True)


def _run(command: Sequence[str], *, cwd: Path, description: str) -> None:
    print(f"{description}: {' '.join(command)}", file=sys.stderr)
    try:
        completed = subprocess.run(command, cwd=cwd, check=False)
    except OSError as error:
        raise GraphGenerationError(f"could not run {command[0]}: {error}") from error
    if completed.returncode != 0:
        raise GraphGenerationError(f"{description} failed with exit code {completed.returncode}")


def _tool_version(command: str) -> str | None:
    try:
        completed = subprocess.run(
            [command, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        return None
    output = completed.stdout.strip() or completed.stderr.strip()
    return output.splitlines()[0] if output else None


def generate_verilator_ast(
    generated_dir: Path,
    top: str,
    output_path: Path,
    metadata_path: Path,
    verilator: str,
) -> None:
    filelist = generated_dir / "filelist.f"
    if not filelist.is_file():
        raise GraphGenerationError(
            f"missing {filelist}; run elaboration or omit --skip-elaboration"
        )
    command = [
        verilator,
        "--json-only",
        "--top-module",
        top,
        "-f",
        filelist.name,
        "--json-only-output",
        str(output_path),
        "--json-only-meta-output",
        str(metadata_path),
    ]
    _run(command, cwd=generated_dir, description="Verilator elaboration")


def write_graph_artifact(
    output_dir: Path,
    name: str,
    title: str,
    mermaid: str,
    dot: str,
    *,
    dot_command: str | None,
) -> GraphArtifact:
    mermaid_path = output_dir / f"{name}.mmd"
    dot_path = output_dir / f"{name}.dot"
    svg_path = output_dir / f"{name}.svg" if dot_command is not None else None
    mermaid_path.write_text(mermaid, encoding="utf-8")
    dot_path.write_text(dot, encoding="utf-8")
    if svg_path is not None:
        _run(
            [dot_command, "-Tsvg", str(dot_path), "-o", str(svg_path)],
            cwd=output_dir,
            description=f"Graphviz render {name}",
        )
    return GraphArtifact(name, title, mermaid_path, dot_path, svg_path)


def write_index(
    output_dir: Path,
    top: str,
    artifacts: Sequence[GraphArtifact],
    module_count: int,
    instance_count: int,
) -> Path:
    lines = [
        "# Generated HJPEG Design Graphs",
        "",
        f"Top module: `{top}`",
        "",
        f"Reachable module types: {module_count}",
        "",
        f"Hierarchy instances: {instance_count}",
        "",
        "These graphs describe elaborated module ownership and instantiation.",
        "They do not by themselves describe cycle-by-cycle behavior or signal",
        "direction through each module.",
        "",
        "## Graphs",
        "",
    ]
    for artifact in artifacts:
        links = [f"[Mermaid]({artifact.mermaid_path.name})", f"[DOT]({artifact.dot_path.name})"]
        if artifact.svg_path is not None:
            links.append(f"[SVG]({artifact.svg_path.name})")
        lines.append(f"- **{artifact.title}:** {' · '.join(links)}")
    lines.extend(
        [
            "",
            "Regenerate from the repository root with:",
            "",
            "```sh",
            "./scripts/dev/generate-design-graphs",
            "```",
            "",
        ]
    )
    index_path = output_dir / "index.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    return index_path


def generate_graphs(
    definitions: dict[str, ModuleDefinition],
    top: str,
    output_dir: Path,
    focus_modules: Sequence[str],
    *,
    dot_command: str | None,
    max_depth: int,
    max_instances: int,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    clean_previous_artifacts(output_dir)
    hierarchy = build_hierarchy(
        definitions,
        top,
        max_depth=max_depth,
        max_instances=max_instances,
    )
    reachable = reachable_modules(definitions, top)
    artifacts: list[GraphArtifact] = []
    artifacts.append(
        write_graph_artifact(
            output_dir,
            "module-hierarchy",
            "Full instance hierarchy",
            render_hierarchy_mermaid(hierarchy),
            render_hierarchy_dot(hierarchy),
            dot_command=dot_command,
        )
    )
    artifacts.append(
        write_graph_artifact(
            output_dir,
            "module-dependencies",
            "Reachable module dependencies",
            render_dependencies_mermaid(definitions, top),
            render_dependencies_dot(definitions, top),
            dot_command=dot_command,
        )
    )

    missing_focus: list[str] = []
    for focus in focus_modules:
        if focus not in definitions:
            missing_focus.append(focus)
            continue
        name = f"focus-{_slug(focus)}"
        artifacts.append(
            write_graph_artifact(
                output_dir,
                name,
                f"{focus} dependency cone",
                render_dependencies_mermaid(definitions, focus),
                render_dependencies_dot(definitions, focus),
                dot_command=dot_command,
            )
        )

    instance_count = sum(1 for _ in iter_hierarchy(hierarchy))
    index_path = write_index(
        output_dir,
        top,
        artifacts,
        module_count=len(reachable),
        instance_count=instance_count,
    )
    report: dict[str, object] = {
        "top": top,
        "module_definitions": len(definitions),
        "reachable_module_types": len(reachable),
        "hierarchy_instances": instance_count,
        "focus_modules": list(focus_modules),
        "missing_focus_modules": missing_focus,
        "index": str(index_path.resolve()),
        "artifacts": [
            {
                "name": artifact.name,
                "title": artifact.title,
                "mermaid": str(artifact.mermaid_path.resolve()),
                "dot": str(artifact.dot_path.resolve()),
                "svg": str(artifact.svg_path.resolve()) if artifact.svg_path is not None else None,
            }
            for artifact in artifacts
        ],
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(report, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate Mermaid, DOT, and SVG graphs from elaborated HJPEG SystemVerilog."
    )
    parser.add_argument("--top", default=DEFAULT_TOP, help="top SystemVerilog module")
    parser.add_argument(
        "--generated-dir",
        type=Path,
        default=DEFAULT_GENERATED_DIR,
        help="directory containing generated SystemVerilog and filelist.f",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="generated graph output directory",
    )
    parser.add_argument(
        "--focus",
        action="append",
        default=None,
        metavar="MODULE",
        help="emit a dependency cone for MODULE; repeat to select several",
    )
    parser.add_argument(
        "--skip-elaboration",
        action="store_true",
        help="reuse generated SystemVerilog instead of running the Chisel elaborator",
    )
    parser.add_argument(
        "--ast-json",
        type=Path,
        help="read an existing Verilator JSON AST and skip Chisel/Verilator execution",
    )
    parser.add_argument("--verilator", default="verilator", help="Verilator executable")
    parser.add_argument("--dot", default="dot", help="Graphviz dot executable")
    parser.add_argument("--no-svg", action="store_true", help="emit Mermaid and DOT without rendering SVG")
    parser.add_argument("--keep-json", action="store_true", help="keep Verilator AST files in the output directory")
    parser.add_argument("--max-depth", type=int, default=64, help="maximum expanded hierarchy depth")
    parser.add_argument("--max-instances", type=int, default=10_000, help="maximum expanded hierarchy node count")
    parser.add_argument("--json", action="store_true", help="print the generation report as strict JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parents[2]
    generated_dir = args.generated_dir if args.generated_dir.is_absolute() else repo_root / args.generated_dir
    output_dir = args.output_dir if args.output_dir.is_absolute() else repo_root / args.output_dir
    focus_modules = tuple(args.focus) if args.focus else DEFAULT_FOCUS_MODULES

    try:
        if args.ast_json is not None:
            ast_path = args.ast_json if args.ast_json.is_absolute() else Path.cwd() / args.ast_json
            definitions = load_verilator_ast(ast_path)
            verilator_version = None
        else:
            if not args.skip_elaboration:
                _run(
                    ["sbt", "runMain hjpeg.ElaborateKv260AxiLiteTop"],
                    cwd=repo_root,
                    description="Chisel elaboration",
                )
            output_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="hjpeg-design-graphs-") as temporary:
                temporary_dir = Path(temporary)
                ast_path = temporary_dir / "hjpeg.tree.json"
                metadata_path = temporary_dir / "hjpeg.tree.meta.json"
                generate_verilator_ast(
                    generated_dir,
                    args.top,
                    ast_path,
                    metadata_path,
                    args.verilator,
                )
                definitions = load_verilator_ast(ast_path)
                if args.keep_json:
                    shutil.copy2(ast_path, output_dir / ast_path.name)
                    shutil.copy2(metadata_path, output_dir / metadata_path.name)
            verilator_version = _tool_version(args.verilator)

        dot_command: str | None = None
        if not args.no_svg:
            dot_command = shutil.which(args.dot)
            if dot_command is None:
                print(
                    f"WARNING: Graphviz executable {args.dot!r} not found; emitting Mermaid and DOT only",
                    file=sys.stderr,
                )

        report = generate_graphs(
            definitions,
            args.top,
            output_dir,
            focus_modules,
            dot_command=dot_command,
            max_depth=args.max_depth,
            max_instances=args.max_instances,
        )
        report["verilator_version"] = verilator_version
        report["graphviz_version"] = _tool_version(dot_command) if dot_command is not None else None
        (output_dir / "manifest.json").write_text(
            json.dumps(report, allow_nan=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if args.json:
            print(json.dumps(report, allow_nan=False, indent=2, sort_keys=True))
        else:
            print(f"Generated design graphs: {report['index']}")
            print(
                f"Reachable module types: {report['reachable_module_types']}; "
                f"hierarchy instances: {report['hierarchy_instances']}"
            )
            missing_focus = report["missing_focus_modules"]
            if isinstance(missing_focus, list) and missing_focus:
                print(f"Skipped missing focus modules: {', '.join(str(item) for item in missing_focus)}")
        return 0
    except GraphGenerationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
