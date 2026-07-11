#!/usr/bin/env python3

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import generate_design_graphs


def fixture_ast() -> dict[str, object]:
    return {
        "type": "NETLIST",
        "modulesp": [
            {
                "type": "MODULE",
                "name": "Top",
                "addr": "top-address",
                "stmtsp": [
                    {
                        "type": "CELL",
                        "name": "left",
                        "modp": "worker-address",
                        "pinsp": [],
                    },
                    {
                        "type": "CELL",
                        "name": "right",
                        "modp": "worker-address",
                        "pinsp": [],
                    },
                    {
                        "type": "CELL",
                        "name": "store",
                        "modp": "memory-address",
                        "pinsp": [],
                    },
                ],
            },
            {
                "type": "MODULE",
                "name": "Worker",
                "addr": "worker-address",
                "stmtsp": [
                    {
                        "type": "BEGIN",
                        "stmtsp": [
                            {
                                "type": "CELL",
                                "name": "leaf",
                                "modp": "leaf-address",
                                "pinsp": [],
                            }
                        ],
                    }
                ],
            },
            {
                "type": "MODULE",
                "name": "Leaf",
                "addr": "leaf-address",
                "stmtsp": [],
            },
            {
                "type": "MODULE",
                "name": "mem_16x8",
                "addr": "memory-address",
                "stmtsp": [],
            },
        ],
    }


