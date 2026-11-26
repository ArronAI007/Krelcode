"""Runtime acceptance contract evaluation."""

from __future__ import annotations

import asyncio
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.agent.turn.acceptance import DefaultAcceptanceEvaluator
from krelcode.contracts.acceptance import AcceptanceEvidence, AcceptanceStatus
from krelcode.contracts.application import (
    AcceptanceContract,
    ApplicationRequestFactory,
)
from krelcode.core.events import AcceptanceEvaluated
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def test_default_acceptance_passes_direct_model_response() -> None:
    request = ApplicationRequestFactory.from_input_source(
        "repl", goal="answer")
    result = DefaultAcceptanceEvaluator().evaluate(
        request,
        "answer",
        (AcceptanceEvidence(
            kind="tool-result",
            source="read_file",
            passed=True,
        ),),
    )

    assert result.status is AcceptanceStatus.PASSED
    assert result.passed
    assert result.request_goal == "answer"
    assert result.final_response_hash


def test_required_test_evidence_fails_without_test_result() -> None:
    request = ApplicationRequestFactory.from_input_source(
        "headless",
        goal="fix tests",
        acceptance=AcceptanceContract(
            criteria=("tests pass",),
            required_evidence=("tests",),
        ),
    )
    result = DefaultAcceptanceEvaluator().evaluate(
        request,
        "implemented",
        (),
    )

    assert result.status is AcceptanceStatus.FAILED
    assert "缺少验收证据" in result.reason

    passed = DefaultAcceptanceEvaluator().evaluate(
        request,
        "implemented",
        (AcceptanceEvidence(
            kind="test-result",
            source="run_command",
            passed=True,
        ),),
    )
    assert passed.status is AcceptanceStatus.PASSED


def test_agent_loop_emits_acceptance_result(tmp_path: Path) -> None:
    request = ApplicationRequestFactory.from_input_source(
        "headless",
        goal="fix",
        acceptance=AcceptanceContract(
            criteria=("tests pass",),
            required_evidence=("tests",),
        ),
    )
    events = []
    loop = AgentLoop(
        llm=MockProvider(scripted=["implemented"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    loop.bus.subscribe(events.append)

    assert asyncio.run(loop.run(
        "fix", application_request=request)) == "implemented"
    assert loop.acceptance_result is not None
    assert loop.acceptance_result.status is AcceptanceStatus.FAILED
    acceptance = next(
        event for event in events if isinstance(event, AcceptanceEvaluated))
    assert acceptance.acceptance["status"] == "failed"
