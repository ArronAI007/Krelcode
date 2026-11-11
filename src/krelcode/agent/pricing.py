"""内置价目表与成本估算（USD / 1M tokens）。

注意：价格是**近似参考值**（官方定价随时间调整），只用于 /cost 与
JSON 输出的量级估算，不用于计费。未知模型返回 None——不估算、不造假。

模型匹配：精确匹配优先，其次最长前缀匹配
（gpt-4o-mini-2024-07-18 → gpt-4o-mini 而非 gpt-4o）。
缓存计价（Anthropic prompt caching）：cache_read 按输入价 10%，
cache_creation 按输入价 125%（5 分钟 ephemeral 档）。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# model → (input USD/1M, output USD/1M)，近似参考值，按需更新
PRICING: Dict[str, Tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.05, 0.20),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "o3-mini": (1.10, 4.40),
    "o4-mini": (1.10, 4.40),
    "claude-opus-4-1": (15.00, 75.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
    "deepseek-chat": (0.28, 1.10),
    "deepseek-reasoner": (0.28, 1.10),
}

CACHE_READ_FACTOR = 0.1       # 缓存命中折扣
CACHE_CREATION_FACTOR = 1.25  # 缓存写入溢价

# model → 上下文窗口（tokens，近似参考值）
CONTEXT_WINDOWS: Dict[str, int] = {
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4.1-nano": 1_000_000,
    "gpt-4.1-mini": 1_000_000,
    "gpt-4.1": 1_000_000,
    "gpt-5-mini": 400_000,
    "gpt-5": 400_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    "claude-opus-4-1": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-haiku": 200_000,
    "gemini-2.5-pro": 1_000_000,
    "gemini-2.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
}

# 自适应预算比例：留 25% 给输出与安全余量
BUDGET_RATIO = 0.75


def _match_model(model: Optional[str], table: Dict):
    """精确 → 最长前缀匹配（gpt-4o-mini-2024-07-18 → gpt-4o-mini 而非 gpt-4o）。"""
    if not model:
        return None
    if model in table:
        return table[model]
    best: Optional[str] = None
    for key in table:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return table[best] if best else None


def match_pricing(model: Optional[str]) -> Optional[Tuple[float, float]]:
    return _match_model(model, PRICING)


def match_context_window(model: Optional[str]) -> Optional[int]:
    """模型上下文窗口（tokens）；未知返回 None。"""
    return _match_model(model, CONTEXT_WINDOWS)


def default_budget_for(model: Optional[str]) -> Optional[int]:
    """自适应压缩预算：窗口 × BUDGET_RATIO；未知模型返回 None（由调用方兜底）。"""
    window = match_context_window(model)
    return int(window * BUDGET_RATIO) if window else None


def estimate_cost(model: Optional[str], usage: Dict[str, int]) -> Optional[float]:
    """按价目表估算单次调用成本（USD）；未知模型返回 None。"""
    pricing = match_pricing(model)
    if pricing is None:
        return None
    p_in, p_out = pricing
    inp = int(usage.get("input_tokens", 0) or 0)
    outp = int(usage.get("output_tokens", 0) or 0)
    cache_read = int(usage.get("cache_read", 0) or 0)
    cache_creation = int(usage.get("cache_creation", 0) or 0)
    return (
        inp * p_in
        + cache_read * p_in * CACHE_READ_FACTOR
        + cache_creation * p_in * CACHE_CREATION_FACTOR
        + outp * p_out
    ) / 1_000_000
