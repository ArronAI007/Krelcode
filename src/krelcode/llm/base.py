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
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, AsyncIterator, Dict, Mapping, Optional, Protocol, Sequence, Tuple, Union

from ..core.context_usage import CacheSegmentSnapshot
from ..core.messages import Message, ToolCall
from ..core.tokens import estimate_text_tokens

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

    def __repr__(self) -> str:
        return (
            "PreparedLLMRequest("
            f"provider={self.provider!r}, model={self.model!r}, "
            f"body_bytes={self.body_bytes}, "
            f"request_body_tokens={self.request_body_tokens}, "
            f"output_limit_tokens={self.output_limit_tokens}, "
            f"cache_mode={self.cache_mode!r}, "
            f"cache_segments={len(self.cache_segments)})"
        )


class LLMProvider(Protocol):
    name: str
    model: str
    capabilities: ProviderCapabilities

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
    ) -> AsyncIterator[LLMEvent]:
        """给定完整历史（与统一 ToolSpec），流式产出下一条 assistant 消息。"""
        ...


class PreparedLLMProvider(LLMProvider, Protocol):
    """Optional provider capability for assemble-once execution and accounting."""

    def prepare_request(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
    ) -> PreparedLLMRequest:
        ...

    async def stream_prepared(
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
    cache_mode: str = "none",
    cache_segments: Sequence[CacheSegmentSnapshot] = (),
    stable_cache_chars: int = 0,
    stable_cache_hash: str = "",
) -> PreparedLLMRequest:
    """Convert a provider's built HTTP request into the public prepared contract."""
    body = request.get("body")
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("provider request body 必须是 bytes")
    body_bytes = bytes(body)
    text = body_bytes.decode("utf-8", "replace")
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
        stable_cache_chars=max(0, int(stable_cache_chars or 0)),
        stable_cache_hash=stable_cache_hash,
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


def stable_system_prefix(messages: Sequence[Message]) -> str:
    """Return the first explicit stable system prefix in provider-neutral form."""
    for message in messages:
        if message.role != "system" or not message.content:
            continue
        boundaries = (message.provider_meta or {}).get(
            "prompt_cache_boundaries") or []
        offsets = []
        for raw in boundaries:
            try:
                offsets.append(int(raw))
            except (TypeError, ValueError):
                continue
        if offsets:
            end = max(0, min(max(offsets), len(message.content)))
            if end:
                return message.content[:end]
    return ""


async def collect(
    provider: LLMProvider,
    messages: Sequence[Message],
    tools: Optional[Sequence[ToolSpec]] = None,
) -> StreamFinished:
    """便捷函数：消费整个流，返回 StreamFinished（丢弃中间 delta）。"""
    finished: Optional[StreamFinished] = None
    async for event in provider.stream(messages, tools):
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

    if "input_tokens" in raw or "prompt_tokens" in raw:
        value = raw.get("input_tokens", raw.get("prompt_tokens", 0)) or 0
        if cached and "prompt_tokens" in raw:      # OpenAI：从 input 中扣除
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
