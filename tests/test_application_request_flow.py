"""ApplicationRequest crosses UI, session, harness, context and observability."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.turn.runtime import AgentRuntime
from krelcode.agent.context.context_source import ContextRequest
from krelcode.agent.query import QueryEngine
from krelcode.contracts.application import (
    ApplicationRequestFactory,
    TaskShape,
    TriggerType,
)
from krelcode.core.events import RunStarted
from krelcode.daemon.server_agents import _daemon_application_request
from krelcode.llm.providers.mock import MockProvider
from krelcode.runtime.execution import LocalTaskRuntime
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.repl_session import SessionPrompt
from krelcode.session.stream_json import parse_input_line
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


class CaptureContextSource:
    name = "application-contract-test"

    def __init__(self) -> None:
        self.requests: list[ContextRequest] = []

    def collect(self, request: ContextRequest) -> tuple[()]:
        self.requests.append(request)
        return ()


def make_loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        llm=MockProvider(scripted=["done"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_application_request_reaches_run_event_and_context(tmp_path: Path) -> None:
    loop = make_loop(tmp_path)
    capture = CaptureContextSource()
    loop.context_assembler.register(capture, replace=True)
    events = []
    loop.bus.subscribe(events.append)
    request = ApplicationRequestFactory.from_input_source(
        "repl",
        goal="explain parser",
        task_shape=TaskShape.SINGLE_AGENT,
        metadata={"delivery": "steer"},
    )

    async def scenario() -> str:
        session = QueryEngine(loop).start(
            "explain parser",
            application_request=request,
        )
        async for _ in session.events:
            pass
        return await session.wait()

    assert asyncio.run(scenario()) == "done"
    started = next(event for event in events if isinstance(event, RunStarted))
    assert started.application == request.to_dict()
    assert capture.requests[-1].application_request == request
    assert loop.application_request == request


def test_strict_agent_runtime_rejects_implicit_application_request(
    tmp_path: Path,
) -> None:
    runtime = AgentRuntime(
        make_loop(tmp_path),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
    )

    with pytest.raises(ValueError, match="ApplicationRequest"):
        asyncio.run(runtime.run_once("run tests"))


def test_session_prompt_builds_repl_contract_by_default() -> None:
    prompt = SessionPrompt.create("run tests", source="terminal")

    assert prompt.application_request is not None
    assert prompt.application_request.trigger is TriggerType.REPL
    assert prompt.application_request.goal == "run tests"


def test_stream_json_parses_explicit_application_contract() -> None:
    parsed = parse_input_line(json.dumps({
        "type": "user",
        "content": "delegate",
        "request_id": "r1",
        "application": {
            "trigger": "stream-json",
            "goal": "delegate",
            "task_shape": "multi-agent",
            "acceptance": {"criteria": ["report returned"]},
            "risk": {"level": "medium"},
            "budget": {"max_steps": 4},
            "metadata": {"request_id": "r1"},
        },
    }))

    assert parsed.application is not None
    assert parsed.application.trigger is TriggerType.STREAM_JSON
    assert parsed.application.task_shape is TaskShape.MULTI_AGENT
    assert parsed.application.budget.max_steps == 4


def test_daemon_builds_one_normalized_application_contract() -> None:
    request = _daemon_application_request(
        {
            "task": "delegate",
            "application": {
                "trigger": "sdk",
                "goal": "stale goal",
                "task_shape": "multi-agent",
                "acceptance": {"criteria": ["report returned"]},
                "risk": {"level": "high"},
                "budget": {"timeout_seconds": 30},
                "metadata": {"rpc_id": "r1"},
            },
        },
        goal="delegate",
    )

    assert request.trigger is TriggerType.DAEMON
    assert request.goal == "delegate"
    assert request.task_shape is TaskShape.MULTI_AGENT
    assert request.metadata["rpc_id"] == "r1"
