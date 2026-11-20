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
from .cache import (
    CACHE_MODE_EXPLICIT,
    CACHE_MODE_IMPLICIT,
    CACHE_MODE_NONE,
    CACHE_PREFERENCE_AUTO,
    CACHE_PREFERENCE_OFF,
    CACHE_PREFERENCE_ON,
    CacheCapabilities,
    CacheNegotiation,
    normalize_cache_preference,
)
from .cache_planner import (
    CACHE_BREAKPOINT_PLANNER_VERSION,
    CACHE_BREAKPOINT_STRATEGY,
    CacheBreakpointCandidate,
    CacheBreakpointPlan,
    CacheBreakpointPlanner,
    DEFAULT_CACHE_BREAKPOINT_PLANNER,
    plan_cache_breakpoints,
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
    "CacheCapabilities", "CacheNegotiation", "normalize_cache_preference",
    "CacheBreakpointCandidate", "CacheBreakpointPlan", "CacheBreakpointPlanner",
    "DEFAULT_CACHE_BREAKPOINT_PLANNER", "plan_cache_breakpoints",
    "CACHE_BREAKPOINT_PLANNER_VERSION", "CACHE_BREAKPOINT_STRATEGY",
    "CACHE_MODE_NONE", "CACHE_MODE_IMPLICIT", "CACHE_MODE_EXPLICIT",
    "CACHE_PREFERENCE_AUTO", "CACHE_PREFERENCE_ON", "CACHE_PREFERENCE_OFF",
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
