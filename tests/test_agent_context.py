"""Step 5 上下文收集测试：目录树 / AGENTS.md / git 快照 / 汇总。"""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from krelcode.agent.context import (
    TREE_MAX_ENTRIES, build_workspace_context, git_snapshot, read_agents_md,
    render_tree,
)


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi", encoding="utf-8")
    (tmp_path / ".hidden").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    return tmp_path


def test_render_tree_structure_and_pruning(ws):
    tree = render_tree(ws)
    assert "src/" in tree and "main.py" in tree and "README.md" in tree
    assert ".git" not in tree and "node_modules" not in tree and ".hidden" not in tree


def test_render_tree_depth_limit(ws):
    deep = ws / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "too_deep.txt").write_text("x", encoding="utf-8")
    tree = render_tree(ws)
    assert "c/" in tree            # 深度 3 的目录还可见
    assert "too_deep.txt" not in tree  # 深度 4 的文件被截去


def test_render_tree_entry_cap(ws):
    for i in range(TREE_MAX_ENTRIES + 10):
        (ws / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
    tree = render_tree(ws)
    assert "目录树截断" in tree


@pytest.fixture
def empty_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return home


def test_read_agents_md(ws, empty_home):
    assert read_agents_md(ws, home=empty_home) == ""
    (ws / "AGENTS.md").write_text("永远用中文注释。", encoding="utf-8")
    out = read_agents_md(ws, home=empty_home)
    assert "AGENTS.md" in out and "永远用中文注释" in out


def test_read_agents_md_size_cap(ws, empty_home):
    (ws / "AGENTS.md").write_text("r" * 20000, encoding="utf-8")
    assert len(read_agents_md(ws, home=empty_home)) < 9000


def test_read_agents_md_global_and_project_layering(ws, tmp_path):
    """全局 ~/.krelcode/AGENTS.md + 项目 AGENTS.md 分层注入，全局在前。"""
    home = tmp_path / "home"
    (home / ".krelcode").mkdir(parents=True)
    (home / ".krelcode" / "AGENTS.md").write_text("全局偏好：简洁回复", encoding="utf-8")
    (ws / "AGENTS.md").write_text("项目约定：中文注释", encoding="utf-8")

    out = read_agents_md(ws, home=home)
    assert "全局规则" in out and "全局偏好：简洁回复" in out
    assert "项目规则" in out and "项目约定：中文注释" in out
    assert out.index("全局偏好") < out.index("项目约定")   # 更具体的项目规则在后


def test_read_agents_md_global_only(ws, tmp_path):
    home = tmp_path / "home"
    (home / ".krelcode").mkdir(parents=True)
    (home / ".krelcode" / "AGENTS.md").write_text("只有全局", encoding="utf-8")
    out = read_agents_md(ws, home=home)
    assert "只有全局" in out and "项目规则" not in out


def test_git_snapshot_non_repo(ws):
    assert asyncio.run(git_snapshot(ws)) == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_git_snapshot_real_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "dirty.txt").write_text("uncommitted", encoding="utf-8")
    snap = asyncio.run(git_snapshot(tmp_path))
    assert "git 分支:" in snap
    assert "dirty.txt" in snap  # porcelain 状态包含未跟踪文件


def test_build_workspace_context_wraps_sections(ws, tmp_path):
    (ws / "AGENTS.md").write_text("规则A", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    ctx = asyncio.run(build_workspace_context(ws, home=home))
    assert ctx.startswith("<workspace_context>") and ctx.endswith("</workspace_context>")
    assert "工作区目录树" in ctx and "规则A" in ctx
