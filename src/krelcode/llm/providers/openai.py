"""OpenAI 兼容 provider：真 SSE 流式 + tool_call 增量聚合。

wire format 知识（消息结构、tools schema、chunk 语义）全部锁在本文件。
适用于官方 OpenAI 及任意兼容端点（OPENAI_BASE_URL）。
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from ...core.errors import ConfigError, LLMHTTPError
from ...core.context_usage import CacheSegmentSnapshot
from ...core.messages import (
    INVALID_ARGUMENTS_KEY,
    ImageBlock,
    Message,
    ResourceBlock,
    StructuredBlock,
    TextBlock,
    ToolCall,
    ToolResultBlock,
)
from ..base import (
    FINISH_LENGTH, FINISH_STOP, FINISH_TOOLS, EvidenceChannel, LLMEvent,
    PreparedLLMRequest, ProviderCapabilities, StreamFinished, TextDelta, ThinkingDelta,
    ToolCallArgumentsDelta, ToolCallReady, ToolCallStartedDelta, ToolSpec,
    SystemPromptBlocks, cache_segment, normalize_usage, prepare_http_request,
    resolve_evidence_channel, resolve_system_prompt_blocks,
)
from ..cache import (
    CACHE_MODE_IMPLICIT, CacheCapabilities, normalize_cache_preference,
)
from ..transport import (
    Transport, UrllibTransport, aread, asend, parse_json_event, sse_data_events,
)


# ---------------------------------------------------------------------------
# wire format 互转（仅本 adapter 使用）
# ---------------------------------------------------------------------------

def to_openai_messages(
    messages: Sequence[Message],
    *,
    evidence: Optional[EvidenceChannel] = None,
) -> List[Dict[str, Any]]:
    wire = []
    system_blocks = resolve_system_prompt_blocks(messages)
    evidence_channel = resolve_evidence_channel(messages, evidence)
    evidence_anchor = evidence_channel.resolved_anchor_index(messages)
    system_emitted = False
    for index, m in enumerate(messages):
        if m.role == "system" and system_blocks is not None:
            # OpenAI-compatible endpoints receive one deterministic system
            # message. Keep the stable -> dynamic order while Provider
            # metadata retains the original block boundary and hashes.
            if not system_emitted and system_blocks.merged_text:
                wire.append({
                    "role": "system",
                    "content": system_blocks.merged_text,
                })
                system_emitted = True
            continue
        content: Any = m.content
        turn_evidence = (
            evidence_channel.text if index == evidence_anchor else "")
        if m.images and m.role == "user":
            # 多模态：content 转 blocks（text + image_url data URI）
            blocks: List[Dict[str, Any]] = []
            if turn_evidence:
                blocks.append({"type": "text", "text": turn_evidence})
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for img in m.images:
                blocks.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{img.media_type};base64,{img.data_b64}",
                    },
                })
            content = blocks
        elif turn_evidence and m.role == "user":
            # OpenAI-compatible text endpoints stay on the string form. Evidence
            # is a separate request channel but shares the current user turn.
            content = f"{turn_evidence}\n\n{m.content}" if m.content else turn_evidence
        if m.role == "tool" and m.tool_blocks:
            content = [_tool_block(block) for block in m.tool_blocks]
        msg: Dict[str, Any] = {"role": m.role, "content": content}
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


def _tool_block(block: ToolResultBlock) -> Dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageBlock):
        return {
            "type": "text",
            "text": (
                block.alt
                or f"[工具返回图片 {block.media_type}，"
                   f"{len(block.data_b64)} 字符 base64]"
            ),
        }
    if isinstance(block, ResourceBlock):
        detail = block.text or (
            f"[二进制资源 {block.mime_type or 'application/octet-stream'}]"
            if block.blob else "")
        return {
            "type": "text",
            "text": f"{block.name or block.uri}\n{detail}".strip(),
        }
    if isinstance(block, StructuredBlock):
        return {
            "type": "text",
            "text": json.dumps(block.data, ensure_ascii=False, sort_keys=True),
        }
    return {"type": "text", "text": str(block)}


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


def _complete_arguments(raw: str) -> Optional[Dict[str, Any]]:
    """仅在原始参数已是完整 JSON 对象时返回，用于流中提前 Ready。"""
    if not raw.strip():
        return None
    try:
        args = json.loads(raw)
        return args if isinstance(args, dict) else None
    except json.JSONDecodeError:
        return None


_FINISH_MAP = {"stop": FINISH_STOP, "tool_calls": FINISH_TOOLS, "length": FINISH_LENGTH}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class OpenAIProvider:
    name = "openai"
    capabilities = ProviderCapabilities(
        supports_tools=True,
        supports_streaming=True,
        supports_evidence_channel=True,
        cache=CacheCapabilities(
            modes=(CACHE_MODE_IMPLICIT,),
            preferred_mode=CACHE_MODE_IMPLICIT,
            reports_cache_read=True,
        ),
    )

    DEFAULT_BASE_URL = "https://api.openai.com/v1"
    DEFAULT_MODEL = "gpt-4o-mini"
    EXTRA_CONFIG_KEYS = ("prompt_cache",)

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 120.0,
        prompt_cache: object = "auto",
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
        self.cache_preference = normalize_cache_preference(prompt_cache)
        self.cache_negotiation = self.capabilities.cache.negotiate(
            self.cache_preference)
        self.prompt_cache = self.cache_negotiation.enabled
        self._transport: Transport = transport or UrllibTransport()

    # -- 请求构造（纯函数，可单测） ---------------------------------------------

    def build_request(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]],
        *,
        evidence: Optional[EvidenceChannel] = None,
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
            "messages": to_openai_messages(messages, evidence=evidence),
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

    def prepare_request(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
        *,
        evidence: Optional[EvidenceChannel] = None,
    ) -> PreparedLLMRequest:
        request = self.build_request(messages, tools, evidence=evidence)
        payload = json.loads(request["body"].decode("utf-8"))
        blocks = resolve_system_prompt_blocks(messages)
        evidence_channel = resolve_evidence_channel(messages, evidence)
        segments: Sequence[CacheSegmentSnapshot]
        stable_chars: int
        stable_hash: str
        if self.cache_negotiation.enabled:
            segments, stable_chars, stable_hash = _openai_cache_segments(
                payload, blocks=blocks)
        else:
            segments, stable_chars, stable_hash = (), 0, ""
        return prepare_http_request(
            provider=self.name,
            model=self.model,
            request=request,
            output_limit_tokens=int(
                payload.get("max_completion_tokens")
                or payload.get("max_tokens") or 0),
            cache_mode="implicit" if segments else "none",
            cache_segments=segments,
            stable_cache_chars=stable_chars,
            stable_cache_hash=stable_hash,
            system_prompt_blocks=blocks,
            evidence_channel=evidence_channel,
            cache_negotiation=self.cache_negotiation,
        )

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
        *,
        evidence: Optional[EvidenceChannel] = None,
    ) -> AsyncIterator[LLMEvent]:
        prepared = self.prepare_request(messages, tools, evidence=evidence)
        async for event in self.stream_prepared(prepared):
            yield event

    async def stream_prepared(
        self,
        prepared: PreparedLLMRequest,
    ) -> AsyncIterator[LLMEvent]:
        resp = await asend(
            self._transport, "POST", prepared.url, dict(prepared.headers),
            prepared.body, self.timeout,
        )
        try:
            if resp.status != 200:
                body = (await aread(resp)).decode("utf-8", "replace")
                raise LLMHTTPError(resp.status, body, self.name,
                                   headers=dict(getattr(resp, "headers", {}) or {}))

            content_parts: List[str] = []
            tool_acc: Dict[int, Dict[str, str]] = {}  # index -> {id,name,args}
            started: set[int] = set()
            ready: set[int] = set()
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
                        index = int(tc.get("index", 0))
                        slot = tool_acc.setdefault(
                            index, {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if index not in started and slot["name"]:
                            started.add(index)
                            yield ToolCallStartedDelta(
                                index=index,
                                id=slot["id"],
                                name=slot["name"],
                            )
                        partial_json = fn.get("arguments") or ""
                        if partial_json:
                            slot["args"] += partial_json
                            yield ToolCallArgumentsDelta(
                                index=index, partial_json=partial_json)
                            args = _complete_arguments(slot["args"])
                            if (
                                index not in ready
                                and args is not None
                                and slot["name"]
                            ):
                                ready.add(index)
                                yield ToolCallReady(
                                    index=index,
                                    tool_call=ToolCall(
                                        id=slot["id"],
                                        name=slot["name"],
                                        arguments=args,
                                    ),
                                )
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

            tool_calls: Optional[List[ToolCall]] = None
            if tool_acc:
                tool_calls = []
                for index in sorted(tool_acc):
                    slot = tool_acc[index]
                    if index not in started:
                        started.add(index)
                        yield ToolCallStartedDelta(
                            index=index, id=slot["id"], name=slot["name"])
                    tool_call = ToolCall(
                        id=slot["id"],
                        name=slot["name"],
                        arguments=_parse_arguments(slot["args"]),
                    )
                    tool_calls.append(tool_call)
                    if index not in ready:
                        ready.add(index)
                        yield ToolCallReady(index=index, tool_call=tool_call)
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



def _openai_cache_segments(
    payload: Dict[str, Any],
    *,
    blocks: Optional[SystemPromptBlocks] = None,
) -> tuple[list[CacheSegmentSnapshot], int, str]:
    """Describe OpenAI/DeepSeek implicit prefix-cache opportunities safely."""
    segments = []
    stable = blocks.stable_text if blocks is not None else ""
    stable_chars = 0
    stable_hash = ""
    if stable:
        segment = cache_segment("system", stable, cache_control=False)
        segments.append(segment)
        stable_chars = segment.chars
        stable_hash = segment.digest
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        segments.append(cache_segment(
            "tools", json.dumps(tools, ensure_ascii=False), cache_control=False))
    wire_messages = payload.get("messages")
    if isinstance(wire_messages, list) and wire_messages:
        segments.append(cache_segment(
            "messages", json.dumps(wire_messages, ensure_ascii=False),
            cache_control=False))
    return segments, stable_chars, stable_hash
