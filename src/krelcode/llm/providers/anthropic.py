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

import hashlib
import json
import os
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Tuple

from ...core.context_usage import CacheSegmentSnapshot
from ...core.errors import ConfigError, LLMHTTPError
from ...core.tokens import estimate_text_tokens
from ...core.messages import (
    INVALID_ARGUMENTS_KEY,
    ImageBlock,
    Message,
    ResourceBlock,
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_TOOL,
    ROLE_USER,
    StructuredBlock,
    TextBlock,
    ToolCall,
)
from ..base import (
    FINISH_LENGTH, FINISH_STOP, FINISH_TOOLS, EvidenceChannel, LLMEvent,
    PreparedLLMRequest, ProviderCapabilities, StreamFinished, TextDelta, ThinkingDelta,
    ToolCallArgumentsDelta, ToolCallReady, ToolCallStartedDelta, ToolSpec,
    SystemPromptBlocks, normalize_usage, prepare_http_request,
    resolve_evidence_channel, resolve_system_prompt_blocks,
)
from ..cache import (
    CACHE_MODE_EXPLICIT, CacheCapabilities,
    normalize_cache_preference,
)
from ..cache_planner import (
    CACHE_BREAKPOINT_KIND_MESSAGES,
    CACHE_BREAKPOINT_KIND_SYSTEM,
    CACHE_BREAKPOINT_KIND_TOOLS,
    CACHE_BREAKPOINT_STABILITY_SESSION,
    CACHE_BREAKPOINT_STABILITY_STATIC,
    CACHE_BREAKPOINT_STABILITY_TURN,
    CacheBreakpointCandidate,
    CacheBreakpointPlan,
    DEFAULT_CACHE_BREAKPOINT_PLANNER,
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

def _to_anthropic_request_messages(
    messages: Sequence[Message],
    *,
    cache_all_without_boundary: bool = False,
    evidence: Optional[EvidenceChannel] = None,
) -> Tuple[Optional[SystemPromptBlocks], List[Dict[str, Any]]]:
    """规范化历史 → (显式 system blocks, messages blocks)。"""
    system_blocks = resolve_system_prompt_blocks(
        messages,
        cache_all_without_boundary=cache_all_without_boundary,
    )
    evidence_channel = resolve_evidence_channel(messages, evidence)
    evidence_anchor = evidence_channel.resolved_anchor_index(messages)
    wire: List[Dict[str, Any]] = []
    pending_tool_results: List[Dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            wire.append({"role": ROLE_USER, "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for index, m in enumerate(messages):
        if m.role == ROLE_SYSTEM:
            continue
        if m.role == ROLE_TOOL:
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": (
                        [_tool_result_block(block) for block in m.tool_blocks]
                        if m.tool_blocks else m.content or ""
                    ),
                }
            )
            continue
        flush_tool_results()
        if m.role == ROLE_USER:
            turn_evidence = (
                evidence_channel.blocks if index == evidence_anchor else ())
            if m.images or turn_evidence:
                blocks_u: List[Dict[str, Any]] = []
                for block in turn_evidence:
                    blocks_u.append({"type": "text", "text": block.text})
                if m.content:
                    blocks_u.append({"type": "text", "text": m.content})
                for img in m.images or []:
                    blocks_u.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img.media_type,
                            "data": img.data_b64,
                        },
                    })
                wire.append({"role": ROLE_USER, "content": blocks_u})
            else:
                wire.append({"role": ROLE_USER, "content": m.content or ""})
        elif m.role == ROLE_ASSISTANT:
            blocks: List[Dict[str, Any]] = []
            # 扩展思考回传：thinking/redacted_thinking 块必须位于 content 最前
            # （工具调用续轮时 API 强制要求；provider_meta 仅本 adapter 读写）
            meta = m.provider_meta or {}
            for block in meta.get("anthropic_thinking") or []:
                if isinstance(block, dict):
                    blocks.append(block)
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls or []:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                )
            wire.append({"role": ROLE_ASSISTANT, "content": blocks or ""})
    flush_tool_results()
    return system_blocks, wire


