"""Failure-policy execution and validated result collection for subagents."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Optional, Sequence, cast

from ..contracts.delegation import (
    DelegationFailurePolicy,
    DelegationResult,
    WorkspaceVersion,
)
from .contracts import STATUS_PAUSED, STATUS_RUNNING, SubagentManagerPort
from .delegation_runtime import validate_delegation_result
from .models import AgentHandle


@dataclass(frozen=True)
class DelegationCollectionReport:
    handles: tuple[AgentHandle, ...]
    results: tuple[DelegationResult, ...]
    actions: tuple[str, ...] = ()
    escalated: bool = False
    timed_out: bool = False

    @property
    def all_completed(self) -> bool:
        return all(
            handle.status not in {STATUS_RUNNING, STATUS_PAUSED}
            for handle in self.handles
        )

    @property
    def all_accepted(self) -> bool:
        return bool(self.results) and all(
            result.accepted for result in self.results)

    @property
    def any_accepted(self) -> bool:
        return any(result.accepted for result in self.results)


async def collect_delegations(
    manager: SubagentManagerPort,
    handles: Sequence[AgentHandle],
    *,
    timeout: float,
    current_workspace: WorkspaceVersion,
    request_input: Callable[
        [str, tuple[str, ...], bool], Awaitable[Optional[str]]
    ],
    usage_recorder: Optional[Callable[[Mapping[str, float]], None]] = None,
) -> DelegationCollectionReport:
    """Wait, apply failure policy, retry/reassign and validate child reports."""
    current = list(handles)
    deadline = time.monotonic() + max(0.0, timeout)
    await _wait_for_policy(manager, current, deadline)
    actions: list[str] = []
    escalated = False

    failed = _failed_handles(current, current_workspace)
    policies = {
        policy
        for handle in failed
        if (policy := _failure_policy(handle)) is not None
    }
    if failed and DelegationFailurePolicy.FAIL_FAST in policies:
        for handle in current:
            if _is_active(handle):
                try:
                    await manager.kill(handle.id)
                    actions.append(f"fail_fast:kill:{handle.id}")
                except (RuntimeError, ValueError):
                    actions.append(f"fail_fast:already_terminal:{handle.id}")

    retryable = [
        handle for handle in failed
        if _failure_policy(handle) is DelegationFailurePolicy.RETRY_OR_REASSIGN
    ]
    for handle in retryable:
        replacement = await _retry_or_reassign(manager, handle)
        if replacement is None:
            continue
        actions.append(f"retry_or_reassign:{handle.id}->{replacement.id}")
        current[current.index(handle)] = replacement
    if retryable:
        await _wait_for_policy(manager, current, deadline)

    failed = _failed_handles(current, current_workspace)
    escalate = [
        handle for handle in failed
        if _failure_policy(handle) is DelegationFailurePolicy.ESCALATE
    ]
    if escalate:
        escalated = True
        decision = await request_input(
            "Delegated work failed and requires a decision: "
            + ", ".join(handle.id for handle in escalate),
            ("retry", "partial", "cancel"),
            True,
        )
        normalized = str(decision or "").strip().lower()
        if normalized in {"retry", "重试", "1"}:
            for handle in escalate:
                replacement = await _retry_or_reassign(manager, handle)
                if replacement is not None:
                    current[current.index(handle)] = replacement
                    actions.append(
                        f"escalate:retry:{handle.id}->{replacement.id}")
            await _wait_for_policy(manager, current, deadline)
        elif normalized in {"cancel", "取消", "3"}:
            for handle in current:
                if _is_active(handle):
                    try:
                        await manager.kill(handle.id)
                        actions.append(f"escalate:cancel:{handle.id}")
                    except (RuntimeError, ValueError):
                        actions.append(
                            f"escalate:already_terminal:{handle.id}")
        elif normalized in {"partial", "部分", "2"}:
            actions.append("escalate:partial")

    results: list[DelegationResult] = []
    for handle in current:
        if handle.status in {STATUS_RUNNING, STATUS_PAUSED}:
            continue
        result = validate_delegation_result(
            handle, current_workspace=current_workspace)
        await manager.record_delegation_result(handle, result)
        _report_usage(handle, usage_recorder)
        results.append(result)
    return DelegationCollectionReport(
        handles=tuple(current),
        results=tuple(results),
        actions=tuple(actions),
        escalated=escalated,
        timed_out=any(_is_active(handle) for handle in current),
    )


def _failed_handles(
    handles: Sequence[AgentHandle],
    current_workspace: WorkspaceVersion,
) -> list[AgentHandle]:
    failed = []
    for handle in handles:
        if handle.status in {"failed", "cancelled", "killed"}:
            failed.append(handle)
        elif handle.status == "completed":
            result = validate_delegation_result(
                handle, current_workspace=current_workspace)
            if not result.accepted:
                failed.append(handle)
    return failed


def _failure_policy(
    handle: AgentHandle,
) -> Optional[DelegationFailurePolicy]:
    contract = handle.delegation_contract
    if contract is None:
        return None
    return DelegationFailurePolicy(contract.failure_policy)


async def _retry_or_reassign(
    manager: SubagentManagerPort,
    handle: AgentHandle,
) -> Optional[AgentHandle]:
    contract = handle.delegation_contract
    if contract is None:
        return None
    if handle.retry_count >= contract.retry_policy.max_retries:
        return None
    definition = (
        contract.retry_policy.reassign_definition
        if contract.retry_policy.allow_reassign else ""
    )
    try:
        return cast(
            AgentHandle,
            await manager.retry(handle.id, definition=definition),
        )
    except RuntimeError:
        return None


def _report_usage(
    handle: AgentHandle,
    recorder: Optional[Callable[[Mapping[str, float]], None]],
) -> None:
    if recorder is None:
        return
    current = dict(handle.budget_usage)
    reported = dict(handle.reported_budget_usage)
    delta = {
        key: max(0.0, value - reported.get(key, 0.0))
        for key, value in current.items()
    }
    delta = {key: value for key, value in delta.items() if value > 0}
    if not delta:
        return
    recorder(delta)
    handle.reported_budget_usage = current


def _is_active(handle: AgentHandle) -> bool:
    task_obj = getattr(handle, "task_obj", None)
    if task_obj is not None:
        return not task_obj.done()
    return handle.status in {STATUS_RUNNING, STATUS_PAUSED}


async def _wait_for_policy(
    manager: SubagentManagerPort,
    handles: Sequence[AgentHandle],
    deadline: float,
) -> None:
    """Poll bounded waits so FAIL_FAST can stop unrelated active children."""
    while True:
        active = [handle for handle in handles if _is_active(handle)]
        if not active:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await manager.wait_many(
            [handle.id for handle in active],
            timeout=min(0.05, remaining),
        )
        if any(
            _failure_policy(handle) is DelegationFailurePolicy.FAIL_FAST
            and handle.status in {"failed", "cancelled", "killed"}
            for handle in handles
        ):
            return


__all__ = ["DelegationCollectionReport", "collect_delegations"]
