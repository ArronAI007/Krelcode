"""会话运行策略：headless 与交互模式共用 AgentRuntime。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.agent.runtime import AgentRuntime
from krelcode.agent.usage import UsageTracker
from krelcode.core.redact import SecretRedactor
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.runner import run_session
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


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
    rc = asyncio.run(run_session(
        base_args(message="hello"),
        runtime,
        supervisor=None,
        checkpoints=None,
        usage=usage,
        model_switch=None,
        skills_reload=None,
        redactor=SecretRedactor(),
    ))
    assert rc == 0
    assert "headless reply" in capsys.readouterr().out
    assert runtime.loop.history[1].content == "hello"


def test_interactive_mode_delegates_without_importing_terminal_ui(tmp_path):
    runtime = make_runtime(tmp_path)
    received = {}

    async def fake_repl(loop, **kwargs):
        received["loop"] = loop
        received.update(kwargs)
        return 7

    rc = asyncio.run(run_session(
        base_args(),
        runtime,
        supervisor=None,
        checkpoints=None,
        usage=UsageTracker(),
        model_switch=None,
        skills_reload=None,
        redactor=SecretRedactor(),
        repl_runner=fake_repl,
    ))
    assert rc == 7
    assert received["loop"] is runtime.loop
    assert received["runtime"] is runtime
