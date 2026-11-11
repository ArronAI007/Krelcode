"""Google Gemini provider（v1beta generateContent，SSE 流式）。

与 OpenAI/Anthropic 的关键差异（全部在本 adapter 内消化）：
- 消息是 contents[].parts[]（text / functionCall / functionResponse）
- system 是独立的 systemInstruction 字段
- **functionCall 没有 id**：本 adapter 生成合成 id（gem-N）；回填结果时
  functionResponse 按 name 配对（转换历史时用 assistant 消息重建 id→name 映射）
- tools 用 functionDeclarations 包装
- finishReason: STOP / MAX_TOKENS / SAFETY…（有 functionCall 时也是 STOP，
  规范化时按「有调用即 tool_calls」处理）
- usage: usageMetadata.promptTokenCount / candidatesTokenCount / totalTokenCount
- 鉴权: x-goog-api-key 头；流式端点 :streamGenerateContent?alt=sse
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
    StreamFinished, TextDelta, ToolSpec, normalize_usage,
)
from ..transport import (
    Transport, UrllibTransport, aread, asend, parse_json_event, sse_data_events,
)

GEMINI_ROLE_MODEL = "model"


# ---------------------------------------------------------------------------
# wire format 互转（仅本 adapter 使用）
# ---------------------------------------------------------------------------

def to_gemini_contents(
    messages: Sequence[Message],
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """规范化历史 → (systemInstruction, contents)。

    - system 消息合并进 systemInstruction
    - 连续 tool 结果合并进同一条 user 消息的多个 functionResponse part
    - functionResponse 的 name 由 assistant.tool_calls 建立的 id→name 映射还原
    """
    system_parts: List[str] = []
    contents: List[Dict[str, Any]] = []
    id_to_name: Dict[str, str] = {}
    pending: List[Dict[str, Any]] = []

    def flush_pending() -> None:
        if pending:
            contents.append({"role": ROLE_USER, "parts": list(pending)})
            pending.clear()

    for m in messages:
        if m.role == ROLE_SYSTEM:
            if m.content:
                system_parts.append(m.content)
            continue
        if m.role == ROLE_TOOL:
            pending.append({
                "functionResponse": {
                    "name": id_to_name.get(m.tool_call_id or "", "unknown_function"),
                    "response": {"content": m.content or ""},
                }
            })
            continue
        flush_pending()
        if m.role == ROLE_USER:
            parts_u: List[Dict[str, Any]] = [{"text": m.content or ""}]
            for img in m.images or []:
                parts_u.append({"inline_data": {
                    "mime_type": img.media_type,
                    "data": img.data_b64,
                }})
            contents.append({"role": ROLE_USER, "parts": parts_u})
        elif m.role == ROLE_ASSISTANT:
            parts: List[Dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            for tc in m.tool_calls or []:
                id_to_name[tc.id] = tc.name
                parts.append({"functionCall": {"name": tc.name, "args": tc.arguments}})
            contents.append({
                "role": GEMINI_ROLE_MODEL,
                "parts": parts or [{"text": ""}],
            })
    flush_pending()

    system_instruction = (
        {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts else None
    )
    return system_instruction, contents


def tool_specs_to_gemini(tools: Sequence[ToolSpec]) -> List[Dict[str, Any]]:
    return [{
        "functionDeclarations": [
            {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            }
            for t in tools
        ],
    }]


_FINISH_MAP = {"STOP": FINISH_STOP, "MAX_TOKENS": FINISH_LENGTH}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class GeminiProvider:
    name = "gemini"
    capabilities = ProviderCapabilities(supports_tools=True, supports_streaming=True)

    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
    DEFAULT_MODEL = "gemini-2.5-flash"

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
        self.api_key = (api_key or os.environ.get("GEMINI_API_KEY")
                        or os.environ.get("GOOGLE_API_KEY"))
        self.generation: Dict[str, Any] = dict(generation or {})
        self.base_url = (
            base_url or os.environ.get("GEMINI_BASE_URL") or self.DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = timeout
        self._transport: Transport = transport or UrllibTransport()

    # -- 请求构造（纯函数，可单测） ------------------------------------------------------

    def build_request(
        self, messages: Sequence[Message], tools: Optional[Sequence[ToolSpec]]
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise ConfigError(
                "缺少 API key：请设置 GEMINI_API_KEY 或 GOOGLE_API_KEY"
                "（或配置文件；--mock 可离线体验）"
            )
        system_instruction, contents = to_gemini_contents(messages)
        payload: Dict[str, Any] = {"contents": contents}
        if self.generation:
            # Gemini 的生成参数在独立 generationConfig 段（temperature/topP/…）
            payload["generationConfig"] = dict(self.generation)
        if system_instruction is not None:
            payload["systemInstruction"] = system_instruction
        if tools:
            payload["tools"] = tool_specs_to_gemini(tools)
        return {
            "url": (f"{self.base_url}/v1beta/models/{self.model}"
                    ":streamGenerateContent?alt=sse"),
            "headers": {
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            },
            "body": json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        }

    # -- LLMProvider 端口实现 -------------------------------------------------------------

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
            tool_calls: List[ToolCall] = []
            finish_reason: Optional[str] = None
            usage: Dict[str, int] = {}
            call_counter = 0

            async for data in sse_data_events(resp):
                chunk = await parse_json_event(data, self.name)
                meta = chunk.get("usageMetadata")
                if meta:
                    usage = normalize_usage({
                        "input_tokens": meta.get("promptTokenCount", 0),
                        "output_tokens": meta.get("candidatesTokenCount", 0),
                        "total_tokens": meta.get("totalTokenCount", 0),
                    })
                for candidate in chunk.get("candidates") or []:
                    content = candidate.get("content") or {}
                    for part in content.get("parts") or []:
                        text = part.get("text")
                        if text:
                            content_parts.append(text)
                            yield TextDelta(text)
                        fn_call = part.get("functionCall")
                        if fn_call:
                            call_counter += 1
                            args = fn_call.get("args")
                            if not isinstance(args, dict):
                                args = {INVALID_ARGUMENTS_KEY: json.dumps(args)}
                            tool_calls.append(ToolCall(
                                id=f"gem-{call_counter}",
                                name=str(fn_call.get("name", "")),
                                arguments=args,
                            ))
                    if candidate.get("finishReason"):
                        finish_reason = candidate["finishReason"]

            if tool_calls:
                normalized = FINISH_TOOLS       # Gemini 带调用时 finishReason 也是 STOP
            else:
                normalized = _FINISH_MAP.get(finish_reason or "", 
                                             (finish_reason or "").lower() or None)
            message = Message.assistant(
                "".join(content_parts) or None,
                tool_calls=tool_calls or None,
            )
            yield StreamFinished(message=message, finish_reason=normalized, usage=usage)
        finally:
            resp.close()
