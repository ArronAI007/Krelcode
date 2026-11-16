"""对话级 rewind：turn 时间线、文件/历史/plan 联合恢复与历史点 fork。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.agent.runtime import AgentRuntime
from krelcode.core.events import (
    EventBus,
    PlanUpdated,
    RunCancelled,
    RunFinished,
    RunStarted,
    ToolCallStarted,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.safety.runtime import PlanApprovalStatus
from krelcode.session.checkpoint import CheckpointManager
from krelcode.session.commands import builtin_command_registry
from krelcode.session.controller import SessionController
from krelcode.session.manager import inspect_session
from krelcode.session.recorder import SessionRecorder, load_history
from krelcode.tools.base import PermissionMode, ToolContext
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.tools.registry import ToolRegistry


def build_controller(tmp_path: Path, *, session: bool = False):
    bus = EventBus()
    loop = AgentLoop(
        llm=MockProvider(scripted=[]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
    )
    checkpoints = CheckpointManager(tmp_path)
    checkpoints.attach(bus)
    session_path = None
    sessions_dir = None
    if session:
        sessions_dir = tmp_path / ".krelcode" / "sessions"
        sessions_dir.mkdir(parents=True)
        session_path = sessions_dir / "source-session.jsonl"
        recorder = SessionRecorder(
            session_path,
            lambda: loop.history,
            metadata={
                "session_id": "source-session",
                "title": "source",
                "provider": "mock",
                "model": "mock",
                "workspace": str(tmp_path),
            },
        )
        recorder.attach(bus)
        recorder.flush_history()
        loop.session_recorder = recorder
    runtime = AgentRuntime(loop)
    controller = SessionController(
        runtime,
        session_path=str(session_path) if session_path else None,
        sessions_dir=sessions_dir,
        checkpoints=checkpoints,
    )
    return loop, bus, controller, checkpoints


async def emit_turn(
    loop: AgentLoop,
    bus: EventBus,
    controller: SessionController,
    *,
    turn_id: str,
    user: str,
    answer: str,
    plan_items: tuple[str, ...] = (),
) -> None:
    loop.history.append(Message.user(user))
    await bus.emit(RunStarted(user, turn_id=turn_id))
    if plan_items:
        statuses = tuple("pending" for _ in plan_items)
        await bus.emit(PlanUpdated(
            items=plan_items,
            statuses=statuses,
            turn_id=turn_id,
        ))
        controller.record_plan(plan_items, statuses)
    loop.history.append(Message.assistant(answer))
    await bus.emit(RunFinished(answer, turn_id=turn_id))


def test_timeline_history_and_plan_rewind_to_turn_boundary(tmp_path):
    loop, bus, controller, _ = build_controller(tmp_path, session=True)

    async def scenario():
        await emit_turn(
            loop, bus, controller, turn_id="turn-1", user="第一轮",
            answer="答一", plan_items=("读取代码", "修改代码"))
        await emit_turn(
            loop, bus, controller, turn_id="turn-2", user="第二轮",
            answer="答二")

    asyncio.run(scenario())
    assert [entry.label for entry in controller.rewind.timeline.entries] == [
        "第一轮", "第二轮"]
    assert [entry.status for entry in controller.rewind.timeline.entries] == [
        "done", "done"]
    listing = asyncio.run(controller.execute_command("/rewind"))
    listing_text = chr(10).join(listing.output)
    assert "[1] done · 第一轮" in listing_text
    assert "[2] done · 第二轮" in listing_text

    loop.history[0].content = "SYS-CURRENT"
    result = asyncio.run(controller.execute_command("/rewind 2"))
    assert "已回到 turn 2 开始前" in "\n".join(result.output)
    assert [message.content for message in loop.history] == [
        "SYS-CURRENT", "第一轮", "答一"]
    assert controller.plan_manager.snapshot.items == ("读取代码", "修改代码")
    assert [entry.label for entry in controller.rewind.timeline.entries] == ["第一轮"]

    recorder_path = loop.session_recorder.path
    assert [message.content for message in load_history(recorder_path)] == [
        "SYS-CURRENT", "第一轮", "答一"]
    records = [json.loads(line) for line in recorder_path.read_text(
        encoding="utf-8").splitlines()]
    assert records[-2]["type"] == "rewind"
    assert records[-2]["removed_turns"] == 1
    assert records[-1]["type"] == "history"


def test_rewind_restores_multiple_checkpoints_in_reverse_order(tmp_path):
    (tmp_path / "a.txt").write_text("v1", encoding="utf-8")
    loop, bus, controller, _ = build_controller(tmp_path, session=True)

    async def turn1():
        loop.history.append(Message.user("修改 a"))
        await bus.emit(RunStarted("修改 a", turn_id="turn-1"))
        await bus.emit(ToolCallStarted(ToolCall(
            "c1", "write_file", {"path": "a.txt", "content": "v2"})))
        (tmp_path / "a.txt").write_text("v2", encoding="utf-8")
        loop.history.append(Message.assistant("完成一"))
        await bus.emit(RunFinished("完成一", turn_id="turn-1"))

    async def turn2():
        loop.history.append(Message.user("再修改并新建"))
        await bus.emit(RunStarted("再修改并新建", turn_id="turn-2"))
        await bus.emit(ToolCallStarted(ToolCall(
            "c2", "write_file", {"path": "a.txt", "content": "v3"})))
        (tmp_path / "a.txt").write_text("v3", encoding="utf-8")
        await bus.emit(ToolCallStarted(ToolCall(
            "c3", "write_file", {"path": "new.txt", "content": "new"})))
        (tmp_path / "new.txt").write_text("new", encoding="utf-8")
        loop.history.append(Message.assistant("完成二"))
        await bus.emit(RunFinished("完成二", turn_id="turn-2"))

    asyncio.run(turn1())
    asyncio.run(turn2())

    to_turn2 = controller.rewind.rewind(2)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v2"
    assert not (tmp_path / "new.txt").exists()
    assert to_turn2.files.groups == 1

    controller.rewind.rewind(1)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1"
    assert controller.rewind.timeline.entries == []


def test_agent_loop_turn_is_rewindable_end_to_end(tmp_path):
    (tmp_path / "a.txt").write_text("v1", encoding="utf-8")
    bus = EventBus()
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[ToolCall(
                "write-1", "write_file",
                {"path": "a.txt", "content": "v2"})]),
            "已完成",
        ]),
        registry=ToolRegistry([WriteFileTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
    )
    checkpoints = CheckpointManager(tmp_path)
    checkpoints.attach(bus)
    runtime = AgentRuntime(loop)
    controller = SessionController(runtime, checkpoints=checkpoints)

    assert asyncio.run(runtime.run_once("修改 a.txt")) == "已完成"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v2"
    entry = controller.rewind.timeline.entries[0]
    assert entry.turn_id == "turn-1"
    assert entry.checkpoint_ids

    controller.rewind.rewind(1)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1"
    assert [message.content for message in loop.history] == ["SYS"]


def test_cancelled_turn_is_complete_and_closes_checkpoint(tmp_path):
    (tmp_path / "a.txt").write_text("before", encoding="utf-8")
    loop, bus, controller, checkpoints = build_controller(tmp_path)

    async def scenario():
        loop.history.append(Message.user("中途取消"))
        await bus.emit(RunStarted("中途取消", turn_id="turn-1"))
        await bus.emit(ToolCallStarted(ToolCall(
            "c1", "write_file", {"path": "a.txt", "content": "after"})))
        (tmp_path / "a.txt").write_text("after", encoding="utf-8")
        loop.history.append(Message.assistant("[已被用户中止]"))
        await bus.emit(RunCancelled("用户中止", turn_id="turn-1"))

    asyncio.run(scenario())
    entry = controller.rewind.timeline.entries[0]
    assert entry.status == "cancelled"
    assert len(checkpoints.groups_for_turn("turn-1")) == 1
    controller.rewind.rewind(1)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "before"


def test_checkpoint_turn_identity_is_scoped_by_session(tmp_path):
    (tmp_path / "old.txt").write_text("old-v1", encoding="utf-8")
    (tmp_path / "current.txt").write_text("current-v1", encoding="utf-8")
    mgr = CheckpointManager(tmp_path)

    mgr.begin_group("旧会话", turn_id="turn-1", session_id="session-old")
    mgr.snapshot("old.txt")
    (tmp_path / "old.txt").write_text("old-v2", encoding="utf-8")
    mgr.close_group()

    mgr.begin_group("当前会话", turn_id="turn-1", session_id="session-current")
    mgr.snapshot("current.txt")
    (tmp_path / "current.txt").write_text("current-v2", encoding="utf-8")
    mgr.close_group()

    mgr.restore_turns(["turn-1"], session_id="session-current")
    assert (tmp_path / "current.txt").read_text(encoding="utf-8") == "current-v1"
    assert (tmp_path / "old.txt").read_text(encoding="utf-8") == "old-v2"
    assert mgr.groups_for_turn("turn-1", session_id="session-old")


def test_checkpoint_turn_id_survives_restart(tmp_path):
    (tmp_path / "a.txt").write_text("v1", encoding="utf-8")
    first = CheckpointManager(tmp_path)
    first.begin_group("第一轮", turn_id="turn-1")
    first.snapshot("a.txt")
    (tmp_path / "a.txt").write_text("v2", encoding="utf-8")
    first.close_group()

    second = CheckpointManager(tmp_path)
    groups = second.groups_for_turn("turn-1")
    assert len(groups) == 1 and groups[0].seq == 1
    second.restore_turns(["turn-1"])
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1"


def test_rewind_plan_restores_pending_approval_and_mode(tmp_path):
    loop, bus, controller, _ = build_controller(tmp_path)
    controller.permission_manager.set_mode(PermissionMode.PLAN)

    async def scenario():
        await emit_turn(
            loop, bus, controller, turn_id="turn-1", user="生成计划",
            answer="请确认", plan_items=("检查", "修改"))
        await emit_turn(
            loop, bus, controller, turn_id="turn-2", user="继续",
            answer="收到")

    asyncio.run(scenario())
    assert controller.plan_manager.status is PlanApprovalStatus.PENDING
    controller.rewind.rewind(2)
    assert controller.plan_manager.status is PlanApprovalStatus.PENDING
    assert controller.permission_manager.mode is PermissionMode.PLAN
    assert controller.plan_manager.snapshot.items == ("检查", "修改")


def test_rewind_fork_uses_turn_history_and_plan_without_touching_workspace(tmp_path):
    (tmp_path / "a.txt").write_text("v1", encoding="utf-8")
    loop, bus, controller, _ = build_controller(tmp_path, session=True)

    async def scenario():
        await emit_turn(
            loop, bus, controller, turn_id="turn-1", user="第一轮",
            answer="答一", plan_items=("计划一",))
        await emit_turn(
            loop, bus, controller, turn_id="turn-2", user="第二轮",
            answer="答二")
        loop.history[0].content = "SYS-CURRENT"
        (tmp_path / "a.txt").write_text("current", encoding="utf-8")

    asyncio.run(scenario())
    result = asyncio.run(controller.execute_command(
        "/rewind fork 1 historical-branch"))
    text = "\n".join(result.output)
    assert "已从 turn 1" in text
    assert [message.content for message in loop.history] == [
        "SYS-CURRENT", "第一轮", "答一"]
    assert controller.plan_manager.snapshot.items == ("计划一",)
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "current"
    assert controller.rewind.timeline.entries == []

    info = inspect_session(loop.session_recorder.path)
    assert info is not None
    assert info.title == "historical-branch"
    assert info.parent_id == "source-session"
    assert load_history(loop.session_recorder.path) == loop.history


def test_fork_command_supports_turn_option(tmp_path):
    loop, bus, controller, _ = build_controller(tmp_path, session=True)
    asyncio.run(emit_turn(
        loop, bus, controller, turn_id="turn-1", user="保留", answer="回答"))
    asyncio.run(emit_turn(
        loop, bus, controller, turn_id="turn-2", user="移除", answer="旧回答"))

    result = asyncio.run(controller.execute_command(
        "/fork --turn 1 legacy-style"))
    assert "已从 turn 1" in "\n".join(result.output)
    assert loop.history[-2:] == [
        Message.user("保留"), Message.assistant("回答")]


def test_invalid_or_running_rewind_does_not_change_state(tmp_path):
    (tmp_path / "a.txt").write_text("v1", encoding="utf-8")
    loop, bus, controller, _ = build_controller(tmp_path)

    async def scenario():
        loop.history.append(Message.user("修改"))
        await bus.emit(RunStarted("修改", turn_id="turn-1"))
        await bus.emit(ToolCallStarted(ToolCall(
            "c1", "write_file", {"path": "a.txt", "content": "v2"})))
        (tmp_path / "a.txt").write_text("v2", encoding="utf-8")
        loop.history.append(Message.assistant("完成"))
        await bus.emit(RunFinished("完成", turn_id="turn-1"))

        before = list(loop.history)
        invalid = await controller.execute_command("/rewind 9")
        assert "超出范围" in "\n".join(invalid.output)
        assert loop.history == before
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v2"

        task = asyncio.create_task(asyncio.sleep(30))
        controller.runtime._task = task
        running = await controller.execute_command("/rewind 1")
        assert "任务运行中" in "\n".join(running.output)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        controller.runtime._task = None

    asyncio.run(scenario())


def test_rewind_command_is_registered_and_undo_remains_available():
    registry = builtin_command_registry()
    assert registry.resolve("/rewind") is not None
    assert registry.resolve("/undo") is not None
    assert registry.resolve("/fork").argument_hint == "[--turn <n>] [名称]"
