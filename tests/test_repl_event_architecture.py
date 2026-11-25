"""第一阶段：事件 reducer、工具进度、session runner 与状态栏。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from krelcode.agent.loop import AgentLoop
from krelcode.agent.query import QueryEvent
from krelcode.agent.runtime import AgentRuntime
from krelcode.core.events import (
    LLMStreamDelta,
    PlanUpdated,
    QueryPhaseChanged,
    RunFinished,
    RunStarted,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    ToolOutputDelta,
    ToolProgress,
    UserInputRequested,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.base import ProviderCapabilities
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.controller import SessionController
from krelcode.session.repl_session import ReplSessionRunner, SessionPrompt
from krelcode.session.state import SessionEventReducer, SessionPhase, SessionViewState
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.builtin.run_command import RunCommandTool
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.approval import ApprovalBroker
from krelcode.ui.render import StreamRenderer
from krelcode.ui.repl import IDLE_PROMPT, PromptWriter
from krelcode.ui.terminal_presenter import TerminalPresenter


def envelope(event, sequence: int = 0, turn_id: str = "turn-1") -> QueryEvent:
    return QueryEvent(turn_id, sequence, event)


def test_reducer_folds_full_query_sequence_into_view_state():
    state = SessionViewState()
    reducer = SessionEventReducer()
    call = ToolCall("c1", "run_command", {"command": "pytest"})
    events = [
        RunStarted("run tests"),
        StepStarted(2),
        QueryPhaseChanged("model", "模型生成中（Step 2）"),
        LLMStreamDelta("开始"),
        ToolCallStarted(call),
        ToolProgress("c1", "run_command", "命令运行中", {"pid": 42}),
        ToolOutputDelta("c1", "run_command", "hello "),
        ToolOutputDelta("c1", "run_command", "world"),
        ToolCallCompleted(call, "hello world", False),
        PlanUpdated(("读取", "测试"), active_index=1),
        UserInputRequested("input-1", "继续吗？", ("是", "否")),
        RunFinished("完成"),
    ]

    for index, event in enumerate(events):
        reducer.apply(envelope(event, index), state)

    assert state.phase is SessionPhase.RUNNING
    assert state.current_turn_id == "turn-1"
    assert state.current_step == 2
    assert state.query_phase == "finished"
    assert state.tool_calls_started == 1
    assert state.tool_calls_completed == 1
    assert state.tool_output_chars == 11
    assert state.plan_items == ("读取", "测试")
    assert state.plan_active_index == 1
    assert state.input_request_id == ""
    assert state.active_tool_name == ""
    assert state.progress == "回答完成"


def test_status_text_contains_phase_step_tool_count_and_elapsed():
    state = SessionViewState()
    state.begin_turn("等待模型响应", turn_id="turn-9")
    state.started_at = 100.0
    state.query_phase = "tool"
    state.current_step = 3
    state.active_tool_name = "run_command"
    state.tool_calls_started = 2
    state.tool_calls_completed = 1
    state.tool_output_chars = 8
    state.progress = "run_command · 命令运行中"

    status = state.status_text(now=101.25)

    assert "run_command · 命令运行中" in status
    assert "Step 3" in status
    assert "工具 1/2" in status
    assert "输出 8 字符" in status
    assert status.endswith("1.2s")


class EmittingTool:
    name = "emit_tool"
    description = "emit progress and output"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    async def execute(self, args, ctx):
        await ctx.emit_progress("阶段一", {"percent": 50})
        await ctx.emit_output("live output", "stdout")
        return ToolResult(content="done")


def test_agent_loop_publishes_tool_progress_and_output_delta(tmp_path):
    call = ToolCall("emit-1", "emit_tool", {})
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[call]),
            "finished",
        ]),
        registry=ToolRegistry([EmittingTool()]),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    events = []
    loop.bus.subscribe(events.append)

    assert asyncio.run(loop.run("emit")) == "finished"

    phases = [event.phase for event in events
              if isinstance(event, QueryPhaseChanged)]
    progress = [event for event in events if isinstance(event, ToolProgress)]
    deltas = [event for event in events if isinstance(event, ToolOutputDelta)]
    assert phases[:2] == ["awaiting_model", "model"]
    assert any(event.message == "阶段一" for event in progress)
    assert [(event.text, event.stream) for event in deltas] == [
        ("live output", "stdout"),
    ]
    assert deltas[0].tool_call_id == "emit-1"


def test_run_command_streams_chunks_before_process_exit(tmp_path):
    async def scenario():
        chunks = []
        progress = []
        first_chunk = asyncio.Event()

        async def output(text, stream):
            chunks.append((text, stream))
            first_chunk.set()

        async def emit_progress(message, data):
            progress.append((message, data))

        ctx = ToolContext(
            workspace_root=tmp_path,
            sandbox_enabled=False,
            progress_emitter=emit_progress,
            output_emitter=output,
        )
        task = asyncio.create_task(RunCommandTool().execute(
            {"command": "printf first; sleep 0.15; printf second"}, ctx))
        await asyncio.wait_for(first_chunk.wait(), timeout=1)
        assert task.done() is False
        result = await task
        return result, chunks, progress

    result, chunks, progress = asyncio.run(scenario())

    assert not result.is_error
    assert "firstsecond" in result.content
    assert "".join(text for text, _ in chunks) == "firstsecond"
    assert {stream for _, stream in chunks} == {"combined"}
    assert any("命令运行中" in message for message, _ in progress)
    assert any("命令结束" in message for message, _ in progress)


def test_renderer_caches_output_delta_and_previews_only_latest_line():
    parts = []
    renderer = StreamRenderer(write=parts.append, color=False)
    call = ToolCall("c1", "run_command", {"command": "printf secret"})
    renderer(RunStarted("q"))
    renderer(ToolCallStarted(call))
    renderer(ToolOutputDelta(
        "c1", "run_command", "first line\nlatest line", "combined"))

    assert renderer.tool_outputs[0].content == "first line\nlatest line"
    assert "latest line" in "".join(parts)
    assert "first line" not in "".join(parts)
    assert renderer.tool_outputs[0].expanded is False


def test_renderer_reports_active_output_until_final_event():
    renderer = StreamRenderer(write=lambda _text: None, color=False)

    renderer(RunStarted("question"))
    assert not renderer.output_active
    renderer(LLMStreamDelta("partial"))
    assert renderer.output_active
    renderer(RunFinished("partial"))
    assert not renderer.output_active


def test_terminal_status_does_not_restore_dock_during_live_output():
    class Prompt:
        def __init__(self) -> None:
            self.resumes = 0

        def set_status(self, _status: str) -> None:
            return

        def retarget_prompt(self, _old: str, _new: str) -> None:
            return

        def resume_or_show(self, _fallback: str) -> None:
            self.resumes += 1

    presenter = object.__new__(TerminalPresenter)
    presenter.state = SimpleNamespace(is_running=True)
    presenter.controller = object()
    presenter.runner = SimpleNamespace(queue=[])
    presenter.renderer = SimpleNamespace(output_active=True)
    presenter.prompt = Prompt()
    presenter.use_color = False
    presenter.status_cache = "status"
    presenter.last_status_draw = 0.0
    presenter.refresh_panels = lambda **_kwargs: None
    presenter.render_status = lambda _view: "status"

    presenter.refresh_status(presenter.state, force=True)
    assert presenter.prompt.resumes == 0

    presenter.renderer.output_active = False
    presenter.refresh_status(presenter.state, force=True)
    assert presenter.prompt.resumes == 1


def test_prompt_writer_status_and_suspend_preserve_draft(monkeypatch):
    import krelcode.ui.repl as repl

    monkeypatch.setattr(
        repl.shutil, "get_terminal_size",
        lambda _fallback=None: repl.os.terminal_size((60, 24)),
    )
    parts = []
    prompt = PromptWriter(write=parts.append, enabled=True, color=False)
    prompt.show(IDLE_PROMPT)
    prompt.update(IDLE_PROMPT + "draft", [])
    prompt.set_status("工具 run_command · 1.2s")

    prompt.suspend()
    prompt.set_status("工具 run_command · 1.3s")
    prompt.resume_or_show(IDLE_PROMPT + "fallback")

    text = "".join(parts)
    assert "工具 run_command · 1.2s" in text
    assert "工具 run_command · 1.3s" in text
    assert prompt._last[0] == IDLE_PROMPT + "draft"
    assert "fallback" not in text


def test_repl_session_runner_owns_queue_and_starts_next_prompt(tmp_path):
    async def scenario():
        loop = AgentLoop(
            llm=MockProvider(scripted=["reply one", "reply two"]),
            registry=ToolRegistry(),
            tool_context=ToolContext(
                workspace_root=tmp_path, sandbox_enabled=False),
            policy=AllowAllPolicy(),
            system_prompt="SYS",
        )
        runtime = AgentRuntime(loop)
        controller = SessionController(runtime)
        state = SessionViewState()
        notices = []
        runner = ReplSessionRunner(
            controller,
            state,
            on_event=lambda _event: None,
            on_notice=notices.append,
            on_status=lambda _state: None,
        )

        runner.start(SessionPrompt.create("one"))
        position = runner.queue_prompt(SessionPrompt.create("two"))
        assert position == 1
        assert runner.is_running

        for _ in range(10):
            if not runner.is_running and not runner.queue:
                break
            done, _ = await asyncio.wait(
                runner.wait_tasks(), return_when=asyncio.FIRST_COMPLETED)
            await runner.process_completed(done)
        else:
            raise AssertionError("runner 未在有限轮次内完成队列")

        assert [m.content for m in loop.history if m.role == "user"] == [
            "one", "two",
        ]
        assert state.phase is SessionPhase.IDLE

    asyncio.run(scenario())


class HangingProvider:
    name = "hanging"
    model = "test"
    capabilities = ProviderCapabilities(
        supports_tools=True, supports_streaming=True)

    def __init__(self):
        self.entered = asyncio.Event()

    async def stream(self, messages, tools=None):
        self.entered.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover - 保持 async generator 类型
            yield None


def test_repl_session_runner_interrupt_cleans_state_and_history(tmp_path):
    async def scenario():
        provider = HangingProvider()
        loop = AgentLoop(
            llm=provider,
            registry=ToolRegistry(),
            tool_context=ToolContext(
                workspace_root=tmp_path, sandbox_enabled=False),
            policy=AllowAllPolicy(),
            system_prompt="SYS",
        )
        runner = ReplSessionRunner(
            SessionController(AgentRuntime(loop)),
            SessionViewState(),
            on_event=lambda _event: None,
            on_notice=lambda _message: None,
        )
        runner.start(SessionPrompt.create("hang"))
        await asyncio.wait_for(provider.entered.wait(), timeout=1)

        await runner.finish_current(interrupt=True)

        assert runner.is_running is False
        assert runner.state.phase is SessionPhase.IDLE
        assert loop.history[-1].role == "assistant"
        assert "中止" in (loop.history[-1].content or "")

    asyncio.run(scenario())


def test_repl_session_runner_routes_approval_without_reading_stdin():
    async def scenario():
        broker = ApprovalBroker()
        state = SessionViewState()
        shown = []
        notices = []
        runner = ReplSessionRunner(
            controller=None,  # 该测试只验证审批 rendezvous
            state=state,
            on_event=lambda _event: None,
            on_approval=shown.append,
            on_notice=notices.append,
            approval_broker=broker,
        )
        call = ToolCall("approval-1", "run_command", {"command": "echo ok"})
        approval = asyncio.create_task(broker.request(call, "需要确认"))
        runner.arm_approval_watcher()
        done, _ = await asyncio.wait(
            {runner.approval_task}, return_when=asyncio.FIRST_COMPLETED)
        await runner.process_completed(done)

        assert shown == [runner.pending]
        assert runner.pending.tool_call == call
        assert runner.resolve_approval("n") is True
        assert await approval == "no"
        assert any("审批结果" in notice for notice in notices)

    asyncio.run(scenario())
