"""agent 层测试：注入 MockProvider + 假工具/假策略，纯内存验证主循环语义。

覆盖：无工具对话、完整 tool-call 循环、未知工具、工具抛异常、非法参数、
策略拒绝、max_steps 守卫、事件序列、历史结构。
"""

import asyncio
from pathlib import Path
from typing import List

from krelcode.agent.loop import AgentLoop, max_steps_reached_text
from krelcode.core.events import (
    AgentEvent, LLMResponseReceived, PermissionDenied, RunFinished,
    RunStarted, StepStarted, ToolCallCompleted, ToolCallStarted,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy, Decision, deny
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry

CTX = ToolContext(workspace_root=Path("."))


class EchoTool:
    """测试用假工具：把 message 参数原样返回；可配置为抛异常。"""
    name = "echo"
    description = "echo back the message"
    parameters = {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]}
    raise_on_execute = False

    async def execute(self, args, ctx):
        if self.raise_on_execute:
            raise RuntimeError("boom")
        return ToolResult(content=f"echo: {args.get('message')}")


class DenyAllPolicy:
    async def check(self, tool_call, ctx):
        return deny("测试策略：一律拒绝")


class BoomPolicy:
    async def check(self, tool_call, ctx):
        raise RuntimeError("policy 自身故障")


class RecordingBus:
    """包装 EventBus：把事件类型名按顺序录下来。"""
    def __init__(self):
        from krelcode.core.events import EventBus
        self.bus = EventBus()
        self.events: List[AgentEvent] = []
        self.bus.subscribe(lambda e: self.events.append(e))

    def types(self):
        return [type(e).__name__ for e in self.events]


def make_loop(scripted, tools=None, policy=None, max_steps=20, bus=None):
    provider = MockProvider(scripted=scripted)
    registry = ToolRegistry(tools or [])
    rec = bus or RecordingBus()
    loop = AgentLoop(
        llm=provider, registry=registry, tool_context=CTX,
        policy=policy or AllowAllPolicy(), bus=rec.bus,
        system_prompt="SYS", max_steps=max_steps,
    )
    return loop, provider, rec


def tool_call_msg(name="echo", args=None, call_id="c1"):
    return Message.assistant(tool_calls=[ToolCall(call_id, name, args or {"message": "hi"})])


# -- 场景 ----------------------------------------------------------------------

def test_plain_chat_no_tools():
    loop, provider, rec = make_loop(["hello!"])
    # 无工具时 provider 收到 tools=None
    reply = asyncio.run(loop.run("hi"))
    assert reply == "hello!"
    assert [m.role for m in loop.history] == ["system", "user", "assistant"]
    assert rec.types() == ["RunStarted", "StepStarted", "LLMStreamDelta", "LLMResponseReceived", "RunFinished"]
    assert provider.tools_seen == [None]


def test_full_tool_call_cycle():
    loop, provider, rec = make_loop([tool_call_msg(), "done: 工具结果已消化"])
    tool = EchoTool()
    loop.registry.register(tool)

    reply = asyncio.run(loop.run("用 echo 工具"))

    assert reply == "done: 工具结果已消化"
    # 历史结构：system, user, assistant(tool_calls), tool(result), assistant(final)
    roles = [m.role for m in loop.history]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert loop.history[3].content == "echo: hi"
    assert loop.history[3].tool_call_id == "c1"
    # 第二次 LLM 调用应看到工具结果
    second_call = provider.calls[1]
    assert second_call[-1].role == "tool" and second_call[-1].content == "echo: hi"
    # provider 收到的是统一 ToolSpec（非 OpenAI 包装格式）
    assert provider.tools_seen[0] == [
        {"name": "echo", "description": EchoTool.description, "parameters": EchoTool.parameters}
    ]
    # 事件序列完整
    # 第一轮 assistant 只有 tool_calls（无文本 delta），第二轮有文本 delta
    assert rec.types() == [
        "RunStarted", "StepStarted", "LLMResponseReceived",
        "ToolCallStarted", "ToolCallCompleted",
        "StepStarted", "LLMStreamDelta", "LLMResponseReceived", "RunFinished",
    ]
    completed = [e for e in rec.events if isinstance(e, ToolCallCompleted)][0]
    assert not completed.is_error


def test_unknown_tool_fed_back_as_error():
    loop, _, _ = make_loop([tool_call_msg(name="no_such_tool"), "ok，我换个办法"])
    reply = asyncio.run(loop.run("x"))
    assert reply == "ok，我换个办法"
    tool_msg = loop.history[3]
    assert tool_msg.role == "tool" and "未知工具" in tool_msg.content


def test_tool_exception_recovered():
    tool = EchoTool()
    tool.raise_on_execute = True
    loop, _, rec = make_loop([tool_call_msg(), "抱歉，工具失败了"], tools=[tool])
    reply = asyncio.run(loop.run("x"))
    assert reply == "抱歉，工具失败了"
    assert "工具执行出错" in loop.history[3].content
    assert "boom" in loop.history[3].content


def test_invalid_json_arguments_fed_back():
    raw = Message.assistant(tool_calls=[ToolCall("c1", "echo", {"__invalid_arguments__": "{oops"})])
    loop, _, _ = make_loop([raw, "参数错了，重来"], tools=[EchoTool()])
    asyncio.run(loop.run("x"))
    assert "不是合法 JSON" in loop.history[3].content


def test_policy_denies_tool():
    loop, _, rec = make_loop([tool_call_msg(), "被拒绝了，无法继续"],
                             tools=[EchoTool()], policy=DenyAllPolicy())
    reply = asyncio.run(loop.run("x"))
    assert reply == "被拒绝了，无法继续"
    assert "拒绝" in loop.history[3].content
    assert "PermissionDenied" in rec.types()


def test_policy_crash_is_conservative_deny():
    loop, _, _ = make_loop([tool_call_msg(), "ok"], tools=[EchoTool()], policy=BoomPolicy())
    asyncio.run(loop.run("x"))
    assert "审批检查失败" in loop.history[3].content


def test_max_steps_guard():
    # 脚本让 LLM 永远请求工具 → 应触发 max_steps 中止
    loop, _, _ = make_loop([tool_call_msg(), tool_call_msg(), tool_call_msg()],
                           tools=[EchoTool()], max_steps=2)
    reply = asyncio.run(loop.run("x"))
    assert reply == max_steps_reached_text(2)
    assert "2 个模型步骤" in reply
    assert "--max-steps 4" in reply
    assert loop.history[-1].role == "assistant"


def test_reset_keeps_system_prompt():
    loop, _, _ = make_loop(["a"])
    asyncio.run(loop.run("q"))
    loop.reset()
    assert len(loop.history) == 1 and loop.history[0].role == "system"
