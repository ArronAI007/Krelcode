"""session 层测试：编解码 roundtrip、JSONL 录制、恢复、与主循环集成。"""

import asyncio
import json
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import (
    EventBus, PermissionDenied, RunFinished, RunStarted,
    ToolCallCompleted, ToolCallStarted,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.codec import history_from_dicts, history_to_dicts
from krelcode.session.recorder import (
    SessionRecorder, latest_session, load_history, new_session_path,
)
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


# -- codec -------------------------------------------------------------------------

def test_codec_roundtrip_all_roles():
    history = [
        Message.system("sys"),
        Message.user("你好"),
        Message.assistant(tool_calls=[ToolCall("c1", "get_time", {"tz": "UTC"})]),
        Message.tool_result("c1", "12:00"),
        Message.assistant("中午了"),
    ]
    assert history_from_dicts(history_to_dicts(history)) == history


def test_codec_handles_empty_and_none():
    assert history_from_dicts([]) == []
    m = message = Message.assistant(None, tool_calls=None)
    assert history_from_dicts(history_to_dicts([m]))[0] == m


# -- recorder ------------------------------------------------------------------------

def test_recorder_writes_events_and_history_snapshot(tmp_path):
    path = tmp_path / "s.jsonl"
    history = [Message.system("S"), Message.user("u"), Message.assistant("a")]
    bus = EventBus()
    recorder = SessionRecorder(path, lambda: history)
    recorder.attach(bus)

    tc = ToolCall("c1", "get_time", {"tz": "UTC"})
    async def go():
        await bus.emit(RunStarted("u"))
        await bus.emit(ToolCallStarted(tc))
        await bus.emit(ToolCallCompleted(tc, "x" * 900, False))
        await bus.emit(PermissionDenied(tc, "危险"))
        await bus.emit(RunFinished("a"))
    asyncio.run(go())

    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    types = [l["type"] for l in lines]
    assert types == ["user_input", "tool_call", "tool_result",
                     "permission_denied", "run_finished", "history"]
    # 超长工具结果在审计流里截断
    assert len(lines[2]["content"]) <= 501
    # history 快照可完整恢复
    assert history_from_dicts(lines[-1]["messages"]) == history


def test_load_history_takes_last_snapshot(tmp_path):
    path = tmp_path / "s.jsonl"
    h1 = [Message.user("第一轮")]
    h2 = [Message.user("第一轮"), Message.assistant("回复"), Message.user("第二轮")]
    bus = EventBus()
    recorder = SessionRecorder(path, lambda: h1)
    recorder.attach(bus)
    asyncio.run(bus.emit(RunFinished("x")))
    recorder._get_history = lambda: h2
    asyncio.run(bus.emit(RunFinished("y")))

    assert load_history(path) == h2  # 取最后一份快照


def test_load_history_corrupt_lines_skipped(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"broken\nnot json\n', encoding="utf-8")
    assert load_history(path) == []


def test_latest_session_by_mtime(tmp_path):
    assert latest_session(tmp_path) is None
    a = tmp_path / "a.jsonl"; a.write_text("{}\n")
    b = tmp_path / "b.jsonl"; b.write_text("{}\n")
    import os
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    assert latest_session(tmp_path) == b
    assert new_session_path(tmp_path).parent == tmp_path


def test_recorder_detach_stops_writing(tmp_path):
    path = tmp_path / "s.jsonl"
    bus = EventBus()
    recorder = SessionRecorder(path, lambda: [])
    recorder.attach(bus)
    recorder.detach()
    asyncio.run(bus.emit(RunStarted("x")))
    assert not path.exists() or path.read_text() == ""


# -- 与主循环集成 ----------------------------------------------------------------------

class PingTool:
    name = "ping"
    description = "ping"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args, ctx):
        return ToolResult(content="pong")


def test_full_loop_recorded_then_resumed(tmp_path):
    scripted = [
        Message.assistant(tool_calls=[ToolCall("c1", "ping", {})]),
        "工具说 pong。",
    ]
    bus = EventBus()
    loop = AgentLoop(
        llm=MockProvider(scripted=scripted),
        registry=ToolRegistry([PingTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
    )
    session_path = tmp_path / ".krelcode" / "sessions" / "s1.jsonl"
    SessionRecorder(session_path, lambda: loop.history).attach(bus)

    asyncio.run(loop.run("ping 一下"))

    # 恢复：新 loop 加载历史后与录制时完全一致
    restored = load_history(session_path)
    assert restored == loop.history
    assert restored[0] == Message.system("SYS")
    assert restored[-1].content == "工具说 pong。"

    # 恢复后的 loop 可以继续对话（上下文延续）
    loop2 = AgentLoop(
        llm=MockProvider(scripted=["我记得：工具说了 pong。"]),
        registry=ToolRegistry([PingTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=EventBus(),
    )
    loop2.history = restored
    reply = asyncio.run(loop2.run("刚才工具说了什么？"))
    assert "pong" in reply
    assert loop2.history[1].content == "ping 一下"  # 旧上下文确实在
