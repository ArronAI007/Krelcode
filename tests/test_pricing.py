"""Step 30 测试：价目表匹配、成本估算、UsageTracker 成本累计。"""

import asyncio

import pytest

from krelcode.agent.pricing import (
    CACHE_CREATION_FACTOR, CACHE_READ_FACTOR, PRICING, estimate_cost,
    match_pricing,
)
from krelcode.agent.usage import UsageTracker
from krelcode.core.events import EventBus, LLMResponseReceived, RunStarted
from krelcode.core.messages import Message


# -- 价目匹配 ------------------------------------------------------------------------------------

def test_match_exact():
    assert match_pricing("gpt-4o") == PRICING["gpt-4o"]
    assert match_pricing("claude-sonnet-4-5") == PRICING["claude-sonnet-4-5"]


def test_match_longest_prefix_wins():
    # 带日期后缀 → 匹配到最具体的型号（mini 而非 gpt-4o）
    assert match_pricing("gpt-4o-mini-2024-07-18") == PRICING["gpt-4o-mini"]
    assert match_pricing("claude-sonnet-4-5-20250929") == PRICING["claude-sonnet-4-5"]
    assert match_pricing("gemini-2.5-flash-lite") is not None   # 前缀命中 flash


def test_match_unknown_and_none():
    assert match_pricing("totally-unknown-model") is None
    assert match_pricing(None) is None
    assert match_pricing("") is None


# -- 成本估算 ------------------------------------------------------------------------------------

def test_estimate_cost_basic_math():
    # gpt-4o-mini: 0.15/1M in, 0.60/1M out
    cost = estimate_cost("gpt-4o-mini",
                         {"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert cost == pytest.approx(0.75)


def test_estimate_cost_with_cache_tokens():
    # claude-sonnet-4-5: 3/15；cache_read 10% 价、cache_creation 125% 价
    usage = {"input_tokens": 1000, "output_tokens": 2000,
             "cache_read": 100_000, "cache_creation": 10_000}
    expected = (1000 * 3 + 100_000 * 3 * CACHE_READ_FACTOR
                + 10_000 * 3 * CACHE_CREATION_FACTOR + 2000 * 15) / 1e6
    assert estimate_cost("claude-sonnet-4-5", usage) == pytest.approx(expected)
    assert expected == pytest.approx(0.1005)


def test_estimate_cost_unknown_model_is_none():
    assert estimate_cost("nope", {"input_tokens": 10, "output_tokens": 10}) is None
    assert estimate_cost(None, {"input_tokens": 10}) is None


# -- UsageTracker 成本累计 -------------------------------------------------------------------------

def test_tracker_accumulates_cost_with_known_model():
    bus = EventBus()
    tracker = UsageTracker(model="gpt-4o-mini")
    tracker.attach(bus)

    async def go():
        await bus.emit(RunStarted("r"))
        await bus.emit(LLMResponseReceived(
            Message.assistant("a"),
            {"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1_000_000}))
        await bus.emit(LLMResponseReceived(
            Message.assistant("b"),
            {"input_tokens": 0, "output_tokens": 1_000_000, "total_tokens": 1_000_000}))

    asyncio.run(go())
    assert tracker.cost_usd == pytest.approx(0.75)
    assert "估算成本 $0.7500" in tracker.summary()


def test_tracker_unknown_model_no_cost():
    bus = EventBus()
    tracker = UsageTracker(model="mock-model")
    tracker.attach(bus)
    asyncio.run(bus.emit(LLMResponseReceived(
        Message.assistant("x"), {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20})))
    assert tracker.cost_usd is None
    assert "估算成本" not in tracker.summary()
    assert tracker.session.calls == 1          # token 统计照常


def test_tracker_cache_tokens_in_text():
    bus = EventBus()
    tracker = UsageTracker(model="claude-sonnet-4-5")
    tracker.attach(bus)
    asyncio.run(bus.emit(LLMResponseReceived(
        Message.assistant("x"),
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
         "cache_read": 5000, "cache_creation": 200})))
    text = tracker.session.text()
    assert "cache 读 5000 / 写 200" in text
    assert tracker.session.cache_read == 5000
