"""用量统计：EventBus 旁路订阅者，累计 LLM 调用的 token 用量。

数据源是 LLMResponseReceived 事件的 usage 字段（llm 层已规范化为
input_tokens/output_tokens/total_tokens；provider 拿不到时为空 dict，
统计自然为 0——不估算、不造假）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..core.events import AgentEvent, EventBus, LLMResponseReceived, RunStarted
from .pricing import estimate_cost, match_pricing


@dataclass
class UsageTotals:
    calls: int = 0
    input: int = 0
    output: int = 0
    total: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    cost: float = 0.0        # USD，估算

    def add(self, usage: dict) -> None:
        self.calls += 1
        self.input += int(usage.get("input_tokens", 0) or 0)
        self.output += int(usage.get("output_tokens", 0) or 0)
        self.total += int(usage.get("total_tokens", 0) or 0)
        self.cache_read += int(usage.get("cache_read", 0) or 0)
        self.cache_creation += int(usage.get("cache_creation", 0) or 0)

    def text(self) -> str:
        base = (f"LLM 调用 {self.calls} 次 · input {self.input} · "
                f"output {self.output} · total {self.total} tokens")
        if self.cache_read or self.cache_creation:
            base += (f" · cache 读 {self.cache_read} / 写 {self.cache_creation}")
        return base


class UsageTracker:
    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model             # 用于成本估算；None/未知 → 不算钱
        self.session = UsageTotals()   # 进程内累计
        self.turn = UsageTotals()      # 当前/最近一轮

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(self.on_event)

    def on_event(self, event: AgentEvent) -> None:
        if isinstance(event, RunStarted):
            self.turn = UsageTotals()
        elif isinstance(event, LLMResponseReceived):
            self.session.add(event.usage)
            self.turn.add(event.usage)
            cost = estimate_cost(self.model, event.usage)
            if cost is not None:
                self.session.cost += cost
                self.turn.cost += cost

    @property
    def cost_usd(self) -> Optional[float]:
        """会话累计成本估算；模型不在价目表时返回 None（不造假）。"""
        if match_pricing(self.model) is None:
            return None
        return round(self.session.cost, 6)

    def cost_note(self) -> str:
        cost = self.cost_usd
        if cost is None:
            return ""
        return f" · 估算成本 ${cost:.4f}（内置价目表，仅供参考）"

    def summary(self) -> str:
        return f"{self.session.text()}（本轮: {self.turn.text()}）{self.cost_note()}"