class GenerateDesignGraphsTest(unittest.TestCase):
    def test_parses_direct_and_nested_cells(self) -> None:
        definitions = generate_design_graphs.parse_verilator_ast(fixture_ast())

        self.assertEqual(
            definitions["Top"].instances,
            (
                generate_design_graphs.Instance("left", "Worker"),
                generate_design_graphs.Instance("right", "Worker"),
                generate_design_graphs.Instance("store", "mem_16x8"),
            ),
        )
        self.assertEqual(
            definitions["Worker"].instances,
            (generate_design_graphs.Instance("leaf", "Leaf"),),
        )

    def test_rejects_unresolved_cell_targets(self) -> None:
        ast = fixture_ast()
        modules = ast["modulesp"]
        assert isinstance(modules, list)
        top = modules[0]
        assert isinstance(top, dict)
        statements = top["stmtsp"]
        assert isinstance(statements, list)
        cell = statements[0]
        assert isinstance(cell, dict)
        cell["modp"] = "missing-address"

        with self.assertRaisesRegex(generate_design_graphs.GraphGenerationError, "unresolved target"):
            generate_design_graphs.parse_verilator_ast(ast)

    def test_builds_instance_hierarchy_with_repeated_module_types(self) -> None:
        definitions = generate_design_graphs.parse_verilator_ast(fixture_ast())
        hierarchy = generate_design_graphs.build_hierarchy(definitions, "Top")
        paths = [node.path for node in generate_design_graphs.iter_hierarchy(hierarchy)]

        self.assertEqual(
            paths,
            [
                "Top",
                "Top.left",
                "Top.left.leaf",
                "Top.right",
                "Top.right.leaf",
                "Top.store",
            ],
        )

    def test_dependency_edges_group_instance_names(self) -> None:
        definitions = generate_design_graphs.parse_verilator_ast(fixture_ast())
        modules = generate_design_graphs.reachable_modules(definitions, "Top")

        self.assertEqual(
            generate_design_graphs.dependency_edges(definitions, modules),
            (
                ("Top", "Worker", ("left", "right")),
                ("Top", "mem_16x8", ("store",)),
                ("Worker", "Leaf", ("leaf",)),
            ),
        )

    def test_mermaid_and_dot_outputs_are_deterministic(self) -> None:
        definitions = generate_design_graphs.parse_verilator_ast(fixture_ast())
        hierarchy = generate_design_graphs.build_hierarchy(definitions, "Top")

        hierarchy_mermaid = generate_design_graphs.render_hierarchy_mermaid(hierarchy)
        dependencies_mermaid = generate_design_graphs.render_dependencies_mermaid(definitions, "Top")
        dependencies_dot = generate_design_graphs.render_dependencies_dot(definitions, "Top")

        self.assertIn('n1["left<br/>Worker"]', hierarchy_mermaid)
        self.assertIn("m1 -->|left, right| m2", dependencies_mermaid)
        self.assertIn('label="left, right"', dependencies_dot)
        self.assertIn("class m3 memory", dependencies_mermaid)

    def test_hierarchy_stops_recursive_instantiation(self) -> None:
        definitions = {
            "Loop": generate_design_graphs.ModuleDefinition(
                "Loop",
                (generate_design_graphs.Instance("again", "Loop"),),
            )
        }
        hierarchy = generate_design_graphs.build_hierarchy(definitions, "Loop")

        self.assertEqual(len(hierarchy.children), 1)
        self.assertTrue(hierarchy.children[0].recursive)
        self.assertEqual(hierarchy.children[0].children, ())

    def test_rejects_missing_top(self) -> None:
        definitions = generate_design_graphs.parse_verilator_ast(fixture_ast())

        with self.assertRaisesRegex(generate_design_graphs.GraphGenerationError, "top module 'Missing' not found"):
            generate_design_graphs.build_hierarchy(definitions, "Missing")

    def test_slug_separates_camel_case_names(self) -> None:
        self.assertEqual(
            generate_design_graphs._slug("HjpegAxiStreamCore"),
            "hjpeg-axi-stream-core",
        )

    def test_generates_artifacts_without_graphviz(self) -> None:
        definitions = generate_design_graphs.parse_verilator_ast(fixture_ast())
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            report = generate_design_graphs.generate_graphs(
                definitions,
                "Top",
                output_dir,
                ("Worker", "Missing"),
                dot_command=None,
                max_depth=64,
                max_instances=100,
            )

            self.assertEqual(report["reachable_module_types"], 4)
            self.assertEqual(report["hierarchy_instances"], 6)
            self.assertEqual(report["missing_focus_modules"], ["Missing"])
            self.assertTrue((output_dir / "module-hierarchy.mmd").is_file())
            self.assertTrue((output_dir / "module-dependencies.dot").is_file())
            self.assertTrue((output_dir / "focus-worker.mmd").is_file())
            self.assertTrue((output_dir / "index.md").is_file())
            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["top"], "Top")
            self.assertIsNone(manifest["artifacts"][0]["svg"])

    def test_removes_only_files_from_previous_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            stale = output_dir / "focus-old.dot"
            unrelated = output_dir / "notes.txt"
            stale.write_text("old", encoding="utf-8")
            unrelated.write_text("keep", encoding="utf-8")
            (output_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "index": str((output_dir / "missing-index.md").resolve()),
                        "artifacts": [{"dot": str(stale.resolve()), "svg": None}],
                    }
                ),
                encoding="utf-8",
            )

            generate_design_graphs.clean_previous_artifacts(output_dir)

            self.assertFalse(stale.exists())
            self.assertTrue(unrelated.exists())
            self.assertFalse((output_dir / "manifest.json").exists())

    def test_cli_reads_existing_ast(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_dir = Path(temporary)
            ast_path = temporary_dir / "tree.json"
            output_dir = temporary_dir / "graphs"
            ast_path.write_text(json.dumps(fixture_ast()), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = generate_design_graphs.main(
                    [
                        "--ast-json",
                        str(ast_path),
                        "--top",
                        "Top",
                        "--output-dir",
                        str(output_dir),
                        "--focus",
                        "Worker",
                        "--no-svg",
                        "--json",
                    ]
                )

            self.assertEqual(exit_code, 0)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["top"], "Top")
            self.assertEqual(report["focus_modules"], ["Worker"])
            self.assertTrue((output_dir / "manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
