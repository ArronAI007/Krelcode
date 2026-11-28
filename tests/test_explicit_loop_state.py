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
from krelcode.contracts.application import TaskBudget
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
        assert waiting_state.wait_kind == "approval"
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


def test_tool_call_budget_denies_second_action_and_allows_final_answer(
    tmp_path: Path,
) -> None:
    calls = 0

    class CountingWriteTool(WriteTool):
        async def execute(self, args, ctx):
            nonlocal calls
            calls += 1
            return await super().execute(args, ctx)

    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[
                ToolCall("w1", "write_file", {
                    "path": "one.txt", "content": "1",
                }),
            ]),
            Message.assistant(tool_calls=[
                ToolCall("w2", "write_file", {
                    "path": "two.txt", "content": "2",
                }),
            ]),
            "unused",
        ]),
        registry=ToolRegistry([CountingWriteTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=ToolPermissionPolicy(
            workspace_root=tmp_path,
            auto_approve=True,
        ),
        system_prompt="SYS",
    )
    application = ApplicationRequestFactory.from_input_source(
        "sdk",
        goal="one write only",
        budget=TaskBudget(
            max_tool_calls=1,
            allow_budget_extension=False,
        ),
    )
    contract = AgentContractFactory.from_application(application)
    runtime = AgentRuntime(
        loop,
        acceptance_evaluator=DefaultAcceptanceEvaluator(),
        control_plane=DefaultControlPlane(),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
    )

    reply = asyncio.run(runtime.run_once(
        "one write only", agent_contract=contract))

    assert reply == "unused"
    assert calls == 1
    assert not (tmp_path / "two.txt").exists()
    assert any(
        message.role == "tool"
        and "工具预算拒绝执行" in (message.content or "")
        for message in loop.history
    )


def test_hard_budget_termination_carries_unfinished_manifest(
    tmp_path: Path,
) -> None:
    loop = AgentLoop(
        llm=MockProvider(
            scripted=[
                Message.assistant(tool_calls=[
                    ToolCall("w1", "write_file", {
                        "path": "partial.txt", "content": "partial",
                    }),
                ]),
                "unused",
            ],
            usage={"input_tokens": 2, "output_tokens": 0},
        ),
        registry=ToolRegistry([WriteTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=ToolPermissionPolicy(
            workspace_root=tmp_path,
            auto_approve=True,
        ),
        system_prompt="SYS",
    )
    application = ApplicationRequestFactory.from_input_source(
        "sdk",
        goal="hard budget",
        budget=TaskBudget(
            max_tokens=1,
            soft_limit_ratio=0.8,
            allow_budget_extension=False,
        ),
    )
    contract = AgentContractFactory.from_application(application)
    runtime = AgentRuntime(
        loop,
        acceptance_evaluator=DefaultAcceptanceEvaluator(),
        control_plane=DefaultControlPlane(),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
    )

    with pytest.raises(Exception) as caught:
        asyncio.run(runtime.run_once(
            "hard budget", agent_contract=contract))

    termination = getattr(caught.value, "termination", None)
    assert termination is not None
    assert termination.reason.value == "budget_exhausted"
    assert termination.unfinished is not None
    assert termination.unfinished.items


def test_high_risk_action_budget_denies_second_approved_action(
    tmp_path: Path,
) -> None:
    async def approver(_call, _reason):
        return "yes"

    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[
                ToolCall("w1", "write_file", {
                    "path": "one.txt", "content": "1",
                }),
            ]),
            Message.assistant(tool_calls=[
                ToolCall("w2", "write_file", {
                    "path": "two.txt", "content": "2",
                }),
            ]),
            "finished",
        ]),
        registry=ToolRegistry([WriteTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=ToolPermissionPolicy(
            approver=approver,
            workspace_root=tmp_path,
        ),
        system_prompt="SYS",
    )
    application = ApplicationRequestFactory.from_input_source(
        "sdk",
        goal="one risky action",
        risk=RiskProfile(level=RiskLevel.HIGH),
        budget=TaskBudget(
            max_high_risk_actions=1,
            allow_budget_extension=False,
        ),
    )
    contract = AgentContractFactory.from_application(application)
    runtime = AgentRuntime(
        loop,
        acceptance_evaluator=DefaultAcceptanceEvaluator(),
        control_plane=DefaultControlPlane(),
        task_runtime=LocalTaskRuntime(),
        strict_application_request=True,
    )

    assert asyncio.run(runtime.run_once(
        "one risky action", agent_contract=contract)) == "finished"
    assert (tmp_path / "one.txt").is_file()
    assert not (tmp_path / "two.txt").exists()
    assert any(
        message.role == "tool"
        and "高风险动作预算拒绝执行" in (message.content or "")
        for message in loop.history
    )


async def _noop_emit(_event) -> None:
    return None
