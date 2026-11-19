"""P0: explicit managed/user/project/directory rule fragments."""

from __future__ import annotations

import asyncio
from pathlib import Path

from krelcode.agent.context import ContextOptions, build_workspace_context_bundle
from krelcode.agent.context_inspection import ContextInspectionService
from krelcode.agent.fragments import (
    LAYER_L0_MANAGED_POLICY,
    LAYER_L2_USER_GLOBAL,
    LAYER_L3_PROJECT_RULES,
    PromptFragmentRegistry,
    apply_prompt_snapshot,
    base_fragment,
    workspace_rule_fragments,
)
from krelcode.agent.loop import AgentLoop
from krelcode.agent.rules import RuleLoader, RuleScope
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands.builtin import builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers.runtime_ops import handle_context
from krelcode.session.commands.types import CommandRequest
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def _rule_workspace(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    workspace = tmp_path / "repo"
    (home / ".krelcode" / "managed").mkdir(parents=True)
    (workspace / "src").mkdir(parents=True)
    (home / ".krelcode" / "managed" / "AGENTS.md").write_text(
        "MANAGED_RULE", encoding="utf-8")
    (home / ".krelcode" / "AGENTS.md").write_text(
        "USER_RULE", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("PROJECT_RULE", encoding="utf-8")
    (workspace / "src" / "AGENTS.md").write_text(
        "DIRECTORY_RULE", encoding="utf-8")
    (workspace / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    return home, workspace


def test_rule_stack_keeps_each_authority_separate(tmp_path):
    home, workspace = _rule_workspace(tmp_path)

    stack = RuleLoader(workspace, home=home).load_stack(
        [workspace / "src" / "main.py"])

    assert [layer.scope for layer in stack.layers] == [
        RuleScope.MANAGED,
        RuleScope.USER,
        RuleScope.PROJECT,
        RuleScope.DIRECTORY,
    ]
    assert [layer.bundle.entry_count for layer in stack.layers] == [1, 1, 1, 1]


def test_workspace_rules_emit_canonical_l0_l2_l3_fragments(tmp_path):
    home, workspace = _rule_workspace(tmp_path)
    bundle = asyncio.run(build_workspace_context_bundle(
        workspace,
        home=home,
        options=ContextOptions(include_git=False, include_tree=False),
    ))

    fragments = {item.id: item for item in workspace_rule_fragments(bundle)}

    assert set(fragments) == {"managed:rules", "user:rules", "project:rules"}
    assert "workspace:rules" not in fragments
    assert fragments["managed:rules"].layer == LAYER_L0_MANAGED_POLICY
    assert fragments["user:rules"].layer == LAYER_L2_USER_GLOBAL
    assert fragments["project:rules"].layer == LAYER_L3_PROJECT_RULES
    assert fragments["managed:rules"].metadata["rule_precedence"] == 0
    assert fragments["user:rules"].metadata["rule_precedence"] == 10
    assert fragments["project:rules"].metadata["rule_precedence"] == 20
    assert fragments["project:rules"].metadata["rule_overrides"] == (
        "user:rules",)
    assert fragments["managed:rules"].metadata["rule_conflict_policy"]


def test_context_why_reports_effective_rule_provenance(tmp_path):
    home, workspace = _rule_workspace(tmp_path)
    bundle = asyncio.run(build_workspace_context_bundle(
        workspace,
        home=home,
        options=ContextOptions(include_git=False, include_tree=False),
    ))
    registry = PromptFragmentRegistry([
        base_fragment(),
        *workspace_rule_fragments(bundle),
    ])
    loop = AgentLoop(
        llm=MockProvider(scripted=["ok"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=workspace),
        policy=AllowAllPolicy(),
        context_assembler=None,
    )
    loop.prompt_registry = registry
    loop.workspace_context = bundle
    apply_prompt_snapshot(loop, registry.snapshot())

    assert asyncio.run(loop.run("explain the project")) == "ok"

    why = "\n".join(
        ContextInspectionService(loop).why_lines("project:rules"))
    assert "effective rule source: project:rules" in why
    assert "scope=project" in why
    assert "precedence=20" in why
    assert "overrides=user:rules" in why
    assert "rule files: AGENTS.md" in why
    assert "conflict policy=" in why


def test_build_loop_registers_explicit_rule_fragments(monkeypatch, tmp_path):
    from krelcode.app import build_loop, build_parser

    home, workspace = _rule_workspace(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    args = build_parser().parse_args([
        "--mock", "--workspace", str(workspace), "--no-compress",
        "--no-skills", "--no-plugins", "--no-mcp", "--no-session",
        "--no-checkpoint", "--no-sandbox",
    ])

    loop, manager = asyncio.run(build_loop(args, {
        "context": {"include_git": False, "include_tree": False},
    }))
    try:
        rule_ids = {
            fragment.id for fragment in loop.prompt_registry.list()
            if fragment.metadata.get("rule_scope")
        }
        assert rule_ids == {"managed:rules", "user:rules", "project:rules"}
        assert "workspace:rules" not in {
            fragment.id for fragment in loop.prompt_registry.list()}
    finally:
        asyncio.run(manager.shutdown())


def test_context_rules_lists_canonical_precedence(tmp_path):
    home, workspace = _rule_workspace(tmp_path)
    loader = RuleLoader(workspace, home=home)
    bundle = asyncio.run(build_workspace_context_bundle(
        workspace,
        home=home,
        options=ContextOptions(include_git=False, include_tree=False),
    ))
    loop = AgentLoop(
        llm=MockProvider(),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=workspace),
        policy=AllowAllPolicy(),
    )
    loop.rule_loader = loader
    loop.workspace_context = bundle
    ctx = CommandContext(
        request=CommandRequest(
            name="context", arguments="rules", raw_text="/context rules"),
        loop=loop,
        command_registry=builtin_command_registry(),
        workspace_root=workspace,
    )

    output = "\n".join(asyncio.run(handle_context(ctx)).output)

    assert "precedence: managed → user → project → directory" in output
    assert "managed:rules" in output
    assert "user:rules" in output
    assert "project:rules" in output
