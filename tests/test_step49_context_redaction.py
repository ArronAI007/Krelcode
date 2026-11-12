"""Step 49 测试：工具结果进入 LLM 历史前的上下文级脱敏。

即使 renderer/recorder 之外，明文密钥也不得进入 loop.history 或回传模型。
"""

import asyncio
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import ToolCallCompleted
from krelcode.core.messages import Message, ToolCall
from krelcode.core.redact import SecretRedactor
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry

SECRET = "sk-test-secret-123"
CTX = ToolContext(workspace_root=Path("."))


class SecretEchoTool:
    name = "echo"
    description = "echo back the message"
    parameters = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }

    async def execute(self, args, ctx):
        return ToolResult(content=f"echo: {args.get('message')}")


def tool_call_msg(call_id="c1"):
    return Message.assistant(tool_calls=[ToolCall(call_id, "echo", {"message": SECRET})])


def make_loop(redactor):
    provider = MockProvider(scripted=[tool_call_msg(), "done"])
    registry = ToolRegistry([SecretEchoTool()])
    loop = AgentLoop(
        llm=provider, registry=registry, tool_context=CTX,
        policy=AllowAllPolicy(), system_prompt="SYS", redactor=redactor,
    )
    return loop, provider


def test_tool_result_redacted_before_history():
    redactor = SecretRedactor([SECRET])
    loop, provider = make_loop(redactor)

    reply = asyncio.run(loop.run("hi"))
    assert reply == "done"

    # 历史里的 tool 结果不含明文，含 ***
    tool_msg = loop.history[3]
    assert tool_msg.role == "tool"
    assert SECRET not in tool_msg.content
    assert "***" in tool_msg.content

    # 回传模型的第二轮调用里同样脱敏
    second_call = provider.calls[1]
    assert SECRET not in second_call[-1].content
    assert "***" in second_call[-1].content


def test_tool_completed_event_redacted():
    from krelcode.core.events import EventBus
    bus = EventBus()
    seen = []
    bus.subscribe(lambda e: seen.append(e))

    redactor = SecretRedactor([SECRET])
    provider = MockProvider(scripted=[tool_call_msg(), "done"])
    registry = ToolRegistry([SecretEchoTool()])
    loop = AgentLoop(
        llm=provider, registry=registry, tool_context=CTX,
        policy=AllowAllPolicy(), system_prompt="SYS", bus=bus, redactor=redactor,
    )
    asyncio.run(loop.run("hi"))

    completed = [e for e in seen if isinstance(e, ToolCallCompleted)][0]
    assert SECRET not in completed.content
    assert "***" in completed.content


def test_no_redactor_leaves_content_untouched():
    # 未注入 redactor 时行为保持原样（向后兼容）
    loop, _ = make_loop(None)
    asyncio.run(loop.run("hi"))
    assert SECRET in loop.history[3].content
