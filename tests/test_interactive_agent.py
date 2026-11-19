"""第二阶段：ask_user、update_plan 与交互式会话闭环。"""

from __future__ import annotations

import asyncio
import io
import json

from krelcode.agent.loop import AgentLoop
from krelcode.agent.query import QueryEvent
from krelcode.agent.runtime import AgentRuntime
from krelcode.agent.usage import UsageTracker
from krelcode.app import default_tools
from krelcode.core.redact import SecretRedactor
from krelcode.core.events import (
    PlanUpdated,
    UserInputRequested,
    UserInputResolved,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.controller import SessionController
from krelcode.session.interactions import UserInputBroker
from krelcode.session.repl_session import ReplSessionRunner, SessionPrompt
from krelcode.session.state import SessionEventReducer, SessionViewState
from krelcode.session.stream_json import (
    StreamJsonEncoder,
    StreamJsonMetadata,
)
from krelcode.session.stream_json_runner import run_stream_json_session
from krelcode.tools.base import (
    PermissionMode,
    RunMode,
    RuntimeSnapshot,
    ToolContext,
)
from krelcode.tools.extension.ask_user import AskUserTool
from krelcode.tools.extension.update_plan import UpdatePlanTool
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.repl import format_plan_lines, run_repl
from krelcode.ui.render import StreamRenderer


def envelope(event, sequence: int = 0) -> QueryEvent:
    return QueryEvent("turn-1", sequence, event)


def test_user_input_broker_maps_option_and_keeps_invalid_request_open():
    async def scenario():
        broker = UserInputBroker()
        exchange = broker.submit(
            "选择颜色", ("红色", "蓝色"), allow_free_text=False)
        request = await broker.next_request()

        invalid = broker.resolve("9", request_id=request.request_id)
        assert invalid.accepted is False
        assert "1-2" in invalid.reason
        assert broker.pending is request
        assert exchange.request.future.done() is False

        accepted = broker.resolve("2", request_id=request.request_id)
        assert accepted.accepted is True
        assert accepted.value == "蓝色"
        assert await exchange.wait() == "蓝色"
        assert broker.pending is None

    asyncio.run(scenario())


def test_update_plan_validates_and_emits_normalized_snapshot(tmp_path):
    captured = []

    async def emit_plan(items, statuses, active_index, explanation):
        captured.append((items, statuses, active_index, explanation))

    ctx = ToolContext(
        workspace_root=tmp_path,
        sandbox_enabled=False,
        plan_emitter=emit_plan,
    )

    result = asyncio.run(UpdatePlanTool().execute({
        "items": [
            {"step": "读取架构", "status": "completed"},
            {"step": "实现交互", "status": "in_progress"},
            {"step": "运行测试", "status": "pending"},
        ],
        "explanation": "进入实现阶段",
    }, ctx))

    assert result.is_error is False
    assert captured == [(
        ("读取架构", "实现交互", "运行测试"),
        ("completed", "in_progress", "pending"),
        1,
        "进入实现阶段",
    )]
    assert result.data["active_index"] == 1


def test_update_plan_rejects_multiple_in_progress_steps(tmp_path):
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)
    result = asyncio.run(UpdatePlanTool().execute({
        "items": [
            {"step": "A", "status": "in_progress"},
            {"step": "B", "status": "in_progress"},
        ],
    }, ctx))
    assert result.is_error is True
    assert "最多只能有一个" in result.content


def test_ask_user_fails_closed_without_frontend(tmp_path):
    ctx = ToolContext(
        workspace_root=tmp_path,
        sandbox_enabled=False,
        runtime=RuntimeSnapshot(run_mode=RunMode.SUBAGENT),
    )
    result = asyncio.run(AskUserTool().execute(
        {"question": "继续吗？"}, ctx))
    assert result.is_error is True
    assert result.data["cancelled"] is True
    assert "未提供输入" in result.content


def test_ask_user_and_plan_tools_are_local_read_only_in_plan_mode(tmp_path):
    from krelcode.agent.tool_pool import TurnToolPool

    pool = TurnToolPool(
        ToolRegistry([AskUserTool(), UpdatePlanTool()]),
        runtime=RuntimeSnapshot(
            run_mode=RunMode.REPL,
            permission_mode=PermissionMode.PLAN,
        ),
    )
    assert pool.names() == ["ask_user", "update_plan"]


def test_interactive_tools_are_registered_in_default_toolset():
    names = {tool.name for tool in default_tools()}
    assert {"ask_user", "update_plan"} <= names


