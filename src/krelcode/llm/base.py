"""统一 Provider 机制的核心契约。

所有 provider（OpenAI 兼容 / Anthropic / Mock / 未来更多）实现同一个端口：
    stream(messages, tools) -> AsyncIterator[LLMEvent]

provider 负责把各自千差万别的 wire format（消息结构、SSE 事件模型、
tool-call 增量协议、finish_reason 命名、usage 统计）翻译成统一事件流；
agent 层只消费统一事件，对 provider 差异零感知。

统一约定：
- finish_reason 规范化为 FINISH_STOP / FINISH_TOOLS / FINISH_LENGTH / None
- usage 规范化为 {"input_tokens", "output_tokens", "total_tokens"}（能拿到多少算多少）
- 工具入参为统一 ToolSpec：{"name", "description", "parameters"(JSON Schema)}
- 工具调用参数非法 JSON 时，用 INVALID_ARGUMENTS_KEY 约定保留原文（见 core.messages）
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    Any, AsyncIterator, Dict, List, Mapping, Optional, Protocol, Sequence,
    Tuple, Union,
)

from ..core.context_usage import CacheSegmentSnapshot
from ..core.messages import Message, ToolCall
from ..core.tokens import estimate_text_tokens
from .cache import CacheCapabilities, CacheNegotiation
from .cache_planner import CacheBreakpointPlan

# 规范化 finish_reason
FINISH_STOP = "stop"
FINISH_TOOLS = "tool_calls"
FINISH_LENGTH = "length"

# 规范化 usage 键
USAGE_INPUT = "input_tokens"
USAGE_OUTPUT = "output_tokens"
USAGE_TOTAL = "total_tokens"
USAGE_CACHE_READ = "cache_read"          # 命中缓存读取的 input tokens（折扣价）
USAGE_CACHE_CREATION = "cache_creation"  # 写入缓存的 input tokens（溢价）

ToolSpec = Dict[str, object]  # {"name": str, "description": str, "parameters": dict}

SYSTEM_PROMPT_BLOCKS_META_KEY = "system_prompt_blocks"
EVIDENCE_BLOCKS_META_KEY = "evidence_blocks"
SYSTEM_BLOCK_STABILITY = ("static", "session", "turn")
SYSTEM_BLOCK_CHANNELS = ("instruction", "evidence")
SYSTEM_BLOCK_CHANNEL_INSTRUCTION = "instruction"
SYSTEM_BLOCK_CHANNEL_EVIDENCE = "evidence"


def supports_keyword_argument(callback: object, keyword: str) -> bool:
    """Return whether a callable can accept one named keyword safely."""
    if not callable(callback): return False
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):
        return False
    return keyword in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


@dataclass(frozen=True)
class SystemPromptBlock:
    """One provider-neutral instruction block before wire serialization.

    Evidence is deliberately not a system channel.  ``channel`` remains only
    for compatibility with callers that still construct typed blocks, and
    ``SystemPromptBlocks`` rejects evidence before a request can be assembled.
    """

    id: str
    text: str
    source: str = ""
    stability: str = "static"
    trust: str = "managed"
    cacheable: bool = False
    channel: str = SYSTEM_BLOCK_CHANNEL_INSTRUCTION
    scope: str = ""
    evidence_type: str = ""
    requires_validation: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("system block 必须包含非空 id")
        if self.stability not in SYSTEM_BLOCK_STABILITY:
            raise ValueError(f"未知 system block stability: {self.stability}")
        if self.channel not in SYSTEM_BLOCK_CHANNELS:
            raise ValueError(f"未知 system block channel: {self.channel}")
        if self.stability == "turn" and self.cacheable:
            raise ValueError(f"turn system block 不允许 cacheable: {self.id}")
        if self.channel == SYSTEM_BLOCK_CHANNEL_EVIDENCE and self.cacheable:
            raise ValueError(f"evidence block 不允许 cacheable: {self.id}")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def as_record(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "stability": self.stability,
            "trust": self.trust,
            "cacheable": self.cacheable,
            "channel": self.channel,
            "scope": self.scope,
            "evidence_type": self.evidence_type,
            "requires_validation": self.requires_validation,
            "chars": self.chars,
            "hash": self.hash,
        }


@dataclass(frozen=True)
class EvidenceBlock:
    """A provider-neutral, non-instruction evidence payload."""

    id: str
    text: str
    source: str = ""
    evidence_type: str = ""
    scope: str = ""
    origin: str = ""
    trust: str = "untrusted"
    requires_validation: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("evidence block 必须包含非空 id")
        if not self.text.strip():
            raise ValueError(f"evidence block 不能为空: {self.id}")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def as_record(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "evidence_type": self.evidence_type,
            "scope": self.scope,
            "origin": self.origin,
            "trust": self.trust,
            "requires_validation": self.requires_validation,
            "chars": self.chars,
            "hash": self.hash,
        }


@dataclass(frozen=True)
class EvidenceChannel:
    """Independent, non-cacheable evidence data sent with the current turn.

    Evidence never participates in the system prompt or its stable/dynamic
    hashes.  Providers serialize this channel into the anchored user turn using
    their native content representation.  ``anchor_message_index`` indexes the
    final provider-neutral message list (including the transient system
    message); ``-1`` lets an adapter fall back to the last user message.
    """

    blocks: Tuple[EvidenceBlock, ...] = ()
    anchor_message_index: int = -1
    separator: str = "\n\n"

    def __post_init__(self) -> None:
        object.__setattr__(self, "blocks", tuple(self.blocks))

    @property
    def text(self) -> str:
        return self.separator.join(block.text for block in self.blocks if block.text)

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest() if self.text else ""

    @property
    def enabled(self) -> bool:
        return bool(self.blocks)

    def resolved_anchor_index(self, messages: Sequence[Message]) -> int:
        if 0 <= self.anchor_message_index < len(messages):
            return self.anchor_message_index
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].role == "user":
                return index
        return -1

    def as_record(self) -> Dict[str, object]:
        return {
            "anchor_message_index": self.anchor_message_index,
            "block_count": len(self.blocks),
            "chars": self.chars,
            "hash": self.hash,
            "blocks": [block.as_record() for block in self.blocks],
        }

    @classmethod
    def from_system_block(cls, block: SystemPromptBlock) -> "EvidenceBlock":
        if block.channel != SYSTEM_BLOCK_CHANNEL_EVIDENCE:
            raise ValueError(f"system block 不是 evidence channel: {block.id}")
        metadata = dict(block.metadata)
        return EvidenceBlock(
            id=block.id,
            text=block.text,
            source=block.source,
            evidence_type=block.evidence_type,
            scope=block.scope,
            origin=str(metadata.get("evidence_origin", "") or ""),
            trust=block.trust,
            requires_validation=block.requires_validation,
            metadata=metadata,
        )


@dataclass(frozen=True)
class SystemPromptBlocks:
    """Stable/dynamic instruction blocks with explicit cache boundary metadata."""

    stable_system_blocks: Tuple[SystemPromptBlock, ...]
    dynamic_system_blocks: Tuple[SystemPromptBlock, ...]
    stable_hash: str = ""
    dynamic_hash: str = ""
    cache_control_boundary: int = 0
    separator: str = "\n\n"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "stable_system_blocks",
            tuple(self.stable_system_blocks),
        )
        object.__setattr__(
            self, "dynamic_system_blocks",
            tuple(self.dynamic_system_blocks),
        )
        stable_text = self.stable_text
        dynamic_text = self.dynamic_text
        if not self.stable_hash:
            object.__setattr__(
                self, "stable_hash",
                hashlib.sha256(stable_text.encode("utf-8")).hexdigest()
                if stable_text else "",
            )
        if not self.dynamic_hash:
            object.__setattr__(
                self, "dynamic_hash",
                hashlib.sha256(dynamic_text.encode("utf-8")).hexdigest()
                if dynamic_text else "",
            )
        if any(
            block.channel != SYSTEM_BLOCK_CHANNEL_INSTRUCTION
            for block in (
                *self.stable_system_blocks, *self.dynamic_system_blocks)
        ):
            raise ValueError("evidence block 必须使用独立 EvidenceChannel")
        # Providers place the explicit cache breakpoint after the complete
        # stable prefix (on its last non-empty block).
        boundary = len(stable_text) if stable_text else 0
        object.__setattr__(self, "cache_control_boundary", boundary)

    @property
    def stable_text(self) -> str:
        return self.separator.join(
            block.text for block in self.stable_system_blocks if block.text)

    @property
    def dynamic_text(self) -> str:
        return self.separator.join(
            block.text for block in self.dynamic_system_blocks if block.text)

    @property
    def merged_text(self) -> str:
        return self.separator.join(
            text for text in (self.stable_text, self.dynamic_text) if text)

    @property
    def instruction_blocks(self) -> Tuple[SystemPromptBlock, ...]:
        return tuple(
            block for block in (
                *self.stable_system_blocks, *self.dynamic_system_blocks)
            if block.channel == SYSTEM_BLOCK_CHANNEL_INSTRUCTION
        )

    @property
    def instruction_hash(self) -> str:
        text = self.separator.join(
            block.text for block in self.instruction_blocks if block.text)
        return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""

    @property
    def stable_chars(self) -> int:
        return len(self.stable_text)

    @property
    def dynamic_chars(self) -> int:
        return len(self.dynamic_text)

    @property
    def cache_control_block_index(self) -> int:
        return max(
            (index for index, block in enumerate(self.stable_system_blocks)
             if block.text),
            default=-1,
        )

    def as_record(self) -> Dict[str, object]:
        return {
            "stable_system_blocks": [
                block.as_record() for block in self.stable_system_blocks],
            "dynamic_system_blocks": [
                block.as_record() for block in self.dynamic_system_blocks],
            "stable_hash": self.stable_hash,
            "dynamic_hash": self.dynamic_hash,
            "stable_chars": self.stable_chars,
            "dynamic_chars": self.dynamic_chars,
            "cache_control_boundary": self.cache_control_boundary,
            "cache_control_block_index": self.cache_control_block_index,
            "instruction_block_count": len(self.instruction_blocks),
            "instruction_hash": self.instruction_hash,
        }


@dataclass(frozen=True)
class TextDelta:
    """一段增量文本。真流式 provider 多次产出；非流式一次性给出全文。"""

    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    """一段增量思考/推理文本（reasoning 模型的中间思维）。

    统一层承诺：ThinkingDelta 永远不进入 message.content——
    OpenAI 兼容的 reasoning_content/reasoning 字段与 Anthropic 的
    thinking 块都在各自 adapter 里翻译到这里，仅用于 UI 展示。
    """

    text: str


@dataclass(frozen=True)
class ToolCallStartedDelta:
    """一个工具调用开始形成；index 是工具序号，不是 wire block index。"""

    index: int
    id: str
    name: str


@dataclass(frozen=True)
class ToolCallArgumentsDelta:
    """工具参数 JSON 的原始增量片段；index 与 StartedDelta 相同。"""

    index: int
    partial_json: str


@dataclass(frozen=True)
class ToolCallReady:
    """一个工具调用的 id/name/arguments 已完整，可在流结束前执行。

    对 SSE 原生带 ``content_block_stop`` 的协议，Ready 在该停点产生；对
    OpenAI 兼容协议则在累计 JSON 第一次可解析时产生。同一个 index 至多发出
    一次 Ready。``StreamFinished.message`` 仍是最终权威快照，兼容未发送增量
    事件的旧 provider。
    """

    index: int
    tool_call: ToolCall


@dataclass(frozen=True)
class StreamFinished:
    """一次 LLM 响应结束：完整 assistant 消息 + 规范化 finish_reason + usage。"""

    message: Message
    finish_reason: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)


LLMEvent = Union[
    TextDelta,
    ThinkingDelta,
    ToolCallStartedDelta,
    ToolCallArgumentsDelta,
    ToolCallReady,
    StreamFinished,
]


@dataclass(frozen=True)
class ProviderCapabilities:
    """provider 能力声明（UI/循环可据此降级）。"""

    supports_tools: bool = True
    supports_streaming: bool = True
    supports_evidence_channel: bool = False
    cache: CacheCapabilities = field(default_factory=CacheCapabilities)

    @property
    def supports_prompt_cache(self) -> bool:
        return self.cache.supports_cache


@dataclass(frozen=True)
class PreparedLLMRequest:
    """A provider-ready request plus content-free accounting metadata.

    The URL, headers and body are kept opaque for transport use. Their repr is
    suppressed so accidental logging cannot reveal prompts or credentials; the
    Agent layer only copies the safe metadata into ``ContextUsageSnapshot``.
    """

    provider: str
    model: str
    url: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    request_body_tokens: int
    payload_digest: str
    output_limit_tokens: int = 0
    cache_mode: str = "none"
    cache_segments: Tuple[CacheSegmentSnapshot, ...] = ()
    stable_cache_chars: int = 0
    stable_cache_hash: str = ""
    dynamic_cache_chars: int = 0
    dynamic_cache_hash: str = ""
    stable_system_block_count: int = 0
    dynamic_system_block_count: int = 0
    cache_control_boundary: int = 0
    instruction_system_block_count: int = 0
    evidence_block_count: int = 0
    evidence_chars: int = 0
    evidence_anchor_message_index: int = -1
    instruction_hash: str = ""
    evidence_hash: str = ""
    cache_negotiation: Optional[CacheNegotiation] = None
    cache_breakpoint_plan: Optional[CacheBreakpointPlan] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "body", bytes(self.body))
        object.__setattr__(self, "cache_segments", tuple(self.cache_segments))

    @property
    def body_bytes(self) -> int:
        return len(self.body)

    @property
    def estimated_input_tokens(self) -> int:
        return self.request_body_tokens

    @property
    def wire_fidelity(self) -> str:
        return "exact"

    @property
    def evidence_system_block_count(self) -> int:
        """Deprecated compatibility alias; evidence is not a system channel."""
        return self.evidence_block_count

    def __repr__(self) -> str:
        return (
            "PreparedLLMRequest("
            f"provider={self.provider!r}, model={self.model!r}, "
            f"body_bytes={self.body_bytes}, "
            f"request_body_tokens={self.request_body_tokens}, "
            f"output_limit_tokens={self.output_limit_tokens}, "
            f"cache_mode={self.cache_mode!r}, "
            f"cache_requested={getattr(self.cache_negotiation, 'requested_mode', '')!r}, "
            f"cache_segments={len(self.cache_segments)}, "
            f"cache_breakpoints={self.cache_breakpoint_plan.breakpoint_count if self.cache_breakpoint_plan else 0}, "
            f"evidence_blocks={self.evidence_block_count})"
        )


class LLMProvider(Protocol):
    name: str
    model: str
    capabilities: ProviderCapabilities

    def stream(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
        *,
        evidence: Optional[EvidenceChannel] = None,
    ) -> AsyncIterator[LLMEvent]:
        """给定完整历史（与统一 ToolSpec），流式产出下一条 assistant 消息。"""
        ...


class PreparedLLMProvider(LLMProvider, Protocol):
    """Optional provider capability for assemble-once execution and accounting."""

    def prepare_request(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
        *,
        evidence: Optional[EvidenceChannel] = None,
    ) -> PreparedLLMRequest:
        ...

    def stream_prepared(
        self,
        prepared: PreparedLLMRequest,
    ) -> AsyncIterator[LLMEvent]:
        ...


def prepare_http_request(
    *,
    provider: str,
    model: str,
    request: Mapping[str, Any],
    output_limit_tokens: int = 0,
    cache_mode: Optional[str] = None,
    cache_segments: Sequence[CacheSegmentSnapshot] = (),
    stable_cache_chars: int = 0,
    stable_cache_hash: str = "",
    dynamic_cache_chars: int = 0,
    dynamic_cache_hash: str = "",
    system_prompt_blocks: Optional[SystemPromptBlocks] = None,
    evidence_channel: Optional[EvidenceChannel] = None,
    cache_negotiation: Optional[CacheNegotiation] = None,
    cache_breakpoint_plan: Optional[CacheBreakpointPlan] = None,
) -> PreparedLLMRequest:
    """Convert a provider's built HTTP request into the public prepared contract."""
    body = request.get("body")
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("provider request body 必须是 bytes")
    body_bytes = bytes(body)
    text = body_bytes.decode("utf-8", "replace")
    stable_cache_chars = max(
        0,
        int(stable_cache_chars or (
            system_prompt_blocks.stable_chars
            if system_prompt_blocks is not None else 0)),
    )
    stable_cache_hash = (
        stable_cache_hash
        or (system_prompt_blocks.stable_hash
            if system_prompt_blocks is not None else ""))
    if cache_mode is None:
        cache_mode = (
            cache_negotiation.effective_mode
            if cache_negotiation is not None else "none")
    return PreparedLLMRequest(
        provider=provider,
        model=model,
        url=str(request.get("url") or ""),
        headers=dict(request.get("headers") or {}),
        body=body_bytes,
        request_body_tokens=estimate_text_tokens(text),
        payload_digest=hashlib.sha256(body_bytes).hexdigest(),
        output_limit_tokens=max(0, int(output_limit_tokens or 0)),
        cache_mode=cache_mode,
        cache_segments=tuple(cache_segments),
        stable_cache_chars=stable_cache_chars,
        stable_cache_hash=stable_cache_hash,
        dynamic_cache_chars=max(
            0,
            int(dynamic_cache_chars or (
                system_prompt_blocks.dynamic_chars
                if system_prompt_blocks is not None else 0)),
        ),
        dynamic_cache_hash=(
            dynamic_cache_hash
            or (system_prompt_blocks.dynamic_hash
                if system_prompt_blocks is not None else "")),
        stable_system_block_count=(
            len(system_prompt_blocks.stable_system_blocks)
            if system_prompt_blocks is not None else 0),
        dynamic_system_block_count=(
            len(system_prompt_blocks.dynamic_system_blocks)
            if system_prompt_blocks is not None else 0),
        cache_control_boundary=(
            system_prompt_blocks.cache_control_boundary
            if system_prompt_blocks is not None else 0),
        instruction_system_block_count=(
            len(system_prompt_blocks.instruction_blocks)
            if system_prompt_blocks is not None else 0),
        evidence_block_count=(
            len(evidence_channel.blocks) if evidence_channel is not None else 0),
        evidence_chars=(
            evidence_channel.chars if evidence_channel is not None else 0),
        evidence_anchor_message_index=(
            evidence_channel.anchor_message_index
            if evidence_channel is not None else -1),
        instruction_hash=(
            system_prompt_blocks.instruction_hash
            if system_prompt_blocks is not None else ""),
        evidence_hash=(
            evidence_channel.hash
            if evidence_channel is not None else ""),
        cache_negotiation=cache_negotiation,
        cache_breakpoint_plan=cache_breakpoint_plan,
    )


