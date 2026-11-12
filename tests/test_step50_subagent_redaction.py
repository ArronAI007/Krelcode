"""Step 50 测试：子代理（SubagentManager spawn）的工具结果上下文级脱敏。

子代理拥有独立 AgentLoop/历史，但其工具结果回传模型前同样要经 SecretRedactor。
"""

import asyncio
from pathlib import Path
from typing import Any, Dict

from krelcode.core.messages import Message, ToolCall
from krelcode.core.redact import SecretRedactor
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.subagent.manager import SubagentManager
from krelcode.tools.base import ToolContext, ToolResult

SECRET = "sk-child-secret-456"
CTX = ToolContext(workspace_root=Path("."))


class LeakTool:
    """返回内容里带明文密钥的假工具。"""
    name = "leak"
    description = "returns a secret"
    parameters: Dict[str, Any] = {
        "type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}

    async def execute(self, args, ctx):
        return ToolResult(content=f"这里泄漏了 {SECRET} 请保护")


def _provider():
    # 第一轮：发起 leak 工具调用；第二轮：收到工具结果后给出最终答复
    return MockProvider(scripted=[
        Message.assistant(tool_calls=[ToolCall("c1", "leak", {"x": "1"})]),
        Message.assistant("done"),
    ])


def test_subagent_tool_result_redacted():
    redactor = SecretRedactor([SECRET])
    mgr = SubagentManager(
        llm=_provider(),
        tool_factory=lambda: [LeakTool()],
        tool_context=CTX,
        policy=AllowAllPolicy(),
        redactor=redactor,
    )

    async def go():
        handle = await mgr.spawn("检查密钥")
        result = await mgr.wait("a1", timeout=5)
        assert result.status == "completed"
        return handle

    handle = asyncio.run(go())

    tool_msgs = [m for m in handle.loop.history if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert SECRET not in tool_msgs[0].content
    assert "***" in tool_msgs[0].content

    # 回传子代理模型的第二轮调用同样脱敏
    second_call = mgr.llm.calls[1]
    assert SECRET not in second_call[-1].content
    assert "***" in second_call[-1].content


def test_subagent_without_redactor_unchanged():
    mgr = SubagentManager(
        llm=_provider(),
        tool_factory=lambda: [LeakTool()],
        tool_context=CTX,
        policy=AllowAllPolicy(),
        # 未注入 redactor：向后兼容，行为不变
    )

    async def go():
        handle = await mgr.spawn("检查密钥")
        await mgr.wait("a1", timeout=5)
        return handle

    handle = asyncio.run(go())
    tool_msg = [m for m in handle.loop.history if m.role == "tool"][0]
    assert SECRET in tool_msg.content
