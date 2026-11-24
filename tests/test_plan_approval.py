"""Plan 生成、待确认、执行确认和取消闭环。"""

import asyncio
from types import SimpleNamespace

from krelcode.agent.query import QueryEvent
from krelcode.agent.tool_pool import TurnToolPool
from krelcode.app import default_tools
from krelcode.core.events import PlanUpdated
from krelcode.safety.runtime import (
    PlanApprovalCoordinator,
    PlanApprovalStatus,
    RuntimePermissionManager,
)
from krelcode.session.repl_session import ReplSessionRunner
from krelcode.session.state import SessionViewState
from krelcode.tools.base import (
    PermissionMode,
    RuntimeSnapshot,
    ToolContext,
)
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.repl import format_plan_lines, handle_plan_command


def fake_loop(tmp_path, mode=PermissionMode.DEFAULT):
    ctx = ToolContext(
        workspace_root=tmp_path,
        runtime=RuntimeSnapshot(permission_mode=mode),
    )
    loop = SimpleNamespace(
        tool_context=ctx,
        _turn_tool_context=ctx,
        _active_tool_pool=None,
        sync_context_attachments=lambda **_: None,
        update_runtime_state=lambda **values: loop.__dict__.update(values),
    )
    return loop


def coordinator(tmp_path, mode=PermissionMode.DEFAULT):
    permissions = RuntimePermissionManager(fake_loop(tmp_path, mode))
    return PlanApprovalCoordinator(permissions)


def test_plan_request_enters_plan_mode_and_is_readonly_instruction(tmp_path):
    plan = coordinator(tmp_path)

    prompt = plan.start_request("重构认证模块")

    assert plan.permissions.mode is PermissionMode.PLAN
    assert plan.permissions.previous_mode is PermissionMode.DEFAULT
    assert "只做只读调查" in prompt
    assert "update_plan" in prompt
    assert "重构认证模块" in prompt


def test_normal_prompt_in_shift_tab_plan_mode_is_wrapped(tmp_path):
    plan = coordinator(tmp_path, mode=PermissionMode.PLAN)

    prompt = plan.wrap_request("分析仓库")

    assert prompt.startswith("<plan_mode_request>")
    assert "分析仓库" in prompt
    assert plan.wrap_request(prompt) == prompt


def test_record_then_approve_restores_previous_mode_and_builds_execution_prompt(tmp_path):
    plan = coordinator(tmp_path)
    plan.start_request("实现登录")
    plan.record(
        ("读取代码", "实现功能", "运行测试"),
        ("completed", "pending", "pending"),
        active_index=-1,
        explanation="先确认现有结构",
    )

    assert plan.status is PlanApprovalStatus.PENDING
    ok, message, prompt = plan.approve()

    assert ok and prompt is not None
    assert plan.status is PlanApprovalStatus.APPROVED
    assert plan.permissions.mode is PermissionMode.DEFAULT
    assert "1. 读取代码" in prompt
    assert "用户已批准" in prompt
    assert "恢复权限模式: default" in message


def test_approve_without_plan_fails_closed(tmp_path):
    plan = coordinator(tmp_path)

    ok, message, prompt = plan.approve()

    assert not ok and prompt is None
    assert "没有可批准" in message


def test_approve_requires_pending_not_already_cancelled(tmp_path):
    plan = coordinator(tmp_path)
    plan.start_request("任务")
    plan.record(("步骤",), ("pending",))
    ok, _ = plan.cancel()
    assert ok

    approved, message, prompt = plan.approve()

    assert not approved and prompt is None
    assert "不处于待确认" in message


def test_cancel_restores_previous_mode_and_keeps_plan_history(tmp_path):
    plan = coordinator(tmp_path)
    plan.start_request("任务")
    plan.record(("步骤一", "步骤二"), ("pending", "pending"))

    ok, message = plan.cancel()

    assert ok and "default" in message
    assert plan.status is PlanApprovalStatus.CANCELLED
    assert plan.permissions.mode is PermissionMode.DEFAULT
    assert plan.snapshot.items == ("步骤一", "步骤二")


def test_manual_mode_change_away_from_plan_cancels_pending_approval(tmp_path):
    plan = coordinator(tmp_path)
    plan.start_request("任务")
    plan.record(("步骤",), ("pending",))

    previous = plan.permissions.mode
    plan.permissions.set_mode(PermissionMode.READ_ONLY)
    plan.on_mode_change(previous, PermissionMode.READ_ONLY)

    assert plan.status is PlanApprovalStatus.CANCELLED
    assert plan.permissions.mode is PermissionMode.READ_ONLY


def test_plan_mode_tool_pool_exposes_readonly_and_plan_tools_only(tmp_path):
    registry = ToolRegistry(default_tools())
    runtime = RuntimeSnapshot(permission_mode=PermissionMode.PLAN)
    pool = TurnToolPool(registry, runtime=runtime)

    names = set(pool.names())
    assert {"read_file", "grep", "update_plan", "ask_user"} <= names
    assert not {"write_file", "edit_file", "run_command"} & names


def test_plan_command_generates_prompt_and_approve_returns_run_prompt(tmp_path):
    plan = coordinator(tmp_path)
    state = SessionViewState()

    started = handle_plan_command("检查并修复登录", plan, state)
    plan.record(("检查", "修复", "验证"), ("pending", "pending", "pending"))
    state.plan_items = plan.snapshot.items
    state.plan_statuses = plan.snapshot.statuses
    state.plan_approval_status = plan.status.value
    view = handle_plan_command("", plan, state)
    approved = handle_plan_command("approve", plan, state)

    assert started.run_prompt is not None
    assert any("待确认" in line for line in view.output)
    assert approved.run_prompt is not None
    assert "用户已批准" in approved.run_prompt
    assert state.plan_approval_status == "approved"


def test_plan_actions_are_blocked_while_a_turn_is_running(tmp_path):
    plan = coordinator(tmp_path)
    state = SessionViewState()

    result = handle_plan_command(
        "approve", plan, state, allow_actions=False)

    assert result.run_prompt is None
    assert "等待当前任务结束" in "\n".join(result.output)


def test_runner_records_plan_event_into_coordinator(tmp_path):
    plan = coordinator(tmp_path)
    plan.start_request("任务")

    class DummyController:
        plan_manager = plan

        def __init__(self):
            self.calls = []

        def record_plan(self, items, statuses, active_index=-1, explanation=""):
            self.calls.append((items, statuses, active_index, explanation))
            self.plan_manager.record(
                items, statuses,
                active_index=active_index,
                explanation=explanation,
            )

    class FakeQuery:
        async def _events(self):
            yield QueryEvent(
                "turn-1", 1,
                PlanUpdated(
                    ("读取", "修改"),
                    active_index=0,
                    statuses=("in_progress", "pending"),
                    explanation="原因",
                ),
            )

        @property
        def events(self):
            return self._events()

    controller = DummyController()
    state = SessionViewState()
    runner = ReplSessionRunner(
        controller,
        state,
        on_event=lambda event: None,
    )

    asyncio.run(runner._consume(FakeQuery()))

    assert controller.calls == [
        (("读取", "修改"), ("in_progress", "pending"), 0, "原因")
    ]
    assert state.plan_approval_status == "pending"
    assert plan.status is PlanApprovalStatus.PENDING


def test_format_plan_lines_shows_pending_approval():
    state = SessionViewState(
        plan_items=("读取", "修改"),
        plan_statuses=("pending", "pending"),
        plan_approval_status="pending",
    )

    text = "\n".join(format_plan_lines(state))

    assert "/plan approve" in text
    assert "/plan cancel" in text
