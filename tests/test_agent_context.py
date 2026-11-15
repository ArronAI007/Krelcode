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


def test_render_tree_does_not_follow_symlinks_outside_workspace(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET", encoding="utf-8")
    try:
        (ws / "escape").symlink_to(outside, target_is_directory=True)
        (ws / "secret-link.txt").symlink_to(outside / "secret.txt")
    except OSError:
        pytest.skip("当前文件系统不支持 symlink")
    tree = render_tree(ws)
    assert "escape" not in tree
    assert "secret-link.txt" not in tree
    assert "SECRET" not in tree


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


def test_read_agents_md_skips_project_symlink_outside_workspace(ws, empty_home):
    outside = ws.parent / "outside-agents.md"
    outside.write_text("LEAKED_RULE", encoding="utf-8")
    try:
        (ws / "AGENTS.md").symlink_to(outside)
    except OSError:
        pytest.skip("当前文件系统不支持 symlink")
    assert "LEAKED_RULE" not in read_agents_md(ws, home=empty_home)


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


# -- Context v2：分层 bundle、token 预算与可观测摘要 ---------------------------------

def test_context_options_from_config_and_clamping():
    from krelcode.agent.context import ContextOptions

    opts = ContextOptions.from_config({
        "max_tokens": 999999,
        "tree_max_depth": 99,
        "tree_max_entries": 1,
        "rules_max_chars": 10,
        "include_git": False,
    })
    assert opts.max_tokens == 100000
    assert opts.tree_max_depth == 12
    assert opts.tree_max_entries == 10
    assert opts.rules_max_chars == 256
    assert opts.include_git is False


def test_workspace_context_bundle_exposes_sections_and_summary(ws, tmp_path):
    from krelcode.agent.context import build_workspace_context_bundle

    (ws / "AGENTS.md").write_text("项目规则", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    bundle = asyncio.run(build_workspace_context_bundle(ws, home=home))
    assert [section.name for section in bundle.sections] == ["rules", "tree"]
    assert bundle.text.startswith("<workspace_context>")
    assert bundle.total_tokens > 0
    summary = "\n".join(bundle.summary_lines())
    assert "rules:" in summary and "tree:" in summary
    assert "预算" in summary


def test_context_budget_prioritizes_rules_and_omits_low_priority_tree(ws, tmp_path):
    from krelcode.agent.context import (
        ContextOptions, build_workspace_context_bundle,
    )

    (ws / "AGENTS.md").write_text("R" * 8000, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    bundle = asyncio.run(build_workspace_context_bundle(
        ws,
        home=home,
        options=ContextOptions(max_tokens=400),
    ))
    assert [section.name for section in bundle.sections] == ["rules"]
    assert "tree" in bundle.omitted
    assert bundle.total_tokens <= bundle.max_tokens
    assert "上下文已按 token 预算截断" in bundle.text


def test_context_extra_sections_can_have_highest_priority(ws, tmp_path):
    from krelcode.agent.context import (
        ContextOptions, ContextSection, build_workspace_context_bundle,
    )

    home = tmp_path / "home"
    home.mkdir()
    bundle = asyncio.run(build_workspace_context_bundle(
        ws,
        home=home,
        options=ContextOptions(max_tokens=300),
        extra_sections=(ContextSection(
            "session", "SESSION_HOOK_CONTEXT " * 100, priority=0),),
    ))
    assert bundle.sections[0].name == "session"
    assert "SESSION_HOOK_CONTEXT" in bundle.text


def test_context_budget_accounts_for_section_separators(ws, tmp_path):
    from krelcode.agent.context import (
        ContextOptions, ContextSection, build_workspace_context_bundle,
    )

    home = tmp_path / "home"
    home.mkdir()
    sections = tuple(
        ContextSection(f"s{i}", "x" * 400, priority=i)
        for i in range(5)
    )
    bundle = asyncio.run(build_workspace_context_bundle(
        ws,
        home=home,
        options=ContextOptions(
            max_tokens=280,
            include_rules=False,
            include_git=False,
            include_tree=False,
        ),
        extra_sections=sections,
    ))
    assert len(bundle.sections) > 1
    assert bundle.total_tokens <= bundle.max_tokens


def test_runtime_hook_context_is_injected_without_polluting_history(tmp_path):
    from krelcode.agent.loop import AgentLoop
    from krelcode.agent.prompts import build_system_prompt
    from krelcode.llm.providers.mock import MockProvider
    from krelcode.safety.policy import AllowAllPolicy
    from krelcode.tools.base import ToolContext
    from krelcode.tools.registry import ToolRegistry

    provider = MockProvider(scripted=["ok"])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt=build_system_prompt(),
    )
    loop.add_runtime_context(["SESSION_RULE"], source="session_start")
    asyncio.run(loop.run("hello"))
    sent = provider.calls[0]
    assert "<runtime_context>\nSESSION_RULE\n</runtime_context>" in sent[0].content
    assert all("SESSION_RULE" not in (m.content or "") for m in loop.history)


def test_build_loop_applies_context_config_and_retains_bundle(tmp_path):
    from krelcode.app import build_loop, build_parser

    (tmp_path / "visible.py").write_text("print(1)", encoding="utf-8")
    args = build_parser().parse_args(
        ["--mock", "--workspace", str(tmp_path), "--no-compress"])
    config = {"context": {
        "max_tokens": 512,
        "include_rules": False,
        "include_git": False,
        "include_tree": True,
        "tree_max_depth": 1,
        "tree_max_entries": 10,
    }}

    loop, manager = asyncio.run(build_loop(args, config))
    try:
        assert loop.context_options.max_tokens == 512
        assert loop.context_options.include_rules is False
        assert loop.workspace_context is not None
        assert [section.name for section in loop.workspace_context.sections] == [
            "tree",
        ]
        assert "visible.py" in loop.history[0].content
        assert loop.workspace_context.total_tokens <= 512
    finally:
        asyncio.run(manager.shutdown())
