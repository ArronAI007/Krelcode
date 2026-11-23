"""P1-4 notebook structure, redaction, and atomic-edit tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.notebook import (
    NotebookCreateTool,
    NotebookEditTool,
    NotebookInspectTool,
    NotebookOptions,
    build_notebook_tools,
)


def _notebook() -> dict:
    return {
        "cells": [
            {
                "cell_type": "markdown",
                "id": "intro",
                "metadata": {},
                "source": ["# Analysis\n", "Project notebook"],
            },
            {
                "cell_type": "code",
                "id": "load",
                "metadata": {"tags": ["keep"]},
                "execution_count": 7,
                "outputs": [{
                    "output_type": "stream",
                    "name": "stdout",
                    "text": ["API_KEY=sk-live-secret-value\n"],
                }],
                "source": ["value = 1\n", "value"],
            },
            {
                "cell_type": "code",
                "id": "other",
                "metadata": {},
                "execution_count": 8,
                "outputs": [{
                    "output_type": "display_data",
                    "data": {"text/plain": ["untouched"]},
                    "metadata": {},
                }],
                "source": ["other = 2"],
            },
        ],
        "metadata": {"kernelspec": {"name": "python3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _write(path: Path, data: dict | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1)
    path.write_text(text, encoding="utf-8")


def test_notebook_tools_are_discoverable_and_config_disable_removes_all():
    tools = build_notebook_tools(NotebookOptions())
    assert [tool.name for tool in tools] == [
        "notebook_inspect", "notebook_edit", "notebook_create"]
    assert all(tool.tool_tier.value == "discoverable" for tool in tools)
    assert build_notebook_tools(NotebookOptions(enabled=False)) == []


def test_notebook_inspect_summarizes_outputs_without_exposing_payload(tmp_path: Path):
    path = tmp_path / "analysis.ipynb"
    _write(path, _notebook())
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)

    result = asyncio.run(NotebookInspectTool(NotebookOptions()).execute(
        {"path": "analysis.ipynb", "cell_index": 1, "max_source_chars": 5000}, ctx))

    assert not result.is_error
    assert "Cell 1: code" in result.content
    assert "execution_count=7" in result.content
    assert '"stream": 1' in result.content
    assert "value = 1" in result.content
    assert "sk-live-secret-value" not in result.content
    assert "API_KEY=***" not in result.content
    assert result.data["cell_index"] == 1


def test_notebook_edit_replaces_one_cell_clears_stale_output_and_preserves_others(tmp_path: Path):
    path = tmp_path / "analysis.ipynb"
    _write(path, _notebook())
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)

    result = asyncio.run(NotebookEditTool(NotebookOptions()).execute(
        {
            "path": "analysis.ipynb",
            "operation": "replace_source",
            "cell_index": 1,
            "source": "value = 42\nvalue",
        },
        ctx,
    ))
    updated = json.loads(path.read_text(encoding="utf-8"))

    assert not result.is_error
    assert updated["cells"][1]["source"] == ["value = 42\n", "value"]
    assert updated["cells"][1]["outputs"] == []
    assert updated["cells"][1]["execution_count"] is None
    assert updated["cells"][2]["outputs"][0]["data"]["text/plain"] == ["untouched"]
    assert updated["cells"][2]["execution_count"] == 8
    assert updated["cells"][0]["metadata"] == {}


def test_notebook_edit_can_explicitly_preserve_outputs(tmp_path: Path):
    path = tmp_path / "analysis.ipynb"
    _write(path, _notebook())
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)

    result = asyncio.run(NotebookEditTool(NotebookOptions()).execute(
        {
            "path": "analysis.ipynb",
            "operation": "replace_source",
            "cell_index": 1,
            "source": "value = 3",
            "clear_outputs": False,
        },
        ctx,
    ))
    updated = json.loads(path.read_text(encoding="utf-8"))

    assert not result.is_error
    assert updated["cells"][1]["execution_count"] == 7
    assert updated["cells"][1]["outputs"][0]["output_type"] == "stream"


def test_notebook_edit_insert_delete_and_clear_outputs(tmp_path: Path):
    path = tmp_path / "analysis.ipynb"
    _write(path, _notebook())
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)
    editor = NotebookEditTool(NotebookOptions())

    inserted = asyncio.run(editor.execute(
        {
            "path": "analysis.ipynb",
            "operation": "insert",
            "cell_index": 1,
            "cell_type": "markdown",
            "source": "## Validation",
        },
        ctx,
    ))
    cleared = asyncio.run(editor.execute(
        {"path": "analysis.ipynb", "operation": "clear_outputs"},
        ctx,
    ))
    deleted = asyncio.run(editor.execute(
        {"path": "analysis.ipynb", "operation": "delete", "cell_index": 0},
        ctx,
    ))
    updated = json.loads(path.read_text(encoding="utf-8"))

    assert not inserted.is_error
    assert not cleared.is_error
    assert not deleted.is_error
    assert all(cell.get("outputs", []) == [] for cell in updated["cells"])
    assert updated["cells"][0]["cell_type"] == "markdown"
    assert len(updated["cells"]) == 3


def test_notebook_create_rejects_overwrite_and_writes_valid_nbformat(tmp_path: Path):
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)
    creator = NotebookCreateTool(NotebookOptions())
    args = {
        "path": "notebooks/new.ipynb",
        "cells": [
            {"cell_type": "markdown", "source": "# New"},
            {"cell_type": "code", "source": "print('ready')"},
        ],
        "kernel_name": "python3",
    }

    first = asyncio.run(creator.execute(args, ctx))
    second = asyncio.run(creator.execute(args, ctx))
    data = json.loads((tmp_path / "notebooks/new.ipynb").read_text(encoding="utf-8"))

    assert not first.is_error
    assert second.is_error
    assert "拒绝覆盖" in second.content
    assert data["nbformat"] == 4
    assert data["nbformat_minor"] == 5
    assert data["cells"][1]["outputs"] == []
    assert data["cells"][1]["execution_count"] is None
    assert data["cells"][0]["source"] == ["# New"]


def test_notebook_rejects_malformed_json_bad_nbformat_and_sensitive_paths(tmp_path: Path):
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)
    inspector = NotebookInspectTool(NotebookOptions())
    (tmp_path / "bad.ipynb").write_text("{not-json", encoding="utf-8")
    _write(tmp_path / "v3.ipynb", {**_notebook(), "nbformat": 3})
    _write(tmp_path / ".krelcode/hidden.ipynb", _notebook())

    malformed = asyncio.run(inspector.execute({"path": "bad.ipynb"}, ctx))
    old = asyncio.run(inspector.execute({"path": "v3.ipynb"}, ctx))
    hidden = asyncio.run(inspector.execute({"path": ".krelcode/hidden.ipynb"}, ctx))

    assert malformed.is_error
    assert "有效 JSON" in malformed.content
    assert old.is_error
    assert "nbformat 4" in old.content
    assert hidden.is_error
    assert "敏感路径" in hidden.content


def test_notebook_rejects_symlink_escape(tmp_path: Path):
    outside = tmp_path.parent / "outside-notebook.ipynb"
    _write(outside, _notebook())
    link = tmp_path / "link.ipynb"
    link.symlink_to(outside)
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)

    result = asyncio.run(NotebookInspectTool(NotebookOptions()).execute(
        {"path": "link.ipynb"}, ctx))

    assert result.is_error
    assert "越出工作区" in result.content