def cache_segment(
    kind: str,
    text: str,
    *,
    cache_control: bool = False,
) -> CacheSegmentSnapshot:
    """Build content-free cache metadata from a wire segment."""
    return CacheSegmentSnapshot(
        kind=kind,
        tokens=estimate_text_tokens(text),
        chars=len(text),
        digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        cache_control=cache_control,
    )

def explicit_system_prompt_blocks(
    messages: Sequence[Message],
) -> Optional[SystemPromptBlocks]:
    """Read the explicit block contract carried by a transient system message."""
    for message in messages:
        if message.role != "system":
            continue
        blocks = (message.provider_meta or {}).get(SYSTEM_PROMPT_BLOCKS_META_KEY)
        if isinstance(blocks, SystemPromptBlocks):
            return blocks
    return None


def explicit_evidence_channel(
    messages: Sequence[Message],
) -> EvidenceChannel:
    """Read a compatibility evidence channel from transient message metadata."""
    for message in messages:
        channel = (message.provider_meta or {}).get(EVIDENCE_BLOCKS_META_KEY)
        if isinstance(channel, EvidenceChannel):
            return channel
        blocks = channel
        if isinstance(blocks, (list, tuple)) and all(
            isinstance(block, EvidenceBlock) for block in blocks
        ):
            return EvidenceChannel(blocks=tuple(blocks))
    return EvidenceChannel()


