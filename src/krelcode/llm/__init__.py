"""llm 层门面：统一 Provider 机制的公开入口。"""

from .base import (
    EVIDENCE_BLOCKS_META_KEY,
    FINISH_LENGTH,
    FINISH_STOP,
    FINISH_TOOLS,
    EvidenceBlock,
    EvidenceChannel,
    LLMEvent,
    LLMProvider,
    PreparedLLMProvider,
    PreparedLLMRequest,
    ProviderCapabilities,
    StreamFinished,
    SystemPromptBlock,
    SystemPromptBlocks,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallReady,
    ToolCallStartedDelta,
    ToolSpec,
    cache_segment,
    collect,
    explicit_evidence_blocks,
    explicit_evidence_channel,
    normalize_usage,
    prepare_http_request,
    resolve_evidence_channel,
    resolve_system_prompt_blocks,
    stable_system_prefix,
    supports_keyword_argument,
)
from .providers.anthropic import AnthropicProvider
from .providers.gemini import GeminiProvider
from .providers.mock import MockProvider
from .providers.openai import OpenAIProvider
from .registry import (
    ProviderSettings, create_provider, list_providers, register, resolve_settings,
)
from .transport import Transport, TransportResponse, UrllibTransport

__all__ = [
    "LLMProvider", "PreparedLLMProvider", "PreparedLLMRequest",
    "LLMEvent", "TextDelta", "StreamFinished", "ToolSpec",
    "SystemPromptBlock", "SystemPromptBlocks", "EvidenceBlock", "EvidenceChannel",
    "EVIDENCE_BLOCKS_META_KEY", "explicit_evidence_blocks",
    "explicit_evidence_channel", "resolve_evidence_channel",
    "ToolCallStartedDelta", "ToolCallArgumentsDelta", "ToolCallReady",
    "ProviderCapabilities", "collect", "normalize_usage",
    "prepare_http_request", "cache_segment", "stable_system_prefix",
    "supports_keyword_argument",
    "resolve_system_prompt_blocks",
    "FINISH_STOP", "FINISH_TOOLS", "FINISH_LENGTH",
    "OpenAIProvider", "AnthropicProvider", "GeminiProvider", "MockProvider",
    "ProviderSettings", "create_provider", "resolve_settings", "register",
    "list_providers",
    "Transport", "TransportResponse", "UrllibTransport",
]
