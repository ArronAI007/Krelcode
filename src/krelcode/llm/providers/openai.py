"""OpenAI 兼容 provider：真 SSE 流式 + tool_call 增量聚合。

wire format 知识（消息结构、tools schema、chunk 语义）全部锁在本文件。
适用于官方 OpenAI 及任意兼容端点（OPENAI_BASE_URL）。
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from ...core.errors import ConfigError, LLMHTTPError
from ...core.messages import (
    INVALID_ARGUMENTS_KEY, Message, ToolCall,
)
from ..base import (
    FINISH_LENGTH, FINISH_STOP, FINISH_TOOLS, LLMEvent, ProviderCapabilities,
    StreamFinished, TextDelta, ThinkingDelta, ToolSpec, normalize_usage,
)
from ..transport import (
    Transport, UrllibTransport, aread, asend, parse_json_event, sse_data_events,
)


# ---------------------------------------------------------------------------
# wire format 互转（仅本 adapter 使用）
# ---------------------------------------------------------------------------

def to_openai_messages(messages: Sequence[Message]) -> List[Dict[str, Any]]:
    wire = []
    for m in messages:
        msg: Dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in m.tool_calls
            ]
        if m.tool_call_id is not None:
            msg["tool_call_id"] = m.tool_call_id
        wire.append(msg)
    return wire


def tool_specs_to_openai(tools: Sequence[ToolSpec]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in tools
    ]


def _parse_arguments(raw: str) -> Dict[str, Any]:
    try:
        args = json.loads(raw or "{}")
        if not isinstance(args, dict):
            raise ValueError("arguments 必须是 JSON 对象")
        return args
    except (json.JSONDecodeError, ValueError):
        return {INVALID_ARGUMENTS_KEY: raw}


_FINISH_MAP = {"stop": FINISH_STOP, "tool_calls": FINISH_TOOLS, "length": FINISH_LENGTH}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class OpenAIProvider:
    name = "openai"
    capabilities = ProviderCapabilities(supports_tools=True, supports_streaming=True)

    DEFAULT_BASE_URL = "https://api.openai.com/v1"
    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 120.0,
        generation: Optional[Dict[str, Any]] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        self.model = model or os.environ.get("KRELCODE_MODEL") or self.DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.generation: Dict[str, Any] = dict(generation or {})
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL") or self.DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = timeout
        self._transport: Transport = transport or UrllibTransport()

    # -- 请求构造（纯函数，可单测） ---------------------------------------------

    def build_request(
        self, messages: Sequence[Message], tools: Optional[Sequence[ToolSpec]]
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise ConfigError(
                "缺少 API key：请设置 OPENAI_API_KEY（或配置文件；--mock 可离线体验）"
            )
        # generation 参数（temperature/top_p/…）来自用户配置；
        # 显式键（model/messages/stream/tools）在其后设置，不可被覆盖
        payload: Dict[str, Any] = {
            **self.generation,
            "model": self.model,
            "messages": to_openai_messages(messages),
            "stream": True,
        }
        if tools:
            payload["tools"] = tool_specs_to_openai(tools)
        return {
            "url": f"{self.base_url}/chat/completions",
            "headers": {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            "body": json.dumps(payload).encode("utf-8"),
        }

    # -- LLMProvider 端口实现 ---------------------------------------------------

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
    ) -> AsyncIterator[LLMEvent]:
        req = self.build_request(messages, tools)
        resp = await asend(
            self._transport, "POST", req["url"], req["headers"], req["body"], self.timeout
        )
        try:
            if resp.status != 200:
                body = (await aread(resp)).decode("utf-8", "replace")
                raise LLMHTTPError(resp.status, body, self.name,
                                   headers=dict(getattr(resp, "headers", {}) or {}))

            content_parts: List[str] = []
            tool_acc: Dict[int, Dict[str, str]] = {}  # index -> {id,name,args}
            finish_reason: Optional[str] = None
            usage: Dict[str, int] = {}

            async for data in sse_data_events(resp):
                chunk = await parse_json_event(data, self.name)
                if chunk.get("usage"):
                    usage = normalize_usage(chunk["usage"])
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    # reasoning 模型的思考流（DeepSeek/Qwen: reasoning_content，
                    # OpenRouter 等: reasoning）——只展示，不进 content
                    thinking = delta.get("reasoning_content") or delta.get("reasoning")
                    if thinking:
                        yield ThinkingDelta(thinking)
                    text = delta.get("content")
                    if text:
                        content_parts.append(text)
                        yield TextDelta(text)
                    for tc in delta.get("tool_calls") or []:
                        slot = tool_acc.setdefault(
                            tc.get("index", 0), {"id": "", "name": "", "args": ""}
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        slot["args"] += fn.get("arguments") or ""
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

            tool_calls: Optional[List[ToolCall]] = None
            if tool_acc:
                tool_calls = [
                    ToolCall(
                        id=tool_acc[i]["id"],
                        name=tool_acc[i]["name"],
                        arguments=_parse_arguments(tool_acc[i]["args"]),
                    )
                    for i in sorted(tool_acc)
                ]
            message = Message.assistant(
                "".join(content_parts) or None, tool_calls=tool_calls
            )
            yield StreamFinished(
                message=message,
                finish_reason=_FINISH_MAP.get(finish_reason or "", finish_reason),
                usage=usage,
            )
        finally:
            resp.close()
