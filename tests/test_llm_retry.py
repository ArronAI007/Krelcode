"""Step 20a 测试：RetryingProvider 重试语义。"""

import asyncio

import pytest

from krelcode.core.errors import ConfigError, LLMError, LLMHTTPError
from krelcode.core.messages import Message
from krelcode.llm.base import StreamFinished, TextDelta, collect
from krelcode.llm.providers.mock import MockProvider
from krelcode.llm.retry import RetryPolicy, RetryingProvider, is_retryable


class FlakyProvider:
    """按计划失败的测试 provider：plan[i] 为异常实例或 None（成功）。"""
    name = "flaky"
    capabilities = MockProvider().capabilities

    def __init__(self, plan, text="ok"):
        self.plan = list(plan)
        self.text = text
        self.model = "flaky-model"
        self.calls = 0

    async def stream(self, messages, tools=None):
        i = self.calls
        self.calls += 1
        failure = self.plan[i] if i < len(self.plan) else None
        if failure is not None:
            raise failure
        yield TextDelta(self.text)
        yield StreamFinished(message=Message.assistant(self.text),
                             finish_reason="stop", usage={})


class EmitThenFailProvider:
    """先产出一个 delta 再失败：验证『已产出绝不重试』。"""
    name = "mid"
    model = "m"
    capabilities = MockProvider().capabilities

    def __init__(self):
        self.calls = 0

    async def stream(self, messages, tools=None):
        self.calls += 1
        yield TextDelta("部分输出")
        raise LLMHTTPError(500, "stream broken", "mid")


def make_retry(inner, max_retries=3, delays=None):
    recorded = []

    async def fake_sleep(d):
        recorded.append(d)

    provider = RetryingProvider(inner, RetryPolicy(max_retries=max_retries),
                                sleep=fake_sleep)
    return provider, recorded


def http(status):
    return LLMHTTPError(status, "err", "flaky")


# -- 可重试性判定 -------------------------------------------------------------------------------

def test_is_retryable_classification():
    assert is_retryable(http(429)) and is_retryable(http(500)) \
        and is_retryable(http(503)) and is_retryable(http(408))
    assert not is_retryable(http(401)) and not is_retryable(http(400)) \
        and not is_retryable(http(404))
    assert is_retryable(LLMError("网络错误"))        # 网络/超时按瞬态
    assert not is_retryable(ConfigError("缺 key"))   # 配置错误不重试
    assert not is_retryable(ValueError("x"))


# -- 重试行为 ------------------------------------------------------------------------------------

def test_retries_transient_then_succeeds():
    inner = FlakyProvider([http(429), http(503), None])
    provider, delays = make_retry(inner)
    finished = asyncio.run(collect(provider, [Message.user("x")]))
    assert finished.message.content == "ok"
    assert inner.calls == 3
    assert delays == [1.0, 2.0]           # 指数退避 base=1, backoff=2
    assert provider.retry_count == 2


def test_no_retry_on_deterministic_error():
    inner = FlakyProvider([http(401)])
    provider, delays = make_retry(inner)
    with pytest.raises(LLMHTTPError):
        asyncio.run(collect(provider, [Message.user("x")]))
    assert inner.calls == 1 and delays == []


def test_exhausts_retries_then_raises():
    inner = FlakyProvider([http(500)] * 10)
    provider, delays = make_retry(inner, max_retries=2)
    with pytest.raises(LLMHTTPError):
        asyncio.run(collect(provider, [Message.user("x")]))
    assert inner.calls == 3               # 1 次初始 + 2 次重试
    assert delays == [1.0, 2.0]


def test_no_retry_after_partial_emission():
    inner = EmitThenFailProvider()
    provider, delays = make_retry(inner)

    async def go():
        events = []
        with pytest.raises(LLMHTTPError):
            async for e in provider.stream([Message.user("x")]):
                events.append(e)
        return events

    events = asyncio.run(go())
    assert inner.calls == 1               # 已产出 delta → 不重试
    assert len(events) == 1 and events[0].text == "部分输出"
    assert delays == []


def test_zero_retries_disables():
    inner = FlakyProvider([http(429), None])
    provider, _ = make_retry(inner, max_retries=0)
    with pytest.raises(LLMHTTPError):
        asyncio.run(collect(provider, [Message.user("x")]))
    assert inner.calls == 1


def test_port_attributes_passthrough():
    inner = FlakyProvider([])
    provider = RetryingProvider(inner)
    assert provider.name == "flaky"
    assert provider.model == "flaky-model"
    assert provider.capabilities is inner.capabilities


def test_on_retry_callback_invoked():
    seen = []
    inner = FlakyProvider([http(429), None])
    provider = RetryingProvider(
        inner, RetryPolicy(max_retries=2),
        sleep=lambda d: asyncio.sleep(0),
        on_retry=lambda attempt, delay, err: seen.append((attempt, delay, type(err).__name__)))
    asyncio.run(collect(provider, [Message.user("x")]))
    assert seen == [(1, 1.0, "LLMHTTPError")]


# -- app 接线 --------------------------------------------------------------------------------------

def test_app_retries_flag_and_wrapping(monkeypatch):
    from krelcode.app import build_llm, build_parser

    parser = build_parser()
    assert parser.parse_args([]).retries == 2          # 默认开

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    args = parser.parse_args(["--provider", "openai"])
    llm = build_llm(args, {})
    assert isinstance(llm, RetryingProvider)

    args0 = parser.parse_args(["--provider", "openai", "--retries", "0"])
    llm0 = build_llm(args0, {})
    assert not isinstance(llm0, RetryingProvider)

    args_mock = parser.parse_args(["--mock"])
    assert isinstance(build_llm(args_mock, {}), MockProvider)  # mock 不包装
