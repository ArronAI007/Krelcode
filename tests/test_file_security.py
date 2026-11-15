"""文件工具安全边界：硬链接、原子写、只读保护和并发修改。"""

import asyncio
import os

import pytest

from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.edit_file import EditFileTool
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.tools.paths import (
    FileChangedError,
    atomic_write_text,
    read_text_file_with_snapshot,
)


def run(coro):
    return asyncio.run(coro)


def test_hardlinked_file_cannot_be_used_to_read_outside_inode(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    linked = workspace / "ordinary.txt"
    os.link(outside, linked)

    result = run(ReadFileTool().execute(
        {"path": "ordinary.txt"}, ToolContext(workspace_root=workspace)))

    assert result.is_error
    assert "硬链接" in result.content
    assert outside.read_text(encoding="utf-8") == "outside-secret"


def test_hardlinked_file_cannot_be_overwritten_through_workspace(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    os.link(outside, workspace / "ordinary.txt")
    ctx = ToolContext(workspace_root=workspace)

    result = run(WriteFileTool().execute(
        {"path": "ordinary.txt", "content": "overwritten"}, ctx))

    assert result.is_error and "硬链接" in result.content
    assert outside.read_text(encoding="utf-8") == "outside-secret"


def test_edit_file_preserves_mode_and_replaces_atomically(tmp_path):
    target = tmp_path / "script.sh"
    target.write_text("echo old\n", encoding="utf-8")
    target.chmod(0o640)

    result = run(EditFileTool().execute(
        {
            "path": "script.sh",
            "old_string": "echo old",
            "new_string": "echo new",
        },
        ToolContext(workspace_root=tmp_path),
    ))

    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "echo new\n"
    assert target.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob(".script.sh.*.tmp"))


def test_atomic_write_rejects_stale_snapshot_and_keeps_new_content(tmp_path):
    target = tmp_path / "config.txt"
    target.write_text("version=1\n", encoding="utf-8")
    _, snapshot = read_text_file_with_snapshot(target)
    target.write_text("version=2\n", encoding="utf-8")

    with pytest.raises(FileChangedError, match="发生变化"):
        atomic_write_text(target, "version=3\n", expected=snapshot)
    assert target.read_text(encoding="utf-8") == "version=2\n"


def test_write_file_refuses_to_replace_read_only_file(tmp_path):
    target = tmp_path / "protected.txt"
    target.write_text("keep\n", encoding="utf-8")
    target.chmod(0o444)

    result = run(WriteFileTool().execute(
        {"path": "protected.txt", "content": "replace\n"},
        ToolContext(workspace_root=tmp_path),
    ))

    assert result.is_error and "只读" in result.content
    assert target.read_text(encoding="utf-8") == "keep\n"
