"""Anthropic (Claude) provider。

与 OpenAI 的关键差异（全部在本 adapter 内消化，agent 层零感知）：
- system 是独立顶层参数，不是 messages 里的一条
- 消息 content 是 blocks 数组：text / tool_use / tool_result
- 工具调用叫 tool_use（input 为对象），结果以 user 消息里的 tool_result block 回填，
  且多个连续 tool 结果必须合并进同一条 user 消息
- tools schema 用 input_schema 字段
- SSE 事件模型：message_start / content_block_start / content_block_delta /
  content_block_stop / message_delta / message_stop；工具参数经
  input_json_delta.partial_json 分片传输
- stop_reason: end_turn / tool_use / max_tokens
- 必须提供 max_tokens
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

from ...core.errors import ConfigError, LLMHTTPError
from ...core.messages import (
    INVALID_ARGUMENTS_KEY, ROLE_ASSISTANT, ROLE_SYSTEM, ROLE_TOOL, ROLE_USER,
    Message, ToolCall,
)
from ..base import (
    FINISH_LENGTH, FINISH_STOP, FINISH_TOOLS, LLMEvent, ProviderCapabilities,
    StreamFinished, TextDelta, ThinkingDelta, ToolSpec, normalize_usage,
)
from ..transport import (
    Transport, UrllibTransport, aread, asend, parse_json_event, sse_data_events,
)

ANTHROPIC_VERSION = "2023-06-01"

_STOP_REASON_MAP = {
    "end_turn": FINISH_STOP,
    "stop_sequence": FINISH_STOP,
    "tool_use": FINISH_TOOLS,
    "max_tokens": FINISH_LENGTH,
}


# ---------------------------------------------------------------------------
# wire format 互转（仅本 adapter 使用）
# ---------------------------------------------------------------------------

def to_anthropic_request_messages(
    messages: Sequence[Message],
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """规范化历史 → (system 文本, messages blocks)。

    - system 消息抽离合并为顶层 system 参数
    - 连续 tool 结果合并为一条 user 消息里的多个 tool_result block
    """
    system_parts: List[str] = []
    wire: List[Dict[str, Any]] = []
    pending_tool_results: List[Dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            wire.append({"role": ROLE_USER, "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for m in messages:
        if m.role == ROLE_SYSTEM:
            if m.content:
                system_parts.append(m.content)
            continue
        if m.role == ROLE_TOOL:
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.content or "",
                }
            )
            continue
        flush_tool_results()
        if m.role == ROLE_USER:
            wire.append({"role": ROLE_USER, "content": m.content or ""})
        elif m.role == ROLE_ASSISTANT:
            blocks: List[Dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls or []:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                )
            wire.append({"role": ROLE_ASSISTANT, "content": blocks or ""})
    flush_tool_results()

    system = "\n\n".join(system_parts) if system_parts else None
    return system, wire


def tool_specs_to_anthropic(tools: Sequence[ToolSpec]) -> List[Dict[str, Any]]:
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": t["parameters"],
        }
        for t in tools
    ]


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class AnthropicProvider:
    name = "anthropic"
    capabilities = ProviderCapabilities(supports_tools=True, supports_streaming=True)

    DEFAULT_BASE_URL = "https://api.anthropic.com"
    DEFAULT_MODEL = "claude-sonnet-4-5"
    DEFAULT_MAX_TOKENS = 4096

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 120.0,
        max_tokens: Optional[int] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        self.model = model or os.environ.get("KRELCODE_MODEL") or self.DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.base_url = (
            base_url or os.environ.get("ANTHROPIC_BASE_URL") or self.DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens or self.DEFAULT_MAX_TOKENS
        self._transport: Transport = transport or UrllibTransport()

    # -- 请求构造（纯函数，可单测） ---------------------------------------------

    def build_request(
        self, messages: Sequence[Message], tools: Optional[Sequence[ToolSpec]]
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise ConfigError(
                "缺少 API key：请设置 ANTHROPIC_API_KEY（或配置文件；--mock 可离线体验）"
            )
        system, wire_messages = to_anthropic_request_messages(messages)
        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": wire_messages,
            "stream": True,
        }
        if system is not None:
            payload["system"] = system
        if tools:
            payload["tools"] = tool_specs_to_anthropic(tools)
        return {
            "url": f"{self.base_url}/v1/messages",
            "headers": {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
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
                raise LLMHTTPError(resp.status, body, self.name)

            content_parts: List[str] = []
            tool_blocks: Dict[int, Dict[str, str]] = {}  # block index -> {id,name,json}
            stop_reason: Optional[str] = None
            usage: Dict[str, int] = {}

            async for data in sse_data_events(resp):
                event = await parse_json_event(data, self.name)
                etype = event.get("type")

                if etype == "message_start":
                    msg_usage = (event.get("message") or {}).get("usage") or {}
                    usage.update(normalize_usage(msg_usage))
                elif etype == "content_block_start":
                    block = event.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        tool_blocks[event.get("index", 0)] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "json": "",
                        }
                elif etype == "content_block_delta":
                    delta = event.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        text = delta.get("text") or ""
                        if text:
                            content_parts.append(text)
                            yield TextDelta(text)
                    elif dtype == "thinking_delta":
                        thinking = delta.get("thinking") or ""
                        if thinking:
                            yield ThinkingDelta(thinking)
                    elif dtype == "input_json_delta":
                        slot = tool_blocks.get(event.get("index", 0))
                        if slot is not None:
                            slot["json"] += delta.get("partial_json") or ""
                    # signature_delta 等：忽略
                elif etype == "message_delta":
                    delta = event.get("delta") or {}
                    if delta.get("stop_reason"):
                        stop_reason = delta["stop_reason"]
                    if event.get("usage"):
                        merged = dict(usage)
                        merged.update(normalize_usage(event["usage"]))
                        if "input_tokens" in merged and "output_tokens" in merged:
                            merged["total_tokens"] = merged["input_tokens"] + merged["output_tokens"]
                        usage = merged

            tool_calls: Optional[List[ToolCall]] = None
            if tool_blocks:
                tool_calls = []
                for i in sorted(tool_blocks):
                    slot = tool_blocks[i]
                    tool_calls.append(
                        ToolCall(slot["id"], slot["name"], _parse_input(slot["json"]))
                    )
            message = Message.assistant(
                "".join(content_parts) or None, tool_calls=tool_calls
            )
            yield StreamFinished(
                message=message,
                finish_reason=_STOP_REASON_MAP.get(stop_reason or "", stop_reason),
                usage=usage,
            )
        finally:
            resp.close()


def _parse_input(raw: str) -> Dict[str, Any]:
    try:
        args = json.loads(raw or "{}")
        if not isinstance(args, dict):
            raise ValueError("input 必须是 JSON 对象")
        return args
    except (json.JSONDecodeError, ValueError):
        return {INVALID_ARGUMENTS_KEY: raw}
