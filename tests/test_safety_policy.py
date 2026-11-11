"""SandboxPolicy 测试：分级放行/拒绝、审批流、规则持久化、事件、fail-closed。"""

import asyncio
import json
from pathlib import Path

import pytest

from krelcode.core.events import ApprovalRequested, EventBus
from krelcode.core.messages import ToolCall
from krelcode.safety.policy import ALLOW, AllowAllPolicy, SandboxPolicy
from krelcode.safety.rules import AllowRules, command_program
from krelcode.tools.base import ToolContext


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(workspace_root=tmp_path)


def tc(name="run_command", command="pytest -q", call_id="c1"):
    return ToolCall(call_id, name, {"command": command})


def run(coro):
    return asyncio.run(coro)


class ScriptedApprover:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, tool_call, reason):
        self.calls.append((tool_call, reason))
        return self.answers.pop(0) if self.answers else "no"


# -- 工具分级 --------------------------------------------------------------------

@pytest.mark.parametrize("name", ["get_time", "read_file", "grep", "glob"])
def test_readonly_tools_always_allowed(ctx, name):
    policy = SandboxPolicy(approver=ScriptedApprover())
    d = run(policy.check(ToolCall("c", name, {}), ctx))
    assert d.allowed


@pytest.mark.parametrize("name", ["write_file", "edit_file"])
def test_workspace_write_tools_allowed(ctx, name):
    # 路径约束由 tools/paths.py 兜底，策略层放行
    policy = SandboxPolicy(approver=None)
    d = run(policy.check(ToolCall("c", name, {"path": "a.txt"}), ctx))
    assert d.allowed


def test_unknown_tool_is_asked_and_fail_closed(ctx):
    policy = SandboxPolicy(approver=None)  # 非交互
    d = run(policy.check(ToolCall("c", "mystery_tool", {}), ctx))
    assert not d.allowed and "非交互" in d.reason


# -- run_command 分级 ---------------------------------------------------------------

def test_safe_command_allowed_without_approver(ctx):
    approver = ScriptedApprover()
    policy = SandboxPolicy(approver=approver)
    d = run(policy.check(tc(command="ls -la"), ctx))
    assert d.allowed and approver.calls == []


def test_dangerous_command_denied_without_approver(ctx):
    approver = ScriptedApprover("yes")
    policy = SandboxPolicy(approver=approver)
    d = run(policy.check(tc(command="sudo rm -rf /"), ctx))
    assert not d.allowed and approver.calls == []  # DENY 不走审批，直接拒


def test_ask_yes_no_flow(ctx):
    approver = ScriptedApprover("yes", "no")
    policy = SandboxPolicy(approver=approver)
    assert run(policy.check(tc(command="pytest -q", call_id="a"), ctx)).allowed
    d = run(policy.check(tc(command="pytest -q", call_id="b"), ctx))
    assert not d.allowed and "用户拒绝" in d.reason
    assert len(approver.calls) == 2


def test_ask_non_interactive_fail_closed(ctx):
    policy = SandboxPolicy(approver=None)
    d = run(policy.check(tc(command="pytest -q"), ctx))
    assert not d.allowed and "非交互" in d.reason


def test_auto_approve_bypasses_ask_but_not_deny(ctx):
    approver = ScriptedApprover()
    policy = SandboxPolicy(approver=approver, auto_approve=True)
    d = run(policy.check(tc(command="npm install"), ctx))
    assert d.allowed and "auto-approve" in d.reason and approver.calls == []
    d2 = run(policy.check(tc(command="dd if=/dev/zero of=x"), ctx))
    assert not d2.allowed  # DENY 级不可被 auto-approve 越过


# -- 规则持久化 ---------------------------------------------------------------------

def test_always_persists_rule_and_reloads(ctx, tmp_path):
    rules_path = tmp_path / ".krelcode" / "allow_rules.json"
    approver = ScriptedApprover("always")
    policy = SandboxPolicy(approver=approver, rules_path=rules_path)

    d = run(policy.check(tc(command="pytest tests/"), ctx))
    assert d.allowed and "always" in d.reason
    assert json.loads(rules_path.read_text()) == {"allow_programs": ["pytest"]}

    # 新会话（新 policy 实例，无 approver）加载规则后直接放行
    policy2 = SandboxPolicy(approver=None, rules_path=rules_path)
    d2 = run(policy2.check(tc(command="pytest -x other/"), ctx))
    assert d2.allowed and "allow 规则" in d2.reason


def test_corrupt_rules_file_fail_closed(tmp_path, ctx):
    p = tmp_path / "rules.json"
    p.write_text("{broken json", encoding="utf-8")
    rules = AllowRules(p)
    assert rules.programs == set()


def test_command_program_extraction():
    assert command_program("pytest -q tests/") == "pytest"
    assert command_program("/usr/local/bin/node x.js") == "node"
    assert command_program("") is None


# -- 事件 ----------------------------------------------------------------------------

def test_approval_requested_event_emitted(ctx):
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    policy = SandboxPolicy(approver=ScriptedApprover("yes"), bus=bus)
    run(policy.check(tc(command="pytest"), ctx))
    requested = [e for e in events if isinstance(e, ApprovalRequested)]
    assert len(requested) == 1 and requested[0].tool_call.name == "run_command"


def test_allow_all_policy_still_available(ctx):
    d = run(AllowAllPolicy().check(tc(command="rm -rf /"), ctx))
    assert d.allowed  # --no-sandbox 的语义：全放行（用户自担风险）
