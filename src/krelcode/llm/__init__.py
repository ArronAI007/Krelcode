"""llm 层门面：统一 Provider 机制的公开入口。"""

from .base import (
    FINISH_LENGTH,
    FINISH_STOP,
    FINISH_TOOLS,
    LLMEvent,
    LLMProvider,
    ProviderCapabilities,
    StreamFinished,
    TextDelta,
    ToolSpec,
    collect,
    normalize_usage,
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
    "LLMProvider", "LLMEvent", "TextDelta", "StreamFinished", "ToolSpec",
    "ProviderCapabilities", "collect", "normalize_usage",
    "FINISH_STOP", "FINISH_TOOLS", "FINISH_LENGTH",
    "OpenAIProvider", "AnthropicProvider", "GeminiProvider", "MockProvider",
    "ProviderSettings", "create_provider", "resolve_settings", "register",
    "list_providers",
    "Transport", "TransportResponse", "UrllibTransport",
]
