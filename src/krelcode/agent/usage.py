"""用量统计：EventBus 旁路订阅者，累计 LLM 调用的 token 用量。

数据源是 LLMResponseReceived 事件的 usage 字段（llm 层已规范化为
input_tokens/output_tokens/total_tokens；provider 拿不到时为空 dict，
统计自然为 0——不估算、不造假）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.events import AgentEvent, EventBus, LLMResponseReceived, RunStarted


@dataclass
class UsageTotals:
    calls: int = 0
    input: int = 0
    output: int = 0
    total: int = 0

    def add(self, usage: dict) -> None:
        self.calls += 1
        self.input += int(usage.get("input_tokens", 0) or 0)
        self.output += int(usage.get("output_tokens", 0) or 0)
        self.total += int(usage.get("total_tokens", 0) or 0)

    def text(self) -> str:
        return (f"LLM 调用 {self.calls} 次 · input {self.input} · "
                f"output {self.output} · total {self.total} tokens")


class UsageTracker:
    def __init__(self) -> None:
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

    def summary(self) -> str:
        return f"{self.session.text()}（本轮: {self.turn.text()}）"
