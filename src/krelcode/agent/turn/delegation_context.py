"""Build parent delegation context for tool execution."""

from __future__ import annotations

from typing import Callable, Mapping, Optional, Protocol

from krelcode.contracts.application import ApplicationRequest, TaskBudget
from krelcode.contracts.agent import AgentContract
from krelcode.contracts.delegation import DelegationParentContext
from krelcode.contracts.tool import ToolContext
from krelcode.agent.turn.budget import BudgetLedger


class DelegationContextHost(Protocol):
    application_request: Optional[ApplicationRequest]
    agent_contract: Optional[AgentContract]
    budget_ledger: Optional[BudgetLedger]
    _current_turn_id: str
    _turn_tool_context: ToolContext


def build_parent_delegation_context(
    host: DelegationContextHost,
) -> tuple[
    DelegationParentContext,
    Optional[Callable[[Mapping[str, float]], None]],
]:
    ledger = host.budget_ledger
    budget: TaskBudget
    if ledger is not None:
        budget = ledger.remaining_task_budget()
    elif host.application_request is not None:
        budget = host.application_request.budget
    else:
        budget = TaskBudget()
    runtime = host._turn_tool_context.runtime
    parent_contract = host.agent_contract
    context = DelegationParentContext(
        parent_task_id=host._current_turn_id,
        parent_turn_id=host._current_turn_id,
        parent_session_id=runtime.session_id or "",
        parent_contract_id=(
            parent_contract.contract_id
            if parent_contract is not None else ""),
        parent_contract_hash=(
            parent_contract.contract_hash
            if parent_contract is not None else ""),
        owner=(
            f"agent:{parent_contract.contract_id}"
            if parent_contract is not None else "parent"
        ),
        budget=budget,
    )
    recorder = (
        ledger.record_external_usage if ledger is not None else None
    )
    return context, recorder


__all__ = ["build_parent_delegation_context"]
