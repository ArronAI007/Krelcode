"""Structured plan control, revision, roles and approval contracts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.turn.planning import PlanControlPolicy
from krelcode.contracts.application import (
    ApplicationRequestFactory,
    RiskLevel,
    RiskProfile,
    TaskBudget,
    TaskShape,
)
from krelcode.contracts.plan import (
    PlanApprovalStatus,
    PlanControl,
    PlanControlMode,
    PlanNode,
    PlanNodeStatus,
    PlannerExecutorControl,
    PlanScheduler,
)
from krelcode.contracts.plan_ops import plan_snapshot_views
from krelcode.contracts.runtime import PermissionMode
from krelcode.core.events import PlanUpdated
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.safety.runtime import (
    PlanApprovalCoordinator,
    RuntimePermissionManager,
)
from krelcode.tools.base import ToolContext
from krelcode.tools.extension.update_plan import UpdatePlanTool
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.repl_surface import handle_plan_command
from krelcode.session.state import SessionViewState


def _role_plan() -> PlanControl:
    return PlanControl.from_nodes(
        (
            PlanNode(
                id="plan",
                objective="Plan the change",
                status=PlanNodeStatus.COMPLETED,
                owner="planner",
                acceptance_checks=("scope is explicit",),
                evidence_refs=("observation:1",),
            ),
            PlanNode(
                id="execute",
                objective="Implement the change",
                status=PlanNodeStatus.IN_PROGRESS,
                owner="executor:backend",
                depends_on=("plan",),
            ),
            PlanNode(
                id="verify",
                objective="Verify behavior",
                status=PlanNodeStatus.PENDING,
                owner="verifier",
                depends_on=("execute",),
                acceptance_checks=("focused tests pass",),
            ),
        ),
        mode=PlanControlMode.PLANNER_EXECUTOR,
        trigger_fact="initial plan",
    )


def test_plan_revision_tracks_trigger_and_preserves_completed_nodes():
    current = _role_plan()
    revised = current.revise(
        (
            current.nodes[0],
            current.nodes[1],
            PlanNode(
                id="verify",
                objective="Verify behavior and rollback",
                status=PlanNodeStatus.PENDING,
                owner="verifier",
                depends_on=("execute",),
                acceptance_checks=("focused tests pass", "rollback succeeds"),
            ),
        ),
        trigger_observation_id="obs-4",
        trigger_fact="migration needs a rollback check",
    )

    assert revised.revision.parent_revision_id == current.revision.revision_id
    assert revised.revision.trigger_observation_id == "obs-4"
    assert revised.revision.trigger_fact == "migration needs a rollback check"
    assert revised.revision.preserved_completed_ids == ("plan",)
    assert revised.revision.modified_ids == ("verify",)
    assert revised.revision_history[-1] == current.revision
    assert revised.nodes_for_role("planner") == (revised.nodes[0],)
    assert revised.nodes_for_role("verifier") == (revised.nodes[2],)

    with pytest.raises(ValueError, match="不能删除或回退已完成节点"):
        current.revise(
            current.nodes[1:],
            trigger_fact="tried to drop completed planning",
        )


def test_plan_revision_requires_trigger_fact_and_completion_evidence():
    current = _role_plan()
    changed = PlanNode(
        id="verify",
        objective="Verify with an additional check",
        owner="verifier",
        depends_on=("execute",),
    )
    with pytest.raises(ValueError, match="trigger_fact"):
        current.revise((*current.nodes[:2], changed))

    with pytest.raises(ValueError, match="缺少 evidence_refs"):
        PlanNode(
            id="done",
            objective="Done",
            status="completed",
            acceptance_checks=("test passes",),
        )


def test_planner_executor_requires_roles_and_dependency_safe_scheduling():
    current = _role_plan()
    wrapper = PlannerExecutorControl(current)
    assert wrapper.supports_parallel is True
    assert [node.id for node in wrapper.ready_nodes()] == []

    completed = PlanControl.from_nodes(
        (
            PlanNode(
                id="a",
                objective="Inspect",
                status="completed",
                owner="planner",
                evidence_refs=("read:1",),
            ),
            PlanNode(
                id="b",
                objective="Implement API",
                owner="executor",
                depends_on=("a",),
                parallel_group="implement",
            ),
            PlanNode(
                id="c",
                objective="Implement CLI",
                owner="executor",
                depends_on=("a",),
                parallel_group="implement",
            ),
            PlanNode(
                id="v",
                objective="Verify",
                owner="verifier",
                depends_on=("b", "c"),
            ),
        ),
        mode=PlanControlMode.PLANNER_EXECUTOR,
        trigger_fact="initial plan",
    )
    assert [node.id for node in PlanScheduler().ready_nodes(completed)] == [
        "b", "c",
    ]
    assert PlanScheduler().parallel_groups(completed) == {
        "implement": ("b", "c"),
    }

    with pytest.raises(ValueError, match="缺少角色 owner"):
        PlanControl.from_nodes(
            (
                PlanNode(id="a", objective="Only executor", owner="executor"),
                PlanNode(id="b", objective="More executor", owner="executor"),
                PlanNode(id="c", objective="Third executor", owner="executor"),
            ),
            mode=PlanControlMode.PLANNER_EXECUTOR,
            trigger_fact="invalid roles",
        )


def test_plan_control_policy_selects_mode_from_task_contract():
    policy = PlanControlPolicy()
    base = ApplicationRequestFactory.from_input_source(
        "repl", goal="small task")
    high_risk = ApplicationRequestFactory.from_input_source(
        "repl",
        goal="migration",
        risk=RiskProfile(level=RiskLevel.HIGH),
    )
    long_horizon = ApplicationRequestFactory.from_input_source(
        "repl",
        goal="large refactor",
        task_shape=TaskShape.LONG_HORIZON,
    )
    many_steps = ApplicationRequestFactory.from_input_source(
        "repl",
        goal="many steps",
        budget=TaskBudget(max_steps=24),
    )

    assert policy.select_mode(base) is PlanControlMode.TODO
    assert policy.select_mode(
        high_risk) is PlanControlMode.PLAN_MODE
    assert policy.select_mode(
        long_horizon) is PlanControlMode.PLANNER_EXECUTOR
    assert policy.select_mode(
        many_steps) is PlanControlMode.PLANNER_EXECUTOR
    assert policy.select_mode(
        base,
        permission_mode=PermissionMode.PLAN,
    ) is PlanControlMode.PLAN_MODE


def test_update_plan_emits_structured_control_and_rejects_untriggered_revision(
    tmp_path,
):
    events = []

    async def emit_control(control):
        events.append(control)

    ctx = ToolContext(
        workspace_root=tmp_path,
        sandbox_enabled=False,
        plan_control_emitter=emit_control,
    )
    tool = UpdatePlanTool()
    first = asyncio.run(tool.execute({
        "items": [
            {
                "id": "inspect",
                "step": "Inspect",
                "status": "completed",
                "acceptance_checks": ["problem is reproduced"],
                "evidence_refs": ["test:repro"],
            },
            {
                "id": "implement",
                "step": "Implement",
                "status": "in_progress",
                "depends_on": ["inspect"],
            },
        ],
        "trigger_fact": "reproduction established",
    }, ctx))

    assert first.is_error is False
    assert events and events[0].nodes[0].id == "inspect"
    assert events[0].nodes[1].depends_on == ("inspect",)
    assert plan_snapshot_views(events[0])[:3] == (
        ("Inspect", "Implement"),
        ("completed", "in_progress"),
        1,
    )

    ctx.plan_control = events[0]
    second = asyncio.run(tool.execute({
        "items": [
            {
                "id": "inspect",
                "step": "Inspect",
                "status": "completed",
                "acceptance_checks": ["problem is reproduced"],
                "evidence_refs": ["test:repro"],
            },
            {
                "id": "implement",
                "step": "Implement the fix",
                "status": "in_progress",
                "depends_on": ["inspect"],
            },
        ],
    }, ctx))
    assert second.is_error is True
    assert "trigger_fact" in second.content


def test_agent_loop_applies_complexity_policy_to_tool_plan_mode(tmp_path):
    call = ToolCall("plan-1", "update_plan", {
        "items": [
            {
                "id": "plan",
                "step": "Plan the migration",
                "status": "completed",
                "owner": "planner",
                "acceptance_checks": ["scope is known"],
                "evidence_refs": ["repo-map:1"],
            },
            {
                "id": "execute",
                "step": "Apply the migration",
                "status": "in_progress",
                "owner": "executor",
                "depends_on": ["plan"],
            },
            {
                "id": "verify",
                "step": "Verify rollback",
                "status": "pending",
                "owner": "verifier",
                "depends_on": ["execute"],
                "acceptance_checks": ["down migration succeeds"],
            },
        ],
        "trigger_fact": "long-horizon migration selected planner_executor",
    })
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[call]),
            "Plan accepted.",
        ]),
        registry=ToolRegistry([UpdatePlanTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    events = []
    loop.bus.subscribe(events.append)
    request = ApplicationRequestFactory.from_input_source(
        "repl",
        goal="long migration",
        task_shape=TaskShape.LONG_HORIZON,
    )

    assert asyncio.run(loop.run(
        "long migration",
        application_request=request,
    )) == "Plan accepted."
    plan_event = next(
        event for event in events if isinstance(event, PlanUpdated))
    assert plan_event.plan_control["mode"] == "planner_executor"
    nodes = plan_event.plan_control["nodes"]
    assert [node["id"] for node in nodes] == ["plan", "execute", "verify"]


def test_structured_user_edit_then_approval_includes_contract_details(tmp_path):
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)
    loop = SimpleNamespace(
        tool_context=ctx,
        _turn_tool_context=ctx,
        invalidate_tool_pool=lambda: None,
        sync_context_attachments=lambda **_: None,
    )
    permissions = RuntimePermissionManager(loop)
    coordinator = PlanApprovalCoordinator(permissions)
    coordinator.start_request("Implement migration")
    initial = PlanControl.from_nodes(
        (
            PlanNode(
                id="inspect",
                objective="Inspect migration",
                owner="planner",
                acceptance_checks=("current schema recorded",),
            ),
            PlanNode(
                id="apply",
                objective="Apply migration",
                owner="executor",
                depends_on=("inspect",),
            ),
        ),
        mode=PlanControlMode.PLAN_MODE,
        approval=PlanApprovalStatus.PENDING,
        trigger_fact="initial plan",
    )
    coordinator.record_control(initial)

    edited = coordinator.edit_nodes(
        [
            {
                "id": "inspect",
                "objective": "Inspect migration",
                "owner": "planner",
                "acceptance_checks": ["current schema recorded"],
            },
            {
                "id": "apply",
                "objective": "Apply migration",
                "owner": "executor",
                "depends_on": ["inspect"],
            },
            {
                "id": "rollback",
                "objective": "Verify rollback",
                "owner": "verifier",
                "depends_on": ["apply"],
                "acceptance_checks": ["down migration succeeds"],
            },
        ],
        trigger_fact="user requested rollback verification",
    )
    assert edited.revision.trigger_fact == (
        "user requested rollback verification")
    assert edited.nodes[-1].id == "rollback"

    approved, _, prompt = coordinator.approve()
    assert approved and prompt is not None
    assert "depends_on=inspect" in prompt
    assert "acceptance: current schema recorded" in prompt
    assert "user requested rollback verification" in prompt


def test_plan_edit_command_accepts_structured_json(tmp_path):
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)
    loop = SimpleNamespace(
        tool_context=ctx,
        _turn_tool_context=ctx,
        invalidate_tool_pool=lambda: None,
        sync_context_attachments=lambda **_: None,
    )
    coordinator = PlanApprovalCoordinator(RuntimePermissionManager(loop))
    coordinator.start_request("Task")
    coordinator.record_control(PlanControl.from_nodes(
        (PlanNode(id="one", objective="First", owner="planner"),),
        mode=PlanControlMode.PLAN_MODE,
        approval=PlanApprovalStatus.PENDING,
        trigger_fact="initial plan",
    ))
    state = SessionViewState()

    result = handle_plan_command(
        'edit {"trigger_fact":"user split",'
        '"items":[{"id":"one","objective":"First","owner":"planner"},'
        '{"id":"two","objective":"Second","owner":"executor",'
        '"depends_on":["one"]}]}',
        coordinator,
        state,
    )

    assert result.handled
    assert state.plan_items == ("First", "Second")
    assert state.plan_control["revision"]["trigger_fact"] == "user split"
    assert coordinator.status is PlanApprovalStatus.PENDING
