"""Step 46 测试：输出/历史脱敏（SecretRedactor + renderer/recorder 接线）。"""

import asyncio
import json

from krelcode.core.events import (
    EventBus, LLMStreamDelta, LLMThinkingDelta, RunStarted, ToolCallCompleted,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.core.redact import SecretRedactor
from krelcode.session.recorder import SessionRecorder
from krelcode.ui.render import StreamRenderer


class Buffer:
    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)

    @property
    def text(self):
        return "".join(self.parts)


# -- SecretRedactor 单元 -------------------------------------------------------------------

def test_secret_redactor_basic():
    r = SecretRedactor()
    r.add("sk-abcdef")
    r.add("")            # 空值忽略
    r.add("short")       # 短于 MIN_SECRET_LEN 忽略
    r.add("sk-abcdef")   # 去重
    assert r.secrets == ("sk-abcdef",)
    assert r.redact("key=sk-abcdef end") == "key=*** end"
    assert r.redact("no secret here") == "no secret here"
    assert r.redact("") == ""
    assert r.redact(None) == ""


def test_secret_redactor_multiple():
    r = SecretRedactor(["sk-aaa-111", "sk-bbb-222"])
    assert r.redact("a=sk-aaa-111 b=sk-bbb-222") == "a=*** b=***"


# -- 渲染器脱敏 ---------------------------------------------------------------------------

def test_renderer_redacts_secret_in_tool_result():
    redactor = SecretRedactor(["sk-secret-123"])
    buf = Buffer()
    renderer = StreamRenderer(write=buf.write, color=False, redactor=redactor)
    tc = ToolCall("c1", "get_time", {"tz": "UTC"})
    renderer(ToolCallCompleted(tc, "result sk-secret-123 here", False))
    assert "sk-secret-123" not in buf.text
    assert "输出已折叠" in buf.text
    assert renderer.expand_tool_output("1")
    assert "sk-secret-123" not in buf.text
    assert "***" in buf.text


def test_renderer_redacts_secret_in_expanded_thinking():
    redactor = SecretRedactor(["sk-secret-123"])
    buf = Buffer()
    renderer = StreamRenderer(write=buf.write, color=False, redactor=redactor)
    renderer(RunStarted("q"))
    renderer(LLMThinkingDelta("# 检查\n\n发现 sk-secret-123"))
    renderer(LLMStreamDelta("完成"))
    renderer.expand_thinking()
    assert "sk-secret-123" not in buf.text
    assert "发现 ***" in buf.text


# -- 会话落盘脱敏 -------------------------------------------------------------------------

def test_recorder_redacts_secret_in_tool_result(tmp_path):
    path = tmp_path / "s.jsonl"
    redactor = SecretRedactor(["sk-secret-123"])
    recorder = SessionRecorder(path, lambda: [Message.user("u")], redactor=redactor)
    bus = EventBus()
    recorder.attach(bus)

    tc = ToolCall("c1", "get_time", {"tz": "UTC"})
    async def go():
        await bus.emit(ToolCallCompleted(tc, "sk-secret-123 leaked", False))
    asyncio.run(go())

    raw = path.read_text(encoding="utf-8")
    assert "sk-secret-123" not in raw
    assert "***" in raw
    # 仍可正常解析为 JSON
    [json.loads(l) for l in raw.splitlines()]
