"""会话运行策略：headless 与交互模式共用 AgentRuntime。"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from pathlib import Path
from typing import get_type_hints

from krelcode.agent.loop import AgentLoop
from krelcode.agent.runtime import AgentRuntime
from krelcode.agent.usage import UsageTracker
from krelcode.core.redact import SecretRedactor
from krelcode.core.events import RunStarted
from krelcode.contracts.application import TriggerType
from krelcode.hooks import HookRunner, HookSpec
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.input import InputProcessor
from krelcode.session.runner import run_session
from krelcode.session.runtime_ports import SessionRuntimePorts
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def test_run_session_uses_explicit_session_runtime_ports():
    signature = inspect.signature(run_session)
    annotations = get_type_hints(run_session)

    assert signature.parameters["ports"].annotation == "SessionRuntimePorts"
    assert annotations["ports"] is SessionRuntimePorts
    assert "supervisor" not in signature.parameters
    assert "checkpoints" not in signature.parameters
    assert "approval_broker" not in signature.parameters
    assert "user_input_broker" not in signature.parameters
    assert "hooks" not in signature.parameters


def make_runtime(tmp_path: Path) -> AgentRuntime:
    loop = AgentLoop(
        llm=MockProvider(scripted=["headless reply"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    loop.skill_manager = None
    return AgentRuntime(loop)


def base_args(**overrides):
    values = {
        "message": None,
        "output_format": "text",
        "resume": None,
        "no_session": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_headless_mode_runs_through_runtime(tmp_path, capsys):
    runtime = make_runtime(tmp_path)
    usage = UsageTracker()
    events = []
    runtime.loop.bus.subscribe(events.append)
    rc = asyncio.run(run_session(
        base_args(message="hello"),
        runtime,
        SessionRuntimePorts(),
        usage=usage,
        model_switch=None,
        skills_reload=None,
        redactor=SecretRedactor(),
    ))
    assert rc == 0
    assert "headless reply" in capsys.readouterr().out
    assert runtime.loop.history[1].content == "hello"
    started = next(event for event in events if isinstance(event, RunStarted))
    assert started.application["trigger"] == TriggerType.HEADLESS.value
    assert started.application["goal"] == "hello"


def test_headless_input_processor_applies_user_input_hook(tmp_path, capsys):
    runtime = make_runtime(tmp_path)
    hooks = HookRunner(tmp_path, [
        HookSpec("user_input", "printf 'headless-context'"),
    ])
    processor = InputProcessor(
        tmp_path, hook_context_provider=hooks.collect_input_context)

    rc = asyncio.run(run_session(
        base_args(message="hello"),
        runtime,
        SessionRuntimePorts(),
        usage=UsageTracker(),
        model_switch=None,
        skills_reload=None,
        redactor=SecretRedactor(),
        input_processor=processor,
    ))

    assert rc == 0
    user_message = next(m for m in runtime.loop.history if m.role == "user")
    assert "hello" in user_message.content
    assert "<hook_context>\nheadless-context\n</hook_context>" in user_message.content


def test_interactive_mode_delegates_without_importing_terminal_ui(tmp_path):
    runtime = make_runtime(tmp_path)
    received = {}
    processor = object()

    async def fake_repl(loop, **kwargs):
        received["loop"] = loop
        received.update(kwargs)
        return 7

    rc = asyncio.run(run_session(
        base_args(),
        runtime,
        SessionRuntimePorts(),
        usage=UsageTracker(),
        model_switch=None,
        skills_reload=None,
        redactor=SecretRedactor(),
        input_processor=processor,
        repl_runner=fake_repl,
    ))
    assert rc == 7
    assert received["loop"] is runtime.loop
    assert received["runtime"] is runtime
    assert received["input_processor"] is processor
    assert received["tui_mouse"] is False