def test_renderer_prints_user_input_options_and_free_text_hint():
    parts = []
    renderer = StreamRenderer(write=parts.append, color=False)
    renderer(UserInputRequested(
        "i1", "选择部署方式", ("本地", "云端"), True))
    text = "".join(parts)
    assert "选择部署方式" in text
    assert "1. 本地" in text
    assert "2. 云端" in text
    assert "自定义回答" in text


def test_agent_loop_ask_user_round_trip_does_not_add_answer_to_user_history(tmp_path):
    call = ToolCall(
        "ask-1",
        "ask_user",
        {
            "question": "选择颜色",
            "options": ["红色", "蓝色"],
            "allow_free_text": False,
        },
    )
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[call]),
        "已按蓝色继续。",
    ])
    broker = UserInputBroker()
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([AskUserTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        user_input_port=broker,
    )
    events = []
    loop.bus.subscribe(events.append)

    async def scenario():
        turn = asyncio.create_task(loop.run("请选择"))
        request = await asyncio.wait_for(broker.next_request(), timeout=1)
        assert request.prompt == "选择颜色"
        resolution = broker.resolve("2", request_id=request.request_id)
        assert resolution.accepted
        return await asyncio.wait_for(turn, timeout=2)

    assert asyncio.run(scenario()) == "已按蓝色继续。"

    requested = [event for event in events if isinstance(event, UserInputRequested)]
    resolved = [event for event in events if isinstance(event, UserInputResolved)]
    assert requested[0].request_id == resolved[0].request_id
    assert resolved[0].answered is True
    user_messages = [m.content for m in loop.history if m.role == "user"]
    assert user_messages == ["请选择"]
    tool_results = [m.content for m in loop.history if m.role == "tool"]
    assert any("用户回答：蓝色" in content for content in tool_results)


def test_repl_runner_routes_user_input_and_finishes_query(tmp_path):
    call = ToolCall(
        "ask-1",
        "ask_user",
        {"question": "是否继续？", "options": ["是", "否"]},
    )
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[call]),
        "继续完成。",
    ])
    broker = UserInputBroker()
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([AskUserTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        user_input_port=broker,
    )
    runner = ReplSessionRunner(
        SessionController(AgentRuntime(loop)),
        SessionViewState(),
        on_event=lambda _event: None,
        input_broker=broker,
    )

    async def pump_until_input():
        for _ in range(20):
            if runner.pending_input is not None:
                return
            done, _ = await asyncio.wait(
                runner.wait_tasks(), return_when=asyncio.FIRST_COMPLETED)
            await runner.process_completed(done)
        raise AssertionError("未进入等待用户输入状态")

    async def pump_to_end():
        for _ in range(20):
            if not runner.is_running:
                return
            done, _ = await asyncio.wait(
                runner.wait_tasks(), return_when=asyncio.FIRST_COMPLETED)
            await runner.process_completed(done)
        raise AssertionError("查询未结束")

    async def scenario():
        runner.start(SessionPrompt.create("执行任务"))
        await pump_until_input()
        assert runner.resolve_user_input("1") is True
        await pump_to_end()
        assert loop.history[-1].content == "继续完成。"

    asyncio.run(scenario())
    assert [m.content for m in loop.history if m.role == "user"] == ["执行任务"]


def test_run_repl_end_to_end_ask_user_then_final_answer(tmp_path):
    call = ToolCall(
        "ask-1",
        "ask_user",
        {
            "question": "选择颜色",
            "options": ["红色", "蓝色"],
            "allow_free_text": False,
        },
    )
    broker = UserInputBroker()
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[call]),
            "最终选择蓝色。",
        ]),
        registry=ToolRegistry([AskUserTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        user_input_port=broker,
    )

    class AsyncLines:
        def __init__(self):
            self.lines = ["请选择", "2", "/exit"]
            self.calls = 0

        async def __call__(self, _prompt):
            self.calls += 1
            if self.calls == 2:
                while broker.pending is None:
                    await asyncio.sleep(0.005)
            else:
                await asyncio.sleep(0.08)
            if not self.lines:
                raise EOFError
            return self.lines.pop(0)

    rc = asyncio.run(run_repl(
        loop,
        input_fn=AsyncLines(),
        input_broker=broker,
    ))
    assert rc == 0
    assert loop.history[-1].content == "最终选择蓝色。"
    assert [m.content for m in loop.history if m.role == "user"] == ["请选择"]


def test_agent_loop_update_plan_emits_typed_snapshot(tmp_path):
    call = ToolCall("plan-1", "update_plan", {
        "items": [
            {"step": "检查现状", "status": "completed"},
            {"step": "实现能力", "status": "in_progress"},
        ],
        "explanation": "开始实现",
    })
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[call]),
            "计划执行完成。",
        ]),
        registry=ToolRegistry([UpdatePlanTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    plans = []
    loop.bus.subscribe(plans.append)

    assert asyncio.run(loop.run("执行计划")) == "计划执行完成。"
    plan_events = [event for event in plans if isinstance(event, PlanUpdated)]
    assert len(plan_events) == 1
    assert plan_events[0].items == ("检查现状", "实现能力")
    assert plan_events[0].statuses == ("completed", "in_progress")
    assert plan_events[0].active_index == 1
    assert plan_events[0].explanation == "开始实现"


def test_interrupt_cancels_pending_user_input_and_repairs_history(tmp_path):
    call = ToolCall("ask-1", "ask_user", {"question": "继续吗？"})
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[call]),
            "不应到达",
        ]),
        registry=ToolRegistry([AskUserTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
        user_input_port=UserInputBroker(),
    )
    runner = ReplSessionRunner(
        SessionController(AgentRuntime(loop)),
        SessionViewState(),
        on_event=lambda _event: None,
        input_broker=loop.user_input_port,
    )

    async def scenario():
        runner.start(SessionPrompt.create("等待"))
        for _ in range(20):
            if runner.pending_input is not None:
                break
            done, _ = await asyncio.wait(
                runner.wait_tasks(), return_when=asyncio.FIRST_COMPLETED)
            await runner.process_completed(done)
        assert runner.pending_input is not None
        await runner.finish_current(interrupt=True)
        assert runner.is_running is False
        assert runner.pending_input is None
        assert loop.history[-1].role == "assistant"
        assert "中止" in (loop.history[-1].content or "")

    asyncio.run(scenario())


def test_reducer_and_plan_formatter_keep_plan_statuses_after_finish():
    state = SessionViewState()
    reducer = SessionEventReducer()
    events = [
        PlanUpdated(
            ("读取", "实现", "测试"),
            statuses=("completed", "in_progress", "pending"),
            active_index=1,
            explanation="实现中",
        ),
        UserInputRequested("i1", "继续？", ("是",), False),
    ]
    for index, event in enumerate(events):
        reducer.apply(envelope(event, index), state)
    assert state.input_allow_free_text is False
    reducer.apply(envelope(UserInputResolved("i1", True), 2), state)
    assert state.awaiting_user_input is False

    state.finish_turn()
    assert state.plan_items == ("读取", "实现", "测试")
    assert state.plan_statuses == ("completed", "in_progress", "pending")
    lines = format_plan_lines(state)
    assert lines[0].endswith("1/3 完成）:")
    assert "[>] 2. 实现" in "\n".join(lines)


def test_stream_json_exposes_plan_and_input_semantics():
    encoder = StreamJsonEncoder(StreamJsonMetadata(
        session_id="s", cwd="/repo", provider="mock", model="m"))
    plan = encoder.encode(envelope(PlanUpdated(
        ("A", "B"),
        statuses=("completed", "in_progress"),
        active_index=1,
        explanation="B 进行中",
    )))
    requested = encoder.encode(envelope(UserInputRequested(
        "i1", "继续？", ("是", "否"), False), 1))
    resolved = encoder.encode(envelope(UserInputResolved("i1", True), 2))

    assert plan["statuses"] == ["completed", "in_progress"]
    assert plan["explanation"] == "B 进行中"
    assert requested["allow_free_text"] is False
    assert resolved["subtype"] == "resolved"
    assert resolved["answered"] is True


def test_stream_json_headless_ask_user_fails_closed_without_hanging(tmp_path):
    call = ToolCall("ask-1", "ask_user", {"question": "需要输入吗？"})
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[call]),
            "已按无交互环境继续。",
        ]),
        registry=ToolRegistry([AskUserTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    loop.skill_manager = None
    runtime = AgentRuntime(loop)
    output = io.StringIO()
    rc = asyncio.run(asyncio.wait_for(run_stream_json_session(
        runtime,
        session_path=None,
        input_processor=None,
        usage=UsageTracker(),
        redactor=SecretRedactor(),
        initial_prompt="执行",
        input_stream=io.StringIO(""),
        output_stream=output,
    ), timeout=2))
    rows = [json.loads(line) for line in output.getvalue().splitlines()]
    assert rc == 0
    completed = next(
        row for row in rows
        if row.get("type") == "tool" and row.get("subtype") == "completed")
    assert completed["is_error"] is True
    assert "未提供输入" in completed["content"]
    assert rows[-1]["subtype"] == "session_end"
