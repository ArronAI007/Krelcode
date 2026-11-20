"""Provider cache capability negotiation and its request observability."""

import asyncio
import json

import pytest

from fakes import FakeTransport, sse
from krelcode.agent.context_usage import build_context_usage_snapshot
from krelcode.core.errors import ConfigError
from krelcode.core.messages import Message
from krelcode.llm.cache import (
    CACHE_MODE_EXPLICIT,
    CACHE_MODE_IMPLICIT,
    CACHE_MODE_NONE,
    CACHE_REASON_AUTO_EXPLICIT,
    CACHE_REASON_AUTO_IMPLICIT,
    CACHE_REASON_DISABLED,
    CACHE_REASON_FALLBACK_IMPLICIT,
    CacheCapabilities,
    normalize_cache_preference,
)
from krelcode.llm.providers.anthropic import AnthropicProvider
from krelcode.llm.providers.gemini import GeminiProvider
from krelcode.llm.providers.openai import OpenAIProvider
from krelcode.llm.registry import ProviderSettings, create_provider


def test_cache_preferences_normalize_bool_and_string_aliases():
    assert normalize_cache_preference(True) == "on"
    assert normalize_cache_preference(False) == "off"
    assert normalize_cache_preference(" TRUE ") == "on"
    assert normalize_cache_preference("disabled") == "off"
    assert normalize_cache_preference("default") == "auto"
    assert normalize_cache_preference(None) == "auto"
    assert normalize_cache_preference("explicit") == "explicit"

    with pytest.raises(ConfigError, match="未知 prompt_cache"):
        normalize_cache_preference("sometimes")


def test_capability_negotiation_selects_and_explains_effective_mode():
    capabilities = CacheCapabilities(
        modes=(CACHE_MODE_IMPLICIT, CACHE_MODE_EXPLICIT),
        preferred_mode=CACHE_MODE_EXPLICIT,
        max_breakpoints=4,
        reports_cache_read=True,
        reports_cache_creation=True,
        supports_ttl=True,
    )

    automatic = capabilities.negotiate("auto")
    assert automatic.effective_mode == CACHE_MODE_EXPLICIT
    assert automatic.reason == CACHE_REASON_AUTO_EXPLICIT
    assert automatic.supported_modes == (
        CACHE_MODE_NONE, CACHE_MODE_IMPLICIT, CACHE_MODE_EXPLICIT)
    assert automatic.max_breakpoints == 4
    assert automatic.reports_cache_read is True

    implicit = CacheCapabilities(
        modes=(CACHE_MODE_IMPLICIT,), preferred_mode=CACHE_MODE_IMPLICIT)
    automatic = implicit.negotiate("on")
    assert automatic.effective_mode == CACHE_MODE_IMPLICIT
    assert automatic.reason == CACHE_REASON_AUTO_IMPLICIT

    fallback = implicit.negotiate("explicit")
    assert fallback.effective_mode == CACHE_MODE_IMPLICIT
    assert fallback.fallback is True
    assert fallback.reason == CACHE_REASON_FALLBACK_IMPLICIT

    disabled = capabilities.negotiate(False)
    assert disabled.effective_mode == CACHE_MODE_NONE
    assert disabled.reason == CACHE_REASON_DISABLED
    assert disabled.max_breakpoints == 4

    unsupported = CacheCapabilities().negotiate("auto")
    assert unsupported.effective_mode == CACHE_MODE_NONE
    assert unsupported.supported_modes == (CACHE_MODE_NONE,)


def test_explicit_only_provider_does_not_silently_enable_implicit_cache():
    capabilities = CacheCapabilities(
        modes=(CACHE_MODE_EXPLICIT,), preferred_mode=CACHE_MODE_EXPLICIT)
    negotiation = capabilities.negotiate("implicit")
    assert negotiation.effective_mode == CACHE_MODE_NONE
    assert negotiation.reason == "requested_implicit_unsupported"