def explicit_evidence_blocks(
    messages: Sequence[Message],
) -> Tuple[EvidenceBlock, ...]:
    """Compatibility view of the independent evidence channel."""
    return explicit_evidence_channel(messages).blocks


def resolve_evidence_channel(
    messages: Sequence[Message],
    evidence: Optional[EvidenceChannel] = None,
) -> EvidenceChannel:
    """Prefer the explicit request channel, then compatibility metadata."""
    return evidence if evidence is not None else explicit_evidence_channel(messages)


def resolve_system_prompt_blocks(
    messages: Sequence[Message],
    *,
    cache_all_without_boundary: bool = False,
) -> Optional[SystemPromptBlocks]:
    """Return explicit blocks, or derive a legacy stable/dynamic view."""
    explicit = explicit_system_prompt_blocks(messages)
    if explicit is not None:
        return explicit

    parts: List[str] = []
    boundaries: List[int] = []
    cursor = 0
    for message in messages:
        if message.role != "system" or not message.content:
            continue
        if parts:
            cursor += 2
        meta = message.provider_meta or {}
        for raw in meta.get("prompt_cache_boundaries") or []:
            try:
                local = int(raw)
            except (TypeError, ValueError):
                continue
            boundaries.append(
                cursor + max(0, min(local, len(message.content))))
        parts.append(message.content)
        cursor += len(message.content)
    merged = "\n\n".join(parts)
    if not merged:
        return None
    boundary = max(boundaries) if boundaries else (
        len(merged) if cache_all_without_boundary else 0)
    boundary = max(0, min(boundary, len(merged)))
    stable_text = merged[:boundary]
    dynamic_text = merged[boundary:]
    if stable_text and dynamic_text.startswith("\n\n"):
        # The separator belongs to the stable/dynamic join, not to the
        # dynamic block itself. SystemPromptBlocks.merged_text restores it.
        dynamic_text = dynamic_text[2:]
    stable = (
        SystemPromptBlock(
            id="legacy:system:stable",
            text=stable_text,
            source="legacy_system",
            stability="static",
            cacheable=True,
        ),
    ) if stable_text else ()
    dynamic = (
        SystemPromptBlock(
            id="legacy:system:dynamic",
            text=dynamic_text,
            source="legacy_system",
            stability="session",
            cacheable=False,
        ),
    ) if dynamic_text else ()
    return SystemPromptBlocks(
        stable_system_blocks=stable,
        dynamic_system_blocks=dynamic,
        cache_control_boundary=len(stable_text),
    )


