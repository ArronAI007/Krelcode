"""Budget ledger integration for Loop, tools, user choice and partial delivery."""

from __future__ import annotations

from typing import Optional, Protocol

from krelcode.contracts.agent import AgentContract
from krelcode.contracts.application import ApplicationRequest, TaskBudget
from krelcode.contracts.budget import (
    BudgetAction,
    BudgetSnapshot,
    UnfinishedItem,
    UnfinishedManifest,
)
from krelcode.contracts.agent import DeliverableKind
from krelcode.contracts.loop import (
    Observation,
    WaitCondition,
    WaitKind,
)
from krelcode.core.events import (
    AgentEvent,
    BudgetThresholdReached,
    BudgetUpdated,
    UserInputRequested,
    UserInputResolved,
)
from krelcode.core.interactions import UserInputPort
from krelcode.core.messages import Message, ToolCall
from krelcode.contracts.tool import ToolExecutionRuntime, ToolResult
from .budget import BudgetLedger
from .observation import user_input_observation


class BudgetControlHost(Protocol):
    budget_ledger: Optional[BudgetLedger]
    application_request: Optional[ApplicationRequest]
    agent_contract: Optional[AgentContract]
    history: list[Message]
    _current_turn_id: str

    @property
    def user_input_port(self) -> Optional[UserInputPort]: ...

    async def _emit(self, event: AgentEvent) -> None: ...

    async def _enter_waiting(self, condition: WaitCondition) -> None: ...

    async def _resume_waiting(self, request_id: str) -> None: ...

    async def _record_observation(self, observation: Observation) -> None: ...


def initialize_budget(
    host: BudgetControlHost,
    budget: TaskBudget,
    *,
    model: str,
) -> BudgetLedger:
    ledger = BudgetLedger(budget, model=model)
    host.budget_ledger = ledger
    return ledger


def record_llm_budget(
    host: BudgetControlHost,
    usage: dict[str, int],
    *,
    total_tokens: int,
) -> None:
    ledger = host.budget_ledger
    if ledger is None:
        return
    ledger.set_tokens(total_tokens)
    ledger.record_llm_usage(usage)


async def reserve_tool_call(
    host: BudgetControlHost,
    tool_call: ToolCall,
) -> str:
    ledger = host.budget_ledger
    if ledger is None:
        return ""
    allowed, reason = ledger.can_start_tool_call()
    if not allowed:
        await emit_budget_snapshot(host, ledger)
        return reason
    ledger.record_tool_call()
    if tool_call.name == "spawn_agent":
        allowed, reason = ledger.can_start_subagent()
        if not allowed:
            await emit_budget_snapshot(host, ledger)
            return reason
        ledger.record_subagent()
    await emit_budget_snapshot(host, ledger)
    return ""


async def reserve_high_risk_action(
    host: BudgetControlHost,
) -> str:
    ledger = host.budget_ledger
    if ledger is None:
        return ""
    allowed, reason = ledger.can_start_high_risk_action()
    if not allowed:
        await emit_budget_snapshot(host, ledger)
        return reason
    ledger.record_high_risk_action()
    await emit_budget_snapshot(host, ledger)
    return ""


async def handle_budget_gate(
    host: BudgetControlHost,
) -> BudgetSnapshot:
    ledger = host.budget_ledger
    if ledger is None:
        return BudgetSnapshot(action=BudgetAction.CONTINUE)
    snapshot = ledger.snapshot()
    await emit_budget_snapshot(host, ledger, snapshot=snapshot)
    if snapshot.hard_dimensions:
        await host._emit(BudgetThresholdReached(
            level="hard",
            dimensions=snapshot.hard_dimensions,
            budget=snapshot.as_record(),
        ))
        if ledger.budget.allow_budget_extension and ledger.extensions_used < 1:
            choice = await request_budget_choice(
                host,
                snapshot,
                options=("extend", "partial", "cancel"),
            )
            if choice == "extend":
                ledger.extend_limits()
                await emit_budget_snapshot(host, ledger)
                return ledger.snapshot()
            if choice == "partial":
                return BudgetSnapshot(
                    action=BudgetAction.PARTIAL_DELIVERY,
                    dimensions=snapshot.dimensions,
                    soft_dimensions=snapshot.soft_dimensions,
                    hard_dimensions=snapshot.hard_dimensions,
                    convergence_mode=ledger.convergence_mode,
                    delegation_blocked=True,
                    extensions_used=ledger.extensions_used,
                )
            if choice == "cancel":
                return BudgetSnapshot(
                    action=BudgetAction.STOP,
                    dimensions=snapshot.dimensions,
                    soft_dimensions=snapshot.soft_dimensions,
                    hard_dimensions=snapshot.hard_dimensions,
                    convergence_mode=ledger.convergence_mode,
                    delegation_blocked=True,
                    extensions_used=ledger.extensions_used,
                )
        return BudgetSnapshot(
            action=BudgetAction.STOP,
            dimensions=snapshot.dimensions,
            soft_dimensions=snapshot.soft_dimensions,
            hard_dimensions=snapshot.hard_dimensions,
            convergence_mode=ledger.convergence_mode,
            delegation_blocked=True,
            extensions_used=ledger.extensions_used,
        )
    if snapshot.soft_dimensions and not ledger.soft_notified:
        ledger.mark_soft_notified()
        ledger.enable_convergence()
        await host._emit(BudgetThresholdReached(
            level="soft",
            dimensions=snapshot.soft_dimensions,
            budget=snapshot.as_record(),
        ))
        await inject_convergence_instruction(host, snapshot)
        if ledger.budget.ask_user_at_soft_limit:
            choice = await request_budget_choice(
                host,
                snapshot,
                options=("continue", "extend", "partial"),
            )
            if choice == "continue":
                ledger.convergence_mode = False
                ledger.delegation_blocked = False
            elif choice == "extend":
                ledger.extend_limits()
                await emit_budget_snapshot(host, ledger)
            elif choice == "partial":
                return BudgetSnapshot(
                    action=BudgetAction.PARTIAL_DELIVERY,
                    dimensions=snapshot.dimensions,
                    soft_dimensions=snapshot.soft_dimensions,
                    hard_dimensions=snapshot.hard_dimensions,
                    convergence_mode=True,
                    delegation_blocked=True,
                    extensions_used=ledger.extensions_used,
                )
    return ledger.snapshot()


