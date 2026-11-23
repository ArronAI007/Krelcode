"""Incremental repo-map cache and optional syntax parser tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.repo_map import RepoMapTool
from krelcode.tools.repo_index import RepoMapIndex
from krelcode.tools.symbols import parse_symbols


def run(coro):
    return asyncio.run(coro)


def write_python(path: Path, name: str = "work") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"class {name.title()}:\n    def run(self):\n        pass\n", encoding="utf-8")


def test_repeat_build_reuses_every_unchanged_file(tmp_path):
    write_python(tmp_path / "pkg" / "one.py", "one")
    write_python(tmp_path / "pkg" / "two.py", "two")
    index = RepoMapIndex()

    first = index.build(tmp_path, tmp_path)
    second = index.build(tmp_path, tmp_path)

    assert first.parsed_files == 2
    assert first.reused_files == 0
    assert second.parsed_files == 0
    assert second.reused_files == 2
    assert second.removed_files == 0
    assert [entry.relative_path for entry in second.files] == [
        "pkg/one.py", "pkg/two.py",
    ]


def test_modified_file_is_the_only_one_reparsed(tmp_path):
    one = tmp_path / "one.py"
    two = tmp_path / "two.py"
    write_python(one, "one")
    write_python(two, "two")
    index = RepoMapIndex()
    index.build(tmp_path, tmp_path)

    one.write_text("def replacement(value):\n    return value\n", encoding="utf-8")
    snapshot = index.build(tmp_path, tmp_path)

    assert snapshot.parsed_files == 1
    assert snapshot.reused_files == 1
    by_path = {entry.relative_path: entry for entry in snapshot.files}
    assert [symbol[2] for symbol in by_path["one.py"].symbols] == [
        "def replacement(value)",
    ]
    assert "class Two" in [symbol[2] for symbol in by_path["two.py"].symbols]


def test_deleted_file_is_evicted_from_cache(tmp_path):
    with_file = tmp_path / "with_file.py"
    removed_file = tmp_path / "removed.py"
    write_python(with_file, "kept")
    write_python(removed_file, "removed")
    index = RepoMapIndex()
    index.build(tmp_path, tmp_path)

    removed_file.unlink()
    snapshot = index.build(tmp_path, tmp_path)

    assert snapshot.removed_files == 1
    assert [entry.relative_path for entry in snapshot.files] == ["with_file.py"]
    assert snapshot.reused_files == 1


def test_subdirectory_build_does_not_evict_outside_cache(tmp_path):
    write_python(tmp_path / "pkg" / "inside.py", "inside")
    write_python(tmp_path / "other.py", "outside")
    index = RepoMapIndex()
    index.build(tmp_path, tmp_path)

    scoped = index.build(tmp_path / "pkg", tmp_path)
    full_again = index.build(tmp_path, tmp_path)

    assert [entry.relative_path for entry in scoped.files] == ["pkg/inside.py"]
    assert scoped.parsed_files == 0
    assert scoped.reused_files == 1
    assert [entry.relative_path for entry in full_again.files] == [
        "other.py", "pkg/inside.py",
    ]
    assert full_again.parsed_files == 0
    assert full_again.reused_files == 2


def test_repo_map_tool_exposes_incremental_metadata(tmp_path):
    write_python(tmp_path / "mod.py", "mod")
    tool = RepoMapTool()
    ctx = ToolContext(workspace_root=tmp_path)

    first = run(tool.execute({}, ctx))
    second = run(tool.execute({}, ctx))

    assert first.data["incremental"] == {
        "parsed_files": 1, "reused_files": 0, "removed_files": 0,
    }
    assert second.data["incremental"] == {
        "parsed_files": 0, "reused_files": 1, "removed_files": 0,
    }
    assert second.data["parser_backends"]["python-ast"] == 1


def test_parser_backend_metadata_includes_python_and_javascript(tmp_path):
    write_python(tmp_path / "mod.py", "mod")
    (tmp_path / "app.js").write_text(
        "export class App {}\nconst run = () => 1;\n", encoding="utf-8")
    (tmp_path / "types.ts").write_text(
        "export interface User { name: string }\nexport type Id = string;\n",
        encoding="utf-8",
    )

    result = run(RepoMapTool().execute({}, ToolContext(workspace_root=tmp_path)))
    backends = result.data["parser_backends"]
    js_backend = parse_symbols(
        Path("app.js"), (tmp_path / "app.js").read_text(encoding="utf-8")).backend
    ts_backend = parse_symbols(
        Path("types.ts"), (tmp_path / "types.ts").read_text(encoding="utf-8")).backend
    expected = {"python-ast": 1}
    expected[js_backend] = expected.get(js_backend, 0) + 1
    expected[ts_backend] = expected.get(ts_backend, 0) + 1

    assert backends == expected
    assert sum(backends.values()) == 3


@pytest.mark.parametrize(
    ("filename", "source", "expected"),
    [
        (
            "component.tsx",
            "export class Panel {}\nexport function render() {}\n"
            "const onPress = () => true;\n",
            {"class Panel", "function render", "const onPress"},
        ),
        (
            "types.ts",
            "export interface User {}\nexport type Id = string;\n"
            "export enum State { Ready }\nexport namespace API {}\n",
            {"interface User", "type Id", "enum State", "namespace API"},
        ),
        (
            "module.jsx",
            "export const View = () => null;\nfunction mount() {}\n",
            {"const View", "function mount"},
        ),
    ],
)
def test_typescript_and_javascript_symbols(filename, source, expected):
    parsed = parse_symbols(Path(filename), source)
    texts = {symbol[2] for symbol in parsed.symbols}
    assert expected <= texts
    assert parsed.backend in {
        "tree-sitter-javascript", "tree-sitter-typescript", "regex",
    }


def test_tree_sitter_nested_class_and_interface_methods_when_available():
    source = (
        "export class Outer {\n"
        "  run() { return 1; }\n"
        "}\n"
        "export interface Service {\n"
        "  call(name: string): void;\n"
        "}\n"
    )
    parsed = parse_symbols(Path("service.ts"), source)
    if not parsed.backend.startswith("tree-sitter-"):
        pytest.skip("optional tree-sitter TypeScript grammar is not installed")

    rendered = {(indent, text) for indent, _, text in parsed.symbols}
    assert ("", "class Outer") in rendered
    assert ("  ", "method run") in rendered
    assert ("", "interface Service") in rendered
    assert ("  ", "method call") in rendered


def test_repo_map_skips_symlink_hardlink_binary_and_oversized(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def outside():\n    pass\n", encoding="utf-8")
    (root / "safe.py").write_text("def safe():\n    pass\n", encoding="utf-8")
    os.symlink(outside, root / "link.py")
    os.link(outside, root / "hard.py")
    (root / "binary.py").write_bytes(b"def binary():\x00\n")
    (root / "large.py").write_text(
        "#" + ("x" * 1_000_001) + "\ndef oversized():\n    pass\n",
        encoding="utf-8",
    )

    result = run(RepoMapTool().execute({}, ToolContext(workspace_root=root)))

    assert "safe.py" in result.content
    for name in ("outside.py", "link.py", "hard.py", "binary.py", "large.py"):
        assert name not in result.content