def stable_system_prefix(messages: Sequence[Message]) -> str:
    """Return the explicit stable system prefix in provider-neutral form."""
    blocks = resolve_system_prompt_blocks(messages)
    return blocks.stable_text if blocks is not None else ""


async def collect(
    provider: LLMProvider,
    messages: Sequence[Message],
    tools: Optional[Sequence[ToolSpec]] = None,
    *,
    evidence: Optional[EvidenceChannel] = None,
) -> StreamFinished:
    """便捷函数：消费整个流，返回 StreamFinished（丢弃中间 delta）。"""
    finished: Optional[StreamFinished] = None
    capabilities = getattr(provider, "capabilities", None)
    if (
        evidence is not None
        and getattr(capabilities, "supports_evidence_channel", False)
        and supports_keyword_argument(provider.stream, "evidence")
    ):
        stream = provider.stream(messages, tools, evidence=evidence)
    else:
        stream = provider.stream(messages, tools)
    async for event in stream:
        if isinstance(event, StreamFinished):
            finished = event
    if finished is None:
        raise RuntimeError(f"provider {getattr(provider, 'name', '?')} 未产出 StreamFinished")
    return finished


def normalize_usage(raw: Dict[str, Any]) -> Dict[str, int]:
    """把 provider 的 usage 映射到统一键名（缺失的键不补造）。

    统一语义（Anthropic 风格）：input_tokens **不含** cache_read/cache_creation。
    OpenAI 的 prompt_tokens 含 cached_tokens → 这里减去，防止成本双计；
    total_tokens 保留 provider 原值（含缓存部分的真实总量）。
    """
    out: Dict[str, int] = {}
    details = raw.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = raw.get("cachedContentTokenCount")

    if "input_tokens" in raw or "prompt_tokens" in raw:
        value = raw.get("input_tokens", raw.get("prompt_tokens", 0)) or 0
        if cached and (
            "prompt_tokens" in raw or "cachedContentTokenCount" in raw
        ):
            # OpenAI and Gemini report prompt/cache totals inclusively while
            # Krelcode normalizes ``input_tokens`` to Anthropic-style
            # exclusive accounting.
            value = max(0, int(value) - int(cached))
        out[USAGE_INPUT] = value
    if "output_tokens" in raw or "completion_tokens" in raw:
        out[USAGE_OUTPUT] = raw.get("output_tokens", raw.get("completion_tokens", 0))
    # 缓存 tokens：Anthropic 显式键 / OpenAI prompt_tokens_details.cached_tokens
    if "cache_read_input_tokens" in raw:
        out[USAGE_CACHE_READ] = raw["cache_read_input_tokens"]
    elif cached:
        out[USAGE_CACHE_READ] = int(cached)
    if "cache_creation_input_tokens" in raw:
        out[USAGE_CACHE_CREATION] = raw["cache_creation_input_tokens"]
    if "total_tokens" in raw:
        out[USAGE_TOTAL] = raw["total_tokens"]
    elif USAGE_INPUT in out and USAGE_OUTPUT in out:
        out[USAGE_TOTAL] = out[USAGE_INPUT] + out[USAGE_OUTPUT]
    return out
