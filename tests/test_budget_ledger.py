"""Unified BudgetLedger, soft/hard thresholds and unfinished manifests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from krelcode.agent.turn.budget import BudgetLedger
from krelcode.agent.turn.budget_control import (
    build_unfinished_manifest,
    handle_budget_gate,
    initialize_budget,
)
from krelcode.contracts.agent import AgentContractFactory
from krelcode.contracts.application import (
    ApplicationRequestFactory,
    TaskBudget,
)
from krelcode.contracts.budget import BudgetAction
from krelcode.contracts.loop import Observation, WaitCondition
from krelcode.core.events import AgentEvent
from krelcode.core.messages import Message


@dataclass
class FakeExchange:
    answer: str

    @property
    def request_id(self) -> str:
        return "budget-choice"

    async def wait(self) -> Optional[str]:
        return self.answer

    def cancel(self) -> None:
        return None


class FakeInputPort:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.options: tuple[str, ...] = ()

    def submit(
        self,
        prompt: str,
        options: tuple[str, ...] = (),
        *,
        allow_free_text: bool = True,
        request_id: str = "",
    ):
        self.options = options
        return FakeExchange(self.answer)


@dataclass
class FakeHost:
    user_input_port: Optional[FakeInputPort] = None
    application_request: object = field(default_factory=lambda: (
        ApplicationRequestFactory.from_input_source("sdk", goal="budget")))
    agent_contract: object = field(default_factory=lambda: (
        AgentContractFactory.from_application(
            ApplicationRequestFactory.from_input_source("sdk", goal="budget"))))
    budget_ledger: Optional[BudgetLedger] = None
    history: list[Message] = field(default_factory=list)
    _current_turn_id: str = "turn-budget"
    events: list[AgentEvent] = field(default_factory=list)
    waits: list[WaitCondition] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)

    async def _emit(self, event: AgentEvent) -> None:
        self.events.append(event)

    async def _enter_waiting(self, condition: WaitCondition) -> None:
        self.waits.append(condition)

    async def _resume_waiting(self, request_id: str) -> None:
        return None

    async def _record_observation(self, observation: Observation) -> None:
        self.observations.append(observation)


def test_soft_limit_waits_and_user_extends_budget() -> None:
    budget = TaskBudget(
        max_tool_calls=4,
        soft_limit_ratio=0.5,
        allow_budget_extension=True,
    )
    host = FakeHost(user_input_port=FakeInputPort("extend"))
    ledger = initialize_budget(host, budget, model="mock")
    ledger.record_tool_call()
    ledger.record_tool_call()

    snapshot = asyncio.run(handle_budget_gate(host))

    assert any(
        getattr(event, "level", "") == "soft"
        for event in host.events
    )
    assert host.user_input_port.options == (
        "continue", "extend", "partial")
    assert host.waits
    assert host.waits[0].kind.value == "budget_decision"
    assert ledger.extensions_used == 1
    assert ledger.limits[next(
        dimension for dimension in ledger.limits
        if dimension.value == "tool_calls"
    )] == 6


def test_hard_limit_produces_structured_unfinished_manifest() -> None:
    budget = TaskBudget(max_tool_calls=1, allow_budget_extension=False)
    host = FakeHost()
    ledger = initialize_budget(host, budget, model="mock")
    ledger.record_tool_call()
    ledger.record_tool_call()

    snapshot = asyncio.run(handle_budget_gate(host))

    assert snapshot.action is BudgetAction.STOP
    manifest = build_unfinished_manifest(
        host, snapshot, reason="hard budget")
    assert manifest.budget is not None
    assert manifest.goal == "budget"
    assert manifest.items
    assert any(
        item.kind == "deliverable" for item in manifest.items)


def test_tool_subagent_and_high_risk_budgets_are_independent() -> None:
    ledger = BudgetLedger(TaskBudget(
        max_tool_calls=3,
        max_subagents=1,
        max_high_risk_actions=1,
    ))
    ledger.record_tool_call()
    assert ledger.can_start_subagent()[0]
    ledger.record_subagent()
    assert not ledger.can_start_subagent()[0]
    assert ledger.can_start_high_risk_action()[0]
    ledger.record_high_risk_action()
    assert not ledger.can_start_high_risk_action()[0]


def test_cost_budget_uses_llm_usage_accounting() -> None:
    ledger = BudgetLedger(
        TaskBudget(max_cost_usd=1.0, soft_limit_ratio=0.5),
        model="gpt-4o",
    )

    ledger.record_llm_usage({"input_tokens": 1_000_000})
    snapshot = ledger.snapshot()

    assert "cost_usd" in snapshot.hard_dimensions
