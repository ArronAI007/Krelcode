"""llm 层门面：统一 Provider 机制的公开入口。"""

from .base import (
    FINISH_LENGTH,
    FINISH_STOP,
    FINISH_TOOLS,
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
    normalize_usage,
    prepare_http_request,
    resolve_system_prompt_blocks,
    stable_system_prefix,
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
    "SystemPromptBlock", "SystemPromptBlocks",
    "ToolCallStartedDelta", "ToolCallArgumentsDelta", "ToolCallReady",
    "ProviderCapabilities", "collect", "normalize_usage",
    "prepare_http_request", "cache_segment", "stable_system_prefix",
    "resolve_system_prompt_blocks",
    "FINISH_STOP", "FINISH_TOOLS", "FINISH_LENGTH",
    "OpenAIProvider", "AnthropicProvider", "GeminiProvider", "MockProvider",
    "ProviderSettings", "create_provider", "resolve_settings", "register",
    "list_providers",
    "Transport", "TransportResponse", "UrllibTransport",
]