async def emit_budget_snapshot(
    host: BudgetControlHost,
    ledger: BudgetLedger,
    *,
    snapshot: Optional[BudgetSnapshot] = None,
) -> None:
    await host._emit(BudgetUpdated(
        budget=(snapshot or ledger.snapshot()).as_record()))


async def inject_convergence_instruction(
    host: BudgetControlHost,
    snapshot: BudgetSnapshot,
) -> None:
    dimensions = ", ".join(snapshot.soft_dimensions) or "budget"
    text = (
        "[Budget Soft Limit]\n"
        f"Soft budget reached: {dimensions}.\n"
        "Converge now: stop spawning new subagents, avoid unnecessary tools, "
        "prioritize required deliverables and finish the verifiable portion."
    )
    host.history.append(Message.user(text))
    await host._record_observation(Observation(
        observation_id=(
            f"obs-budget-soft-{host._current_turn_id}-"
            f"{snapshot.created_at}"),
        turn_id=host._current_turn_id,
        source="system",
        kind="budget.soft_limit",
        summary=f"soft budget reached: {dimensions}",
        data=snapshot.as_record(),
        trust="trusted",
    ))


async def request_budget_choice(
    host: BudgetControlHost,
    snapshot: BudgetSnapshot,
    *,
    options: tuple[str, ...],
) -> str:
    port = host.user_input_port
    if port is None:
        return ""
    request_id = (
        f"{host._current_turn_id}:budget:"
        f"{snapshot.extensions_used}:{'-'.join(options)}")
    prompt = (
        "任务预算已达到阈值。请选择：继续收敛、扩大预算，"
        "或交付当前可完成的部分结果。"
    )
    exchange = port.submit(
        prompt,
        options,
        allow_free_text=False,
        request_id=request_id,
    )
    await host._emit(UserInputRequested(
        request_id=exchange.request_id,
        prompt=prompt,
        options=options,
        allow_free_text=False,
    ))
    await host._enter_waiting(WaitCondition(
        kind=WaitKind.BUDGET_DECISION,
        request_id=exchange.request_id,
        reason="预算阈值需要用户决策",
    ))
    try:
        answer = await exchange.wait()
    finally:
        await host._resume_waiting(exchange.request_id)
    normalized = str(answer or "").strip().lower()
    await host._emit(UserInputResolved(
        request_id=exchange.request_id,
        answered=bool(normalized),
    ))
    await host._record_observation(user_input_observation(
        turn_id=host._current_turn_id,
        request_id=exchange.request_id,
        answered=bool(normalized),
        answer_chars=len(normalized),
    ))
    return normalized if normalized in options else ""


def build_unfinished_manifest(
    host: BudgetControlHost,
    snapshot: BudgetSnapshot,
    *,
    reason: str,
) -> UnfinishedManifest:
    contract = host.agent_contract
    items: list[UnfinishedItem] = []
    if contract is not None:
        for requirement in contract.deliverables.requirements:
            if not requirement.required:
                continue
            items.append(UnfinishedItem(
                kind="deliverable",
                id=requirement.id,
                summary=(
                    f"deliverable {requirement.id} "
                    f"({DeliverableKind(requirement.kind).value}) not verified"
                ),
                reason=reason,
                metadata={
                    "path": requirement.path,
                    "artifact_required": requirement.artifact_required,
                },
            ))
    goal = (
        contract.goal if contract is not None
        else host.application_request.goal
        if host.application_request is not None else ""
    )
    return UnfinishedManifest(
        goal=goal,
        reason=reason,
        items=tuple(items),
        next_steps=(
            "Resume with a larger budget, or review and deliver the partial result.",
        ),
        budget=snapshot,
        metadata={"exhausted_dimensions": list(snapshot.hard_dimensions)},
    )


def partial_delivery_text(manifest: UnfinishedManifest) -> str:
    lines = [
        "Task stopped at its hard budget before completion.",
        f"Reason: {manifest.reason}",
        "",
        "Unfinished items:",
    ]
    if manifest.items:
        lines.extend(
            f"- [{item.kind}] {item.summary}"
            for item in manifest.items
        )
    else:
        lines.append("- No structured unfinished items were available.")
    if manifest.next_steps:
        lines.extend(("", "Next steps:"))
        lines.extend(f"- {item}" for item in manifest.next_steps)
    return "\n".join(lines)


def budget_denial_result(
    runtime: ToolExecutionRuntime,
    source: str,
    reason: str,
    error_kind: str,
) -> ToolResult:
    label = (
        "高风险动作预算拒绝执行"
        if error_kind == "high_risk_budget_exceeded"
        else "工具预算拒绝执行"
    )
    return ToolResult(
        content=f"{label}: {reason}",
        is_error=True,
        data={
            "source": source,
            "runtime": runtime.value,
            "error_kind": error_kind,
        },
    )


__all__ = [
    "BudgetControlHost",
    "build_unfinished_manifest",
    "budget_denial_result",
    "emit_budget_snapshot",
    "handle_budget_gate",
    "initialize_budget",
    "partial_delivery_text",
    "record_llm_budget",
    "reserve_high_risk_action",
    "reserve_tool_call",
]
