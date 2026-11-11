"""Step 36 测试：Retry-After 尊重 + generation 参数透传（真实使用就绪）。"""

import asyncio
import json

import pytest

from fakes import FakeTransport, sse
from krelcode.core.errors import LLMError, LLMHTTPError
from krelcode.core.messages import Message
from krelcode.llm import collect
from krelcode.llm.base import StreamFinished, TextDelta
from krelcode.llm.providers.anthropic import AnthropicProvider
from krelcode.llm.providers.gemini import GeminiProvider
from krelcode.llm.providers.openai import OpenAIProvider
from krelcode.llm.retry import RETRY_AFTER_HARD_CAP, RetryPolicy, RetryingProvider


# -- Retry-After 解析 -----------------------------------------------------------------------------

def test_retry_after_parsing():
    assert LLMHTTPError(429, "x", headers={"Retry-After": "7"}).retry_after == 7.0
    assert LLMHTTPError(429, "x", headers={"retry-after": "2.5"}).retry_after == 2.5
    # HTTP-date 形式与非法值 → None（退回指数退避）
    assert LLMHTTPError(429, "x", headers={
        "retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}).retry_after is None
    assert LLMHTTPError(429, "x", headers={"retry-after": "-3"}).retry_after is None
    assert LLMHTTPError(429, "x").retry_after is None


def test_providers_propagate_retry_after_header():
    """429 响应头一路传到 LLMHTTPError（三家 provider 同构，测 openai 代表）。"""
    body = sse({"choices": [{"delta": {"content": "x"}}]})
    transport = FakeTransport(b'{"error":"rate limited"}', status=429,
                              headers={"Retry-After": "11"})
    provider = OpenAIProvider(api_key="k", transport=transport)
    with pytest.raises(LLMHTTPError) as ei:
        asyncio.run(collect(provider, [Message.user("q")]))
    assert ei.value.status == 429
    assert ei.value.retry_after == 11.0


# -- RetryingProvider 尊重 Retry-After ----------------------------------------------------------------

class FlakyProvider:
    name = "flaky"
    model = "m"
    from krelcode.llm.base import ProviderCapabilities
    capabilities = ProviderCapabilities()

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    async def stream(self, messages, tools=None):
        i = self.calls
        self.calls += 1
        if i < len(self.errors) and self.errors[i] is not None:
            raise self.errors[i]
        yield TextDelta("ok")
        yield StreamFinished(message=Message.assistant("ok"),
                             finish_reason="stop", usage={})


def test_retry_uses_retry_after_when_larger_than_backoff():
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    err = LLMHTTPError(429, "x", headers={"retry-after": "7"})
    inner = FlakyProvider([err, None])
    provider = RetryingProvider(inner, RetryPolicy(max_retries=2, base_delay=1.0),
                                sleep=fake_sleep)
    asyncio.run(collect(provider, [Message.user("q")]))
    assert delays == [7.0]          # 服务器指定 7s > 退避 1s → 用 7s


def test_retry_keeps_backoff_when_retry_after_smaller():
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    err = LLMHTTPError(429, "x", headers={"retry-after": "0.2"})
    inner = FlakyProvider([err, None])
    provider = RetryingProvider(inner, RetryPolicy(max_retries=2, base_delay=1.0),
                                sleep=fake_sleep)
    asyncio.run(collect(provider, [Message.user("q")]))
    assert delays == [1.0]          # max(退避 1.0, retry-after 0.2)


def test_retry_gives_up_when_retry_after_beyond_hard_cap():
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    err = LLMHTTPError(429, "x", headers={"retry-after": str(RETRY_AFTER_HARD_CAP + 1)})
    inner = FlakyProvider([err, None])
    provider = RetryingProvider(inner, RetryPolicy(max_retries=3), sleep=fake_sleep)
    with pytest.raises(LLMHTTPError):
        asyncio.run(collect(provider, [Message.user("q")]))
    assert delays == [] and inner.calls == 1     # 快速失败，不等待


def test_retry_after_absent_falls_back_to_backoff():
    delays = []

    async def fake_sleep(d):
        delays.append(d)

    inner = FlakyProvider([LLMHTTPError(500, "x"), None])
    provider = RetryingProvider(inner, RetryPolicy(max_retries=2, base_delay=1.0),
                                sleep=fake_sleep)
    asyncio.run(collect(provider, [Message.user("q")]))
    assert delays == [1.0]


# -- generation 参数透传 ------------------------------------------------------------------------------

def test_openai_generation_params_and_explicit_precedence():
    provider = OpenAIProvider(
        api_key="k", model="m1",
        generation={"temperature": 0.3, "top_p": 0.9,
                    "model": "evil", "stream": False})   # 恶意/误配不可覆盖显式键
    payload = json.loads(provider.build_request([Message.user("q")], None)["body"])
    assert payload["temperature"] == 0.3 and payload["top_p"] == 0.9
    assert payload["model"] == "m1"          # 显式键赢
    assert payload["stream"] is True


def test_anthropic_generation_params():
    provider = AnthropicProvider(api_key="k", model="m1",
                                 generation={"temperature": 0.5, "model": "evil"})
    payload = json.loads(provider.build_request([Message.user("q")], None)["body"])
    assert payload["temperature"] == 0.5
    assert payload["model"] == "m1"
    assert payload["max_tokens"] == AnthropicProvider.DEFAULT_MAX_TOKENS


def test_gemini_generation_config_section():
    provider = GeminiProvider(api_key="k", model="m1",
                              generation={"temperature": 0.2, "topP": 0.8})
    req = provider.build_request([Message.user("q")], None)
    payload = json.loads(req["body"])
    # Gemini 的 model 在 URL 路径中，生成参数在独立 generationConfig 段
    assert "models/m1:streamGenerateContent" in req["url"]
    assert payload["generationConfig"] == {"temperature": 0.2, "topP": 0.8}
    assert "model" not in payload


def test_registry_generation_passthrough(monkeypatch):
    from krelcode.llm.registry import create_provider, resolve_settings
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = {"providers": {"openai": {"generation": {"temperature": 0.7}}}}
    settings = resolve_settings(provider="openai", config=config)
    provider = create_provider(settings)
    assert provider.generation == {"temperature": 0.7}
    payload = json.loads(provider.build_request([Message.user("q")], None)["body"])
    assert payload["temperature"] == 0.7


def test_generation_default_empty_no_payload_pollution():
    provider = OpenAIProvider(api_key="k")
    assert provider.generation == {}
    payload = json.loads(provider.build_request([Message.user("q")], None)["body"])
    assert set(payload) == {"model", "messages", "stream"}
