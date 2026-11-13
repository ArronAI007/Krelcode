"""MockProvider：脚本化响应，用于测试与离线体验（--mock）。

脚本项可以是：
- str            → assistant 纯文本回复（按 chunk_size 切片模拟流式）
- Message        → 原样返回（可携带 tool_calls，驱动完整工具循环）
脚本耗尽后返回 fallback。每次调用的 messages 记录在 self.calls，
tools 参数记录在 self.tools_seen，便于断言。
"""

from __future__ import annotations

from typing import AsyncIterator, List, Optional, Sequence, Union

from ...core.messages import Message
from ..base import (
    FINISH_STOP, LLMEvent, ProviderCapabilities, StreamFinished, TextDelta, ToolSpec,
)

ScriptItem = Union[str, Message]


class MockProvider:
    name = "mock"
    capabilities = ProviderCapabilities(supports_tools=True, supports_streaming=True)
    # mock 不接收 model/api_key/base_url/timeout/generation 等标准 wire 配置
    STANDARD_CONFIG = False

    def __init__(
        self,
        scripted: Optional[Sequence[ScriptItem]] = None,
        fallback: str = "[mock] 收到",
        chunk_size: int = 0,
        usage: Optional[dict] = None,
    ) -> None:
        self._scripted: List[ScriptItem] = list(scripted or [])
        self._fallback = fallback
        self._chunk_size = chunk_size  # >0 时把文本切片，模拟逐 token 流式
        self._usage = dict(usage or {})
        self.calls: List[List[Message]] = []
        self.tools_seen: List[Optional[List[ToolSpec]]] = []
        self.model = "mock-model"

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
    ) -> AsyncIterator[LLMEvent]:
        self.calls.append(
            [Message(m.role, m.content, m.tool_calls, m.tool_call_id) for m in messages]
        )
        self.tools_seen.append(list(tools) if tools else None)
        item: ScriptItem = self._scripted.pop(0) if self._scripted else self._fallback
        message = Message.assistant(item) if isinstance(item, str) else item
        if message.content:
            text = message.content
            if self._chunk_size > 0:
                for i in range(0, len(text), self._chunk_size):
                    yield TextDelta(text[i:i + self._chunk_size])
            else:
                yield TextDelta(text)
        yield StreamFinished(message=message, finish_reason=FINISH_STOP, usage=dict(self._usage))