def test_provider_adapters_declare_real_cache_capabilities():
    anthropic = AnthropicProvider(model="claude-x", api_key="k")
    openai = OpenAIProvider(model="deepseek-chat", api_key="k")
    gemini_new = GeminiProvider(model="gemini-2.5-flash", api_key="k")
    gemini_old = GeminiProvider(model="gemini-2.0-flash", api_key="k")

    assert anthropic.cache_negotiation.effective_mode == CACHE_MODE_EXPLICIT
    assert anthropic.cache_negotiation.max_breakpoints == 4
    assert anthropic.cache_negotiation.reports_cache_creation is True

    assert openai.cache_negotiation.effective_mode == CACHE_MODE_IMPLICIT
    assert openai.capabilities.cache.reports_cache_read is True
    assert openai.cache_negotiation.max_breakpoints == 0

    assert gemini_new.cache_negotiation.effective_mode == CACHE_MODE_IMPLICIT
    assert gemini_old.cache_negotiation.effective_mode == CACHE_MODE_NONE


def test_generic_provider_config_flows_into_cache_negotiation():
    openai = create_provider(ProviderSettings(
        provider="openai",
        model="deepseek-chat",
        api_key="k",
        extra={"prompt_cache": "off"},
    ))
    gemini = create_provider(ProviderSettings(
        provider="gemini",
        model="gemini-2.5-flash",
        api_key="k",
        extra={"prompt_cache": True},
    ))
    anthropic = create_provider(ProviderSettings(
        provider="anthropic",
        api_key="k",
        extra={"prompt_cache": False},
    ))

    assert openai.cache_negotiation.effective_mode == CACHE_MODE_NONE
    assert openai.cache_negotiation.reason == CACHE_REASON_DISABLED
    assert gemini.cache_negotiation.effective_mode == CACHE_MODE_IMPLICIT
    assert anthropic.cache_negotiation.effective_mode == CACHE_MODE_NONE


def test_prepared_request_and_context_snapshot_expose_negotiation_without_content():
    provider = OpenAIProvider(
        model="gpt-cache-test",
        api_key="sk-secret-cache-key",
        prompt_cache="explicit",
        transport=FakeTransport(b""),
    )
    messages = [
        Message.system(
            "STABLE PRIVATE PREFIX\n\nDYNAMIC PRIVATE TAIL",
            provider_meta={"prompt_cache_boundaries": [len("STABLE PRIVATE PREFIX")]},
        ),
        Message.user("PRIVATE QUESTION"),
    ]

    prepared = provider.prepare_request(messages)
    snapshot = build_context_usage_snapshot(
        messages,
        turn_id="cache-negotiation",
        step=1,
        prepared_request=prepared,
    )

    assert prepared.cache_mode == CACHE_MODE_IMPLICIT
    assert prepared.cache_negotiation is provider.cache_negotiation
    assert snapshot.cache_mode == CACHE_MODE_IMPLICIT
    assert snapshot.cache_requested_mode == CACHE_MODE_EXPLICIT
    assert snapshot.cache_negotiation_reason == CACHE_REASON_FALLBACK_IMPLICIT
    assert snapshot.cache_fallback is True
    assert snapshot.cache_supported_modes == (
        CACHE_MODE_NONE, CACHE_MODE_IMPLICIT)

    record = json.dumps(snapshot.as_record(), ensure_ascii=False)
    assert "sk-secret-cache-key" not in record
    assert "STABLE PRIVATE PREFIX" not in record
    assert "PRIVATE QUESTION" not in record


def test_openai_cache_off_disables_implicit_segment_accounting():
    provider = OpenAIProvider(
        model="m", api_key="k", prompt_cache=False,
        transport=FakeTransport(b""),
    )
    prepared = provider.prepare_request([Message.user("hello")])
    assert prepared.cache_mode == CACHE_MODE_NONE
    assert prepared.cache_segments == ()
    assert prepared.cache_negotiation.reason == CACHE_REASON_DISABLED


def test_gemini_reports_cached_content_tokens_in_normalized_usage():
    body = sse(
        {
            "candidates": [{
                "content": {"parts": [{"text": "ok"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 5,
                "cachedContentTokenCount": 80,
                "totalTokenCount": 105,
            },
        }
    )
    provider = GeminiProvider(
        model="gemini-2.5-flash", api_key="k", transport=FakeTransport(body))

    async def consume():
        return [event async for event in provider.stream([Message.user("hi")])]

    events = asyncio.run(consume())
    usage = events[-1].usage
    assert usage["input_tokens"] == 20
    assert usage["cache_read"] == 80
    assert usage["total_tokens"] == 105