def to_anthropic_request_messages(
    messages: Sequence[Message],
    *,
    evidence: Optional[EvidenceChannel] = None,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """兼容接口：返回合并 system 文本与 messages blocks。

    Provider 的实际请求构造使用 ``_to_anthropic_request_messages``，以保留显式
    system block 数组和 stable/dynamic 边界。
    """
    system_blocks, wire = _to_anthropic_request_messages(
        messages, evidence=evidence)
    system = system_blocks.merged_text if system_blocks is not None else None
    return system, wire


def _anthropic_system_blocks(blocks: SystemPromptBlocks) -> List[Dict[str, Any]]:
    """Preserve the explicit block array; planning adds cache markers later."""
    wire: List[Dict[str, Any]] = []
    for block in blocks.stable_system_blocks:
        if block.text:
            wire.append({"type": "text", "text": block.text})
    for block in blocks.dynamic_system_blocks:
        if block.text:
            wire.append({"type": "text", "text": block.text})
    return wire


def minimum_cacheable_tokens_for_model(model: str) -> int:
    """Return the documented Anthropic minimum cacheable prefix for a model."""
    normalized = str(model or "").strip().lower()
    if "haiku" in normalized:
        return 2048
    return 1024


def _wire_text(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _cache_breakpoint_candidates(
    payload: Dict[str, Any],
    *,
    stable_system_block_count: int,
) -> Tuple[CacheBreakpointCandidate, ...]:
    """Translate one final Anthropic wire request into ordered prefix candidates."""
    candidates: List[CacheBreakpointCandidate] = []
    cumulative_tokens = 0
    order = 0

    def append_candidate(
        *,
        candidate_id: str,
        kind: str,
        item_index: int,
        block_index: int,
        text: str,
        stability: str,
        anchor: bool,
        moving: bool,
    ) -> None:
        nonlocal cumulative_tokens, order
        tokens = estimate_text_tokens(text) if text else 0
        cumulative_tokens += tokens
        candidates.append(CacheBreakpointCandidate(
            id=candidate_id,
            kind=kind,
            order=order,
            item_index=item_index,
            block_index=block_index,
            cumulative_tokens=cumulative_tokens,
            incremental_tokens=tokens,
            chars=len(text),
            digest=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
            stability=stability,
            anchor=anchor,
            moving=moving,
        ))
        order += 1

    def accumulate_only(text: str) -> None:
        nonlocal cumulative_tokens
        cumulative_tokens += estimate_text_tokens(text) if text else 0

    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        append_candidate(
            candidate_id=f"tools:{len(tools) - 1}",
            kind=CACHE_BREAKPOINT_KIND_TOOLS,
            item_index=len(tools) - 1,
            block_index=-1,
            text=_wire_text(tools),
            stability=CACHE_BREAKPOINT_STABILITY_STATIC,
            anchor=True,
            moving=False,
        )

    system = payload.get("system")
    if isinstance(system, list):
        stable_limit = max(0, min(int(stable_system_block_count), len(system)))
        for index, block in enumerate(system[:stable_limit]):
            append_candidate(
                candidate_id=f"system:{index}",
                kind=CACHE_BREAKPOINT_KIND_SYSTEM,
                item_index=index,
                block_index=index,
                text=_wire_text(block),
                stability=CACHE_BREAKPOINT_STABILITY_STATIC,
                anchor=True,
                moving=False,
            )
        # Dynamic system blocks are never cache anchors, but they still sit in
        # the final wire prefix before messages. Include their tokens when
        # calculating message checkpoint positions and the cache threshold.
        for block in system[stable_limit:]:
            accumulate_only(_wire_text(block))

    messages = payload.get("messages")
    if isinstance(messages, list):
        last_index = len(messages) - 1
        for index, message in enumerate(messages):
            append_candidate(
                candidate_id=f"messages:{index}",
                kind=CACHE_BREAKPOINT_KIND_MESSAGES,
                item_index=index,
                block_index=-1,
                text=_wire_text(message),
                stability=(
                    CACHE_BREAKPOINT_STABILITY_TURN
                    if index == last_index
                    else CACHE_BREAKPOINT_STABILITY_SESSION
                ),
                anchor=False,
                moving=True,
            )
    return tuple(candidates)


def _apply_cache_breakpoints(
    payload: Dict[str, Any],
    plan: CacheBreakpointPlan,
) -> None:
    tools = payload.get("tools")
    system = payload.get("system")
    messages = payload.get("messages")
    for breakpoint in plan.breakpoints:
        if breakpoint.kind == CACHE_BREAKPOINT_KIND_TOOLS:
            if isinstance(tools, list) and 0 <= breakpoint.item_index < len(tools):
                target = tools[breakpoint.item_index]
                if isinstance(target, dict):
                    target["cache_control"] = {"type": "ephemeral"}
        elif breakpoint.kind == CACHE_BREAKPOINT_KIND_SYSTEM:
            if isinstance(system, list) and 0 <= breakpoint.item_index < len(system):
                target = system[breakpoint.item_index]
                if isinstance(target, dict):
                    target["cache_control"] = {"type": "ephemeral"}
        elif breakpoint.kind == CACHE_BREAKPOINT_KIND_MESSAGES:
            if (
                isinstance(messages, list)
                and 0 <= breakpoint.item_index < len(messages)
                and isinstance(messages[breakpoint.item_index], dict)
            ):
                _mark_last_block_cacheable(messages[breakpoint.item_index])


def _tool_result_block(block: object) -> Dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ImageBlock):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": block.media_type,
                "data": block.data_b64,
            },
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
    capabilities = ProviderCapabilities(
        supports_tools=True,
        supports_streaming=True,
        supports_evidence_channel=True,
        cache=CacheCapabilities(
            modes=(CACHE_MODE_EXPLICIT,),
            preferred_mode=CACHE_MODE_EXPLICIT,
            max_breakpoints=4,
            reports_cache_read=True,
            reports_cache_creation=True,
            supports_ttl=True,
        ),
    )
    # TOML [providers.anthropic] 中除标准字段外、构造函数额外接受的键
    EXTRA_CONFIG_KEYS = (
        "max_tokens", "prompt_cache", "cache_min_tokens", "thinking_budget")

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
        prompt_cache: object = True,
        cache_min_tokens: Optional[int] = None,
        thinking_budget: Optional[int] = None,
        generation: Optional[Dict[str, Any]] = None,
        transport: Optional[Transport] = None,
    ) -> None:
        self.model = model or os.environ.get("KRELCODE_MODEL") or self.DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.base_url = (
            base_url or os.environ.get("ANTHROPIC_BASE_URL") or self.DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = timeout
        self.max_tokens = max_tokens or self.DEFAULT_MAX_TOKENS
        self.cache_preference = normalize_cache_preference(prompt_cache)
        self.cache_negotiation = self.capabilities.cache.negotiate(
            self.cache_preference)
        self.prompt_cache = self.cache_negotiation.uses_explicit_control
        self.cache_min_tokens = (
            minimum_cacheable_tokens_for_model(self.model)
            if cache_min_tokens is None else max(0, int(cache_min_tokens))
        )
        # 扩展思考预算（None=不启用）。启用时 Anthropic 要求
        # max_tokens > budget_tokens，build_request 会自动保证。
        self.thinking_budget = thinking_budget
        self.generation: Dict[str, Any] = dict(generation or {})
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
                "缺少 API key：请设置 ANTHROPIC_API_KEY（或配置文件；--mock 可离线体验）"
            )
        system_blocks, wire_messages = _to_anthropic_request_messages(
            messages,
            cache_all_without_boundary=self.prompt_cache,
            evidence=evidence,
        )
        max_tokens = self.max_tokens
        payload: Dict[str, Any] = {
            **self.generation,          # temperature/top_p 等；显式键后置不可覆盖
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": wire_messages,
            "stream": True,
        }
        if self.thinking_budget:
            budget = max(1024, int(self.thinking_budget))   # API 最小 1024
            payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
            if max_tokens <= budget:
                payload["max_tokens"] = budget + 1024        # 必须大于思考预算
        stable_system_wire_count = 0
        if system_blocks is not None:
            payload["system"] = _anthropic_system_blocks(system_blocks)
            stable_system_wire_count = sum(
                1 for block in system_blocks.stable_system_blocks if block.text)
        if tools:
            payload["tools"] = tool_specs_to_anthropic(tools)

        candidates = _cache_breakpoint_candidates(
            payload,
            stable_system_block_count=stable_system_wire_count,
        )
        cache_plan = DEFAULT_CACHE_BREAKPOINT_PLANNER.plan(
            candidates,
            mode=self.cache_negotiation.effective_mode,
            max_breakpoints=self.cache_negotiation.max_breakpoints,
            minimum_cacheable_tokens=self.cache_min_tokens,
        )
        _apply_cache_breakpoints(payload, cache_plan)
        return {
            "url": f"{self.base_url}/v1/messages",
            "headers": {
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
            "body": json.dumps(payload).encode("utf-8"),
            "cache_plan": cache_plan,
        }

    # -- LLMProvider 端口实现 ---------------------------------------------------

    def prepare_request(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
        *,
        evidence: Optional[EvidenceChannel] = None,
    ) -> PreparedLLMRequest:
        """Build once, then expose safe accounting alongside the opaque wire body."""
        request = self.build_request(messages, tools, evidence=evidence)
        payload = json.loads(request["body"].decode("utf-8"))
        blocks = resolve_system_prompt_blocks(
            messages,
            cache_all_without_boundary=self.prompt_cache,
        )
        evidence_channel = resolve_evidence_channel(messages, evidence)
        segments, marker_chars, marker_hash = _anthropic_cache_segments(payload)
        cache_plan = request.get("cache_plan")
        if not isinstance(cache_plan, CacheBreakpointPlan):
            cache_plan = None
        stable_chars = marker_chars or (
            blocks.stable_chars if blocks is not None else 0)
        stable_hash = marker_hash or (
            blocks.stable_hash if blocks is not None else "")
        return prepare_http_request(
            provider=self.name,
            model=self.model,
            request=request,
            output_limit_tokens=int(payload.get("max_tokens", 0) or 0),
            cache_mode="explicit" if segments else "none",
            cache_segments=segments,
            stable_cache_chars=stable_chars,
            stable_cache_hash=stable_hash,
            system_prompt_blocks=blocks,
            evidence_channel=evidence_channel,
            cache_negotiation=self.cache_negotiation,
            cache_breakpoint_plan=cache_plan,
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
            tool_blocks: Dict[int, Dict[str, str]] = {}  # tool ordinal -> {id,name,json}
            tool_ordinals: Dict[int, int] = {}  # wire block index -> tool ordinal
            ready_tools: set[int] = set()
            thinking_blocks: Dict[int, Dict[str, Any]] = {}  # index -> thinking/redacted 块
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
                    btype = block.get("type")
                    if btype == "tool_use":
                        wire_index = int(event.get("index", 0))
                        index = len(tool_blocks)
                        tool_ordinals[wire_index] = index
                        tool_blocks[index] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "json": "",
                        }
                        yield ToolCallStartedDelta(
                            index=index,
                            id=tool_blocks[index]["id"],
                            name=tool_blocks[index]["name"],
                        )
                    elif btype == "thinking":
                        thinking_blocks[event.get("index", 0)] = {
                            "type": "thinking", "thinking": "", "signature": None,
                        }
                    elif btype == "redacted_thinking":
                        thinking_blocks[event.get("index", 0)] = {
                            "type": "redacted_thinking", "data": block.get("data", ""),
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
                            slot = thinking_blocks.get(event.get("index", 0))
                            if slot is not None:
                                slot["thinking"] += thinking
                    elif dtype == "signature_delta":
                        slot = thinking_blocks.get(event.get("index", 0))
                        if slot is not None:
                            slot["signature"] = delta.get("signature")
                    elif dtype == "input_json_delta":
                        wire_index = int(event.get("index", 0))
                        tool_index = tool_ordinals.get(wire_index)
                        if tool_index is None:
                            continue
                        slot = tool_blocks.get(tool_index)
                        if slot is not None:
                            partial_json = delta.get("partial_json") or ""
                            if partial_json:
                                slot["json"] += partial_json
                                yield ToolCallArgumentsDelta(
                                    index=tool_index, partial_json=partial_json)
                elif etype == "content_block_stop":
                    wire_index = int(event.get("index", 0))
                    tool_index = tool_ordinals.get(wire_index)
                    if tool_index is None:
                        continue
                    slot = tool_blocks.get(tool_index)
                    if slot is not None and tool_index not in ready_tools:
                        ready_tools.add(tool_index)
                        yield ToolCallReady(
                            index=tool_index,
                            tool_call=ToolCall(
                                id=slot["id"],
                                name=slot["name"],
                                arguments=_parse_input(slot["json"]),
                            ),
                        )
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
                    tool_call = ToolCall(
                        slot["id"], slot["name"], _parse_input(slot["json"]))
                    tool_calls.append(tool_call)
                    if i not in ready_tools:
                        ready_tools.add(i)
                        yield ToolCallReady(index=i, tool_call=tool_call)
            message = Message.assistant(
                "".join(content_parts) or None, tool_calls=tool_calls
            )
            if thinking_blocks:
                # 完整块（含签名）挂到 provider_meta，随历史回传给下一轮
                message.provider_meta = {
                    "anthropic_thinking": [thinking_blocks[i] for i in sorted(thinking_blocks)],
                }
            yield StreamFinished(
                message=message,
                finish_reason=_STOP_REASON_MAP.get(stop_reason or "", stop_reason),
                usage=usage,
            )
        finally:
            resp.close()


def _anthropic_cache_segments(
    payload: Dict[str, Any],
) -> Tuple[List[CacheSegmentSnapshot], int, str]:
    """Extract content-free attribution from every final wire breakpoint."""
    segments: List[CacheSegmentSnapshot] = []
    prefix_tokens = 0
    stable_chars = 0
    stable_hash = ""

    tools = payload.get("tools")
    if isinstance(tools, list):
        for index, item in enumerate(tools):
            text = _wire_text(item)
            prefix_tokens += estimate_text_tokens(text)
            if not isinstance(item, dict) or not item.get("cache_control"):
                continue
            segment_text = _wire_text(tools[:index + 1])
            segments.append(CacheSegmentSnapshot(
                id=f"tools:{index}",
                kind="tools",
                tokens=estimate_text_tokens(segment_text),
                chars=len(segment_text),
                digest=hashlib.sha256(
                    segment_text.encode("utf-8")).hexdigest(),
                cache_control=True,
                stability=CACHE_BREAKPOINT_STABILITY_STATIC,
                cumulative_tokens=prefix_tokens,
            ))

    system = payload.get("system")
    if isinstance(system, list):
        system_parts: List[str] = []
        for index, block in enumerate(system):
            text = _wire_text(block)
            prefix_tokens += estimate_text_tokens(text)
            if not isinstance(block, dict):
                continue
            block_text = str(block.get("text") or "")
            if block_text:
                system_parts.append(block_text)
            if not block.get("cache_control"):
                continue
            segment_text = "\n\n".join(system_parts)
            segment = CacheSegmentSnapshot(
                id=f"system:{index}",
                kind="system",
                tokens=estimate_text_tokens(segment_text),
                chars=len(segment_text),
                digest=hashlib.sha256(
                    segment_text.encode("utf-8")).hexdigest(),
                cache_control=True,
                stability=CACHE_BREAKPOINT_STABILITY_STATIC,
                cumulative_tokens=prefix_tokens,
            )
            segments.append(segment)
            stable_chars = segment.chars
            stable_hash = segment.digest

    messages = payload.get("messages")
    if isinstance(messages, list):
        for index, message in enumerate(messages):
            text = _wire_text(message)
            prefix_tokens += estimate_text_tokens(text)
            content = message.get("content") if isinstance(message, dict) else None
            marked = (
                isinstance(content, list) and content
                and isinstance(content[-1], dict)
                and bool(content[-1].get("cache_control"))
            )
            if not marked:
                continue
            segment_text = _wire_text(messages[:index + 1])
            segments.append(CacheSegmentSnapshot(
                id=f"messages:{index}",
                kind="messages",
                tokens=estimate_text_tokens(segment_text),
                chars=len(segment_text),
                digest=hashlib.sha256(
                    segment_text.encode("utf-8")).hexdigest(),
                cache_control=True,
                stability=(
                    CACHE_BREAKPOINT_STABILITY_TURN
                    if index == len(messages) - 1
                    else CACHE_BREAKPOINT_STABILITY_SESSION
                ),
                cumulative_tokens=prefix_tokens,
            ))
    return segments, stable_chars, stable_hash


def _mark_last_block_cacheable(wire_message: Dict[str, Any]) -> None:
    """给一条 wire 消息的最后一个 content block 打 cache_control 断点。"""
    content = wire_message.get("content")
    if isinstance(content, str):
        if not content:
            return
        wire_message["content"] = [{
            "type": "text", "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
    elif isinstance(content, list) and content:
        last = content[-1]
        if isinstance(last, dict):
            last["cache_control"] = {"type": "ephemeral"}


def _parse_input(raw: str) -> Dict[str, Any]:
    try:
        args = json.loads(raw or "{}")
        if not isinstance(args, dict):
            raise ValueError("input 必须是 JSON 对象")
        return args
    except (json.JSONDecodeError, ValueError):
        return {INVALID_ARGUMENTS_KEY: raw}
