"""Input routing and safe execution for ``!`` and ``#`` shortcuts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import ToolCallCompleted
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy, SandboxPolicy
from krelcode.session.input import InputEnvelope, InputKind, InputProcessor
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.run_command import RunCommandTool
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.repl import run_repl


def make_loop(tmp_path, *, policy=None) -> AgentLoop:
    return AgentLoop(
        llm=MockProvider(fallback="should-not-run"),
        registry=ToolRegistry([RunCommandTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=policy or AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_input_processor_classifies_shell_and_memory_shortcuts(tmp_path):
    processor = InputProcessor(tmp_path)

    shell = asyncio.run(processor.process(InputEnvelope("!pytest -q")))
    memory = asyncio.run(processor.process(InputEnvelope("#使用 uv 运行测试")))
    empty_shell = asyncio.run(processor.process(InputEnvelope("!")))

    assert shell.kind is InputKind.SHELL
    assert shell.command == "pytest -q" and shell.notes == ()
    assert memory.kind is InputKind.MEMORY and memory.text == "使用 uv 运行测试"
    assert empty_shell.kind is InputKind.SHELL and empty_shell.text == ""


def test_direct_tool_uses_same_policy_and_never_appends_history(tmp_path):
    approvals = []

    async def deny(tool_call, reason):
        approvals.append((tool_call, reason))
        return "no"

    loop = make_loop(
        tmp_path,
        policy=SandboxPolicy(
            approver=deny,
            workspace_root=tmp_path,
            rules_path=tmp_path / ".krelcode" / "allow_rules.json",
        ),
    )
    completed = []
    loop.bus.subscribe(completed.append)

    result = asyncio.run(loop.execute_direct_tool(
        "run_command", {"command": "touch should-not-exist"}))

    assert result.is_error and "安全策略拒绝" in result.content
    assert approvals
    assert not (tmp_path / "should-not-exist").exists()
    assert [message.role for message in loop.history] == ["system"]
    assert any(isinstance(event, ToolCallCompleted) for event in completed)


def test_direct_tool_executes_without_model_history_and_can_be_rendered(tmp_path):
    loop = make_loop(tmp_path)
    completed = []
    loop.bus.subscribe(completed.append)

    result = asyncio.run(loop.execute_direct_tool(
        "run_command", {"command": "printf shell-ok"}))

    assert not result.is_error and "shell-ok" in result.content
    assert [message.role for message in loop.history] == ["system"]
    event = next(item for item in completed if isinstance(item, ToolCallCompleted))
    assert "shell-ok" in event.content


def test_repl_shell_shortcut_runs_without_llm_and_can_exit_cleanly(tmp_path, capsys):
    loop = make_loop(tmp_path)
    completed = []
    loop.bus.subscribe(completed.append)
    calls = 0

    async def inputs(_prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "!printf shell-shortcut"
        if calls == 2:
            await asyncio.sleep(1.0)
            return "/exit"
        raise EOFError

    assert asyncio.run(run_repl(
        loop, input_fn=inputs, persist_history=False)) == 0
    event = next(item for item in completed if isinstance(item, ToolCallCompleted))
    assert "shell-shortcut" in event.content
    assert [message.role for message in loop.history] == ["system"]
    assert "输出已折叠" in capsys.readouterr().out


def test_repl_memory_shortcut_persists_and_shows_panel(tmp_path, capsys):
    loop = make_loop(tmp_path)
    calls = 0

    async def inputs(_prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "#项目测试使用 pytest -q"
        if calls == 2:
            return "#"
        if calls == 3:
            return "/exit"
        raise EOFError

    assert asyncio.run(run_repl(
        loop, input_fn=inputs, persist_history=False)) == 0
    path = tmp_path / ".krelcode" / "memory" / "entries.jsonl"
    payload = json.loads(path.read_text(encoding="utf-8").strip())
    assert payload["text"] == "项目测试使用 pytest -q"
    out = capsys.readouterr().out
    assert "已保存项目记忆" in out
    assert "项目记忆（最近 1 条）" in out
    # Memory notes are context, never user/assistant history.
    assert [message.role for message in loop.history] == ["system"]


def test_exit_during_shell_approval_cancels_direct_task(tmp_path):
    from krelcode.ui.approval import ApprovalBroker

    broker = ApprovalBroker()
    loop = make_loop(
        tmp_path,
        policy=SandboxPolicy(
            approver=broker.request,
            workspace_root=tmp_path,
            rules_path=tmp_path / ".krelcode" / "allow_rules.json",
        ),
    )
    calls = 0

    async def inputs(_prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "!touch should-not-exist"
        if calls == 2:
            for _ in range(100):
                if broker.pending is not None:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.02)
            return "/exit"
        raise EOFError

    try:
        assert asyncio.run(run_repl(
            loop,
            input_fn=inputs,
            persist_history=False,
            approval_broker=broker,
        )) == 0
    finally:
        broker.close()
    assert not (tmp_path / "should-not-exist").exists()
    assert [message.role for message in loop.history] == ["system"]
