"""运行时权限：模式同步、session/workspace 路径规则与命令管理面。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.core.messages import ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import SandboxPolicy
from krelcode.safety.rules import (
    AllowRules,
    RuleDecision,
    workspace_relative_path,
)
from krelcode.safety.runtime import RuntimePermissionManager
from krelcode.session.controller import SessionController
from krelcode.tools.base import (
    PermissionMode,
    RuntimeSnapshot,
    ToolContext,
    ToolDescriptor,
)
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.repl import ReplControl, TerminalLineEditor


def run(coro):
    return asyncio.run(coro)


class ScriptedApprover:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, tool_call, reason):
        self.calls.append((tool_call, reason))
        return self.answers.pop(0) if self.answers else "no"


def fake_loop(tmp_path, mode=PermissionMode.DEFAULT):
    ctx = ToolContext(
        workspace_root=tmp_path,
        runtime=RuntimeSnapshot(permission_mode=mode),
    )
    loop = SimpleNamespace(
        tool_context=ctx,
        _turn_tool_context=ctx,
        _active_tool_pool=None,
        invalidate_tool_pool=lambda: None,
        sync_context_attachments=lambda **_: None,
    )
    return loop


def make_controller(tmp_path):
    loop = AgentLoop(
        llm=MockProvider(fallback="ok"),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=SandboxPolicy(
            rules_path=tmp_path / ".krelcode" / "allow_rules.json",
            workspace_root=tmp_path,
        ),
        system_prompt="SYS",
    )
    return SessionController(loop, workspace_root=tmp_path), loop


# -- 模式 --------------------------------------------------------------------------

def test_runtime_mode_updates_next_turn_and_current_policy_snapshot(tmp_path):
    loop = fake_loop(tmp_path)
    manager = RuntimePermissionManager(loop)

    manager.set_mode(PermissionMode.PLAN)

    assert loop.tool_context.runtime.permission_mode is PermissionMode.PLAN
    assert loop._turn_tool_context.runtime.permission_mode is PermissionMode.PLAN
    assert manager.previous_mode is PermissionMode.DEFAULT


def test_permission_cycle_order_and_restore(tmp_path):
    manager = RuntimePermissionManager(fake_loop(tmp_path))

    assert manager.cycle_mode() is PermissionMode.PLAN
    assert manager.cycle_mode() is PermissionMode.READ_ONLY
    assert manager.cycle_mode() is PermissionMode.AUTO_APPROVE
    assert manager.cycle_mode() is PermissionMode.DEFAULT
    assert manager.cycle_mode(reverse=True) is PermissionMode.AUTO_APPROVE


def test_shift_tab_is_a_dedicated_editor_control():
    assert TerminalLineEditor._apply_escape_sequence(
        b"[Z", []) is ReplControl.CYCLE_PERMISSION


# -- 规则仓库 ----------------------------------------------------------------------

def test_session_path_rule_is_not_persisted(tmp_path):
    path = tmp_path / ".krelcode" / "allow_rules.json"
    rules = AllowRules(path, workspace_root=tmp_path)
    rule, added = rules.add_rule(
        RuleDecision.ALLOW, "session", "path", "docs/**")

    assert added and rule.id == "session-allow-1"
    assert rules.evaluate_path("docs/guide.md").decision is RuleDecision.ALLOW
    reloaded = AllowRules(path, workspace_root=tmp_path)
    assert not reloaded.session_rules
    assert not any(r.id == rule.id for r in reloaded.rules)


def test_workspace_path_rule_persists_and_deny_beats_allow(tmp_path):
    path = tmp_path / ".krelcode" / "allow_rules.json"
    rules = AllowRules(path, workspace_root=tmp_path)
    rules.add_rule("allow", "workspace", "path", "docs/**")
    deny_rule, _ = rules.add_rule(
        "deny", "workspace", "path", "docs/private/**")

    match = rules.evaluate_path("docs/private/secret.md")
    assert match is not None and match.decision is RuleDecision.DENY
    payload = json.loads(path.read_text())
    assert payload["rules"][-1]["id"] == deny_rule.id

    reloaded = AllowRules(path, workspace_root=tmp_path)
    assert reloaded.evaluate_path("docs/public/a.md").decision is RuleDecision.ALLOW
    assert reloaded.evaluate_path("docs/private/a.md").decision is RuleDecision.DENY


def test_path_rule_rejects_absolute_and_parent_traversal(tmp_path):
    rules = AllowRules(tmp_path / "rules.json", workspace_root=tmp_path)
    with pytest.raises(ValueError):
        rules.add_rule("allow", "session", "path", "/etc/**")
    with pytest.raises(ValueError):
        rules.add_rule("allow", "session", "path", "../outside/**")


def test_symlink_outside_workspace_cannot_match_a_path_rule(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    rules = AllowRules(tmp_path / "rules.json", workspace_root=tmp_path)
    rules.add_rule("allow", "workspace", "path", "**")

    assert workspace_relative_path("linked/secret.txt", tmp_path) is None
    assert rules.evaluate_path("linked/secret.txt") is None


def test_legacy_rules_remain_compatible(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({
        "allow_programs": ["pytest"],
        "allow_commands": ["git status --short"],
    }), encoding="utf-8")
    rules = AllowRules(path, workspace_root=tmp_path)

    assert rules.matches("pytest -q")
    assert rules.matches("git   status --short")
    assert not rules.matches("pytest && python3 unsafe.py")


# -- 策略集成 ----------------------------------------------------------------------

def test_path_deny_blocks_read_even_before_readonly_allow(tmp_path):
    ctx = ToolContext(workspace_root=tmp_path)
    policy = SandboxPolicy(workspace_root=tmp_path)
    policy.rules.add_rule("deny", "workspace", "path", "private/**")

    decision = run(policy.check(
        ToolCall("r", "read_file", {"path": "private/secret.txt"}), ctx))

    assert not decision.allowed
    assert "权限规则拒绝" in decision.reason


def test_explicit_ask_rule_is_not_bypassed_by_auto_approve(tmp_path):
    approver = ScriptedApprover("no")
    ctx = ToolContext(
        workspace_root=tmp_path,
        runtime=RuntimeSnapshot(
            permission_mode=PermissionMode.AUTO_APPROVE),
    )
    policy = SandboxPolicy(approver=approver, workspace_root=tmp_path)
    policy.rules.add_rule("ask", "session", "path", "docs/**")

    decision = run(policy.check(
        ToolCall("w", "write_file", {"path": "docs/a.md", "content": "x"}), ctx))

    assert not decision.allowed
    assert len(approver.calls) == 1
    assert "ask" in approver.calls[0][1]


def test_command_deny_rule_overrides_program_allow(tmp_path):
    ctx = ToolContext(workspace_root=tmp_path)
    policy = SandboxPolicy(workspace_root=tmp_path)
    policy.rules.add_rule("allow", "workspace", "command_program", "pytest")
    policy.rules.add_rule("deny", "workspace", "command", "pytest -q")

    allowed = run(policy.check(
        ToolCall("a", "run_command", {"command": "pytest tests"}), ctx))
    denied = run(policy.check(
        ToolCall("b", "run_command", {"command": "pytest -q"}), ctx))

    assert allowed.allowed
    assert not denied.allowed and "权限规则拒绝" in denied.reason


def test_review_mode_still_denies_side_effects_with_allow_rule(tmp_path):
    ctx = ToolContext(
        workspace_root=tmp_path,
        runtime=RuntimeSnapshot(permission_mode=PermissionMode.PLAN),
        tool_descriptor=ToolDescriptor(read_only=False),
    )
    policy = SandboxPolicy(workspace_root=tmp_path)
    policy.rules.add_rule("allow", "session", "path", "**")

    decision = run(policy.check(
        ToolCall("w", "write_file", {"path": "a.txt", "content": "x"}), ctx))

    assert not decision.allowed
    assert "禁止有副作用" in decision.reason


# -- 命令管理面 --------------------------------------------------------------------

def test_permissions_command_switches_mode_and_manages_session_rule(tmp_path):
    controller, loop = make_controller(tmp_path)

    status = run(controller.execute_command("/permissions"))
    switched = run(controller.execute_command("/permissions mode plan"))
    added = run(controller.execute_command(
        '/permissions add allow session path "docs/**"'))
    removed = run(controller.execute_command(
        "/permissions remove session-allow-1"))

    assert status.handled and "当前权限模式" in "\n".join(status.output)
    assert "default → plan" in "\n".join(switched.output)
    assert loop.tool_context.runtime.permission_mode is PermissionMode.PLAN
    assert "session-allow-1" in "\n".join(added.output)
    assert "已移除" in "\n".join(removed.output)
    assert not loop.policy.rules.session_rules


def test_permissions_shorthand_add_form(tmp_path):
    controller, loop = make_controller(tmp_path)

    result = run(controller.execute_command(
        "/permissions deny session tool dangerous_tool"))

    assert result.handled and "已添加" in "\n".join(result.output)
    assert loop.policy.rules.evaluate_tool("dangerous_tool").decision is RuleDecision.DENY


def test_permission_command_persists_workspace_rule(tmp_path):
    controller, loop = make_controller(tmp_path)

    result = run(controller.execute_command(
        "/permissions add deny workspace path .git/**"))

    assert result.handled and "已添加" in "\n".join(result.output)
    reloaded = AllowRules(
        tmp_path / ".krelcode" / "allow_rules.json",
        workspace_root=tmp_path,
    )
    assert reloaded.evaluate_path(".git/config").decision is RuleDecision.DENY
    assert loop.policy.rules.path.is_file()


def test_quick_permission_cycle_reaches_plan_mode(tmp_path, capsys):
    from krelcode.ui.repl import run_repl

    loop = AgentLoop(
        llm=MockProvider(fallback="ok"),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=SandboxPolicy(workspace_root=tmp_path),
        system_prompt="SYS",
    )

    async def inputs(_prompt):
        await asyncio.sleep(0.01)
        if not hasattr(inputs, "done"):
            inputs.done = True
            return ReplControl.CYCLE_PERMISSION
        return "/exit"

    assert asyncio.run(run_repl(loop, input_fn=inputs)) == 0
    assert loop.tool_context.runtime.permission_mode is PermissionMode.PLAN
    assert "权限模式: default → plan" in capsys.readouterr().out
