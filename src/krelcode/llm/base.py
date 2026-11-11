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

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, Sequence, Union

from ..core.messages import Message

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
class StreamFinished:
    """一次 LLM 响应结束：完整 assistant 消息 + 规范化 finish_reason + usage。"""

    message: Message
    finish_reason: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)


LLMEvent = Union[TextDelta, ThinkingDelta, StreamFinished]


@dataclass(frozen=True)
class ProviderCapabilities:
    """provider 能力声明（UI/循环可据此降级）。"""

    supports_tools: bool = True
    supports_streaming: bool = True


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
