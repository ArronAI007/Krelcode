"""Explicit Loop phases, observations, waiting and verification retries."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.turn.loop_state import (
    AgentLoopStateMachine,
    LoopTransitionError,
)
from krelcode.agent.turn.runtime import AgentRuntime
from krelcode.application.acceptance import DefaultAcceptanceEvaluator
from krelcode.contracts.acceptance import AcceptanceStatus
from krelcode.contracts.agent import AgentContractFactory
from krelcode.contracts.application import (
    AcceptanceContract,
    ApplicationRequestFactory,
    RiskLevel,
    RiskProfile,
)
from krelcode.contracts.loop import LoopPhase
from krelcode.contracts.runtime import RuntimeTaskStatus
from krelcode.core.events import (
    AcceptanceEvaluated,
    LoopPhaseChanged,
    ObservationRecorded,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.governance.control_plane import DefaultControlPlane
from krelcode.llm.providers.mock import MockProvider
from krelcode.runtime.execution import LocalTaskRuntime
from krelcode.safety.policy import ToolPermissionPolicy
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


class WriteTool:
    name = "write_file"
    description = "write"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    async def execute(self, args, ctx):
        path = Path(ctx.workspace_root) / str(args["path"])
        path.write_text(str(args["content"]), encoding="utf-8")
        return ToolResult(content=f"wrote {args['path']}")


def make_loop(tmp_path: Path, scripted, policy=None) -> AgentLoop:
    return AgentLoop(
        llm=MockProvider(scripted=scripted),
        registry=ToolRegistry([WriteTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=policy,
        system_prompt="SYS",
    )


def test_state_machine_rejects_invalid_transition() -> None:
    async def go() -> None:
        state = AgentLoopStateMachine("turn-1", _noop_emit)
        with pytest.raises(LoopTransitionError):
            await state.transition(LoopPhase.COMPLETED, "skip verify")

    asyncio.run(go())


def test_tool_loop_emits_all_five_phases_and_observation(
    tmp_path: Path,
) -> None:
    loop = make_loop(tmp_path, [
        Message.assistant(tool_calls=[
            ToolCall("w1", "write_file", {
                "path": "a.txt",
                "content": "x",
            }),
        ]),
        "done",
    ])
    events = []
    loop.bus.subscribe(events.append)

    assert asyncio.run(loop.run("write")) == "done"

    phases = [
        event.phase
        for event in events
        if isinstance(event, LoopPhaseChanged)
    ]
    assert phases[:5] == ["model", "act", "observe", "prepare", "model"]
    assert phases[-2:] == ["verify", "completed"]
    observations = [
        event.observation
        for event in events
        if isinstance(event, ObservationRecorded)
    ]
    assert any(
        item["source"] == "tool" and item["kind"] == "tool:write_file"
        for item in observations
    )


def test_user_steering_is_normalized_as_observation(
    tmp_path: Path,
) -> None:
    loop = make_loop(tmp_path, ["done"])
    loop.steering_queue.put_nowait("also check the tests")
    observations = []
    loop.bus.subscribe(
        lambda event: observations.append(event.observation)
        if isinstance(event, ObservationRecorded) else None)

    assert asyncio.run(loop.run("continue")) == "done"

    assert any(
        item["source"] == "user"
        and item["kind"] == "user.steering"
        for item in observations
    )


def test_verification_failure_returns_to_prepare_until_budget(
    tmp_path: Path,
) -> None:
    loop = make_loop(tmp_path, ["attempt-1", "attempt-2"])
    request = ApplicationRequestFactory.from_input_source(
        "sdk",
        goal="fix tests",
        acceptance=AcceptanceContract(
            criteria=("tests pass",),
            required_evidence=("tests",),
        ),
    )
    contract = AgentContractFactory.from_application(
        request,
        max_verification_attempts=2,
    )
    runtime = AgentRuntime(
        loop,
        acceptance_evaluator=DefaultAcceptanceEvaluator(),
        control_plane=DefaultControlPlane(),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
    )

    with pytest.raises(Exception, match="验收未通过"):
        asyncio.run(runtime.run_once(
            "fix tests", agent_contract=contract))

    assert loop.loop_state is not None
    transitions = [
        (item.previous_phase, item.phase)
        for item in loop.loop_state.transitions
    ]
    assert (LoopPhase.VERIFY, LoopPhase.PREPARE) in transitions
    assert loop.loop_state.verification_attempts == 2
    assert loop.acceptance_result is not None
    assert loop.acceptance_result.status is AcceptanceStatus.FAILED
    assert any(
        message.role == "user"
        and "[Verification Gap]" in (message.content or "")
        for message in loop.history
    )


def test_waiting_is_a_first_class_runtime_state(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def approver(_call, _reason):
        await release.wait()
        return "yes"

    policy = ToolPermissionPolicy(
        approver=approver,
        workspace_root=tmp_path,
    )
    loop = make_loop(tmp_path, [
        Message.assistant(tool_calls=[
            ToolCall("w1", "write_file", {
                "path": "approved.txt",
                "content": "x",
            }),
        ]),
        "approved",
    ], policy=policy)
    contract = AgentContractFactory.from_application(
        ApplicationRequestFactory.from_input_source(
            "sdk",
            goal="write after approval",
            risk=RiskProfile(level=RiskLevel.HIGH),
        ),
    )
    runtime = AgentRuntime(
        loop,
        acceptance_evaluator=DefaultAcceptanceEvaluator(),
        control_plane=DefaultControlPlane(),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
    )

    async def scenario() -> None:
        query = runtime.start_query(
            "write after approval",
            agent_contract=contract,
        )
        waiting = asyncio.Event()
        events = []

        async def consume() -> None:
            async for item in query.events:
                events.append(item.event)
                if (
                    isinstance(item.event, LoopPhaseChanged)
                    and item.event.phase == "waiting"
                ):
                    waiting.set()

        consumer = asyncio.create_task(consume())
        await asyncio.wait_for(waiting.wait(), timeout=2)
        waiting_state = runtime.inspect_task(query.task_id)
        assert waiting_state is not None
        assert waiting_state.status is RuntimeTaskStatus.WAITING
        assert waiting_state.wait_reason
        release.set()
        await consumer
        assert await query.wait() == "approved"
        completed = runtime.inspect_task(query.task_id)
        assert completed is not None
        assert completed.status is RuntimeTaskStatus.COMPLETED
        phases = [
            event.phase
            for event in events
            if isinstance(event, LoopPhaseChanged)
        ]
        waiting_index = phases.index("waiting")
        assert phases[waiting_index + 1] == "act"

    asyncio.run(scenario())


def test_acceptance_events_include_contract_verification(
    tmp_path: Path,
) -> None:
    loop = make_loop(tmp_path, [
        Message.assistant(tool_calls=[
            ToolCall("w1", "write_file", {
                "path": "deliverable.txt",
                "content": "ok",
            }),
        ]),
        "done",
    ])
    request = ApplicationRequestFactory.from_input_source(
        "sdk", goal="deliver")
    contract = AgentContractFactory.from_application(request)
    events = []
    loop.bus.subscribe(events.append)

    asyncio.run(AgentRuntime(
        loop,
        acceptance_evaluator=DefaultAcceptanceEvaluator(),
        control_plane=DefaultControlPlane(),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
    ).run_once("deliver", agent_contract=contract))

    acceptance = [
        event for event in events if isinstance(event, AcceptanceEvaluated)
    ]
    assert acceptance
    assert acceptance[-1].acceptance["status"] == "passed"
    assert acceptance[-1].acceptance["metadata"]["contract_id"] == (
        contract.contract_id)


async def _noop_emit(_event) -> None:
    return None
