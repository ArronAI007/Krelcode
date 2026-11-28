"""Subagent lifecycle state, interaction, retry and waiting controls."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..contracts.application import ApplicationRequest
from ..contracts.delegation import (
    DelegationKind,
    DelegationResult,
    DelegationStatus,
)
from ..core.events import (
    AgentEvent,
    ContextAssembled,
    ContextCheckpointCreated,
    ErrorOccurred,
    EventBus,
    RunCancelled,
    RunFinished,
    SubagentFeedUpdated,
    SubagentFinished,
    SubagentStateChanged,
    ToolCallCompleted,
)
from ..core.messages import sanitize_history
from .contracts import (
    STATUS_KILLED,
    STATUS_PAUSED,
    STATUS_RUNNING,
)
from .execution import run_handle
from .feed import AgentFeedEntry
from .feed_projection import project_feed_event as _feed_event
from .models import INTERRUPT_MARKER, AgentHandle
from .worktrees import SubagentWorktreeManager


class SubagentControlMixin:
    """Control-plane behavior mixed into :class:`SubagentManager`."""

    bus: EventBus
    worktrees: SubagentWorktreeManager
    _handles: Dict[str, AgentHandle]
    _spawn_slots: asyncio.Semaphore

    async def spawn(self, *args: Any, **kwargs: Any) -> AgentHandle:
        raise NotImplementedError

    def _require_handle(self, agent_id: str) -> AgentHandle:
        raise NotImplementedError

    async def _on_child_event(
        self,
        handle: AgentHandle,
        event: AgentEvent,
    ) -> None:
        if isinstance(event, ContextAssembled):
            ledger = handle.loop.runtime_state.context.last_context_cache_ledger
            if (
                ledger is not None
                and ledger.complete_request_hash
                and ledger.complete_request_hash != handle.cache_ledger_hash
            ):
                handle.cache_ledger_hash = ledger.complete_request_hash
                if handle.sidechain is not None:
                    handle.sidechain.write({
                        "type": "context_cache_ledger",
                        "ledger": ledger.as_dict(),
                    })
        if isinstance(event, ContextCheckpointCreated):
            handle.context_checkpoint_id = event.checkpoint_id
            if handle.sidechain is not None:
                handle.sidechain.write({
                    "type": "context_checkpoint",
                    "checkpoint_id": event.checkpoint_id,
                    "turn_id": event.turn_id,
                    "phase": event.phase,
                    "history_hash": event.history_hash,
                })
        if handle.feed is None:
            return
        now = time.time()
        if isinstance(event, (ToolCallCompleted, RunFinished, RunCancelled,
                              ErrorOccurred)):
            await self._publish_feed_entries(handle, handle.feed.flush_all(ts=now))
        mapped = _feed_event(event)
        if mapped is None:
            return
        kind, text, data = mapped
        entry = handle.feed.add(kind, text, ts=now, data=data)
        if entry is not None:
            await self._publish_feed_entries(handle, (entry,))

    async def _publish_feed_entries(
        self,
        handle: AgentHandle,
        entries: Iterable[AgentFeedEntry],
    ) -> None:
        for entry in entries:
            if handle.sidechain is not None:
                handle.sidechain.append_feed(entry)
            await self.bus.emit(SubagentFeedUpdated(
                handle.id, entry.sequence, entry.kind, entry.text, entry.data))

    async def _set_state(
        self,
        handle: AgentHandle,
        status: str,
        reason: str = "",
    ) -> None:
        if handle.status == status:
            return
        handle.status = status
        if (
            status in {"completed", "failed", "cancelled", "killed"}
            and handle.delegation_relation is not None
            and DelegationKind(handle.delegation_relation.kind)
            is DelegationKind.HANDOFF
        ):
            handle.responsibility_owner = (
                handle.delegation_relation.from_owner)
        if handle.sidechain is not None:
            handle.sidechain.append_state(
                status, reason=reason, turns=handle.turns,
                result=handle.result, error=handle.error)
        await self.bus.emit(SubagentStateChanged(
            handle.id,
            status,
            reason,
            delegation_id=(
                handle.delegation_contract.delegation_id
                if handle.delegation_contract is not None else ""),
            responsibility_owner=handle.responsibility_owner,
        ))

    async def _emit_finished(self, handle: AgentHandle) -> None:
        if handle.sidechain is not None:
            handle.sidechain.append_history(handle.loop.history, turn=handle.turns)
        await self.bus.emit(SubagentFinished(
            handle.id,
            handle.status,
            handle.preview(),
            delegation_id=(
                handle.delegation_contract.delegation_id
                if handle.delegation_contract is not None else ""),
            accepted=bool(
                handle.delegation_result is not None
                and handle.delegation_result.accepted),
            responsibility_owner=handle.responsibility_owner,
        ))

    async def _finalize_worktree(self, handle: AgentHandle) -> None:
        await self.worktrees.finalize(handle)

    async def _run(self, handle: AgentHandle, message: str) -> None:
        try:
            await run_handle(self, handle, message)
        finally:
            if handle.global_slot_acquired:
                self._spawn_slots.release()
                handle.global_slot_acquired = False

    def get(self, agent_id: str) -> Optional[AgentHandle]:
        return self._handles.get(agent_id)

    def is_running(self, handle: AgentHandle) -> bool:
        return handle.task_obj is not None and not handle.task_obj.done()

    async def send_input(
        self,
        agent_id: str,
        message: str,
        interrupt: bool = False,
        *,
        application_request: Optional[ApplicationRequest] = None,
    ) -> Tuple[AgentHandle, str]:
        handle = self._require_handle(agent_id)
        if handle.status == STATUS_KILLED:
            raise RuntimeError(f"子代理 {agent_id} 已被终止，不能追加任务")
        if not self.is_running(handle):
            handle.kill_requested = False
            handle.task = message
            handle.application_request = application_request
            handle.status = STATUS_RUNNING
            handle.result = None
            handle.error = None
            handle.delegation_result = None
            handle.finished_at = 0.0
            if handle.sidechain is not None:
                handle.sidechain.append_message("user", message, source="input")
            handle.task_obj = asyncio.create_task(self._run(handle, message))
            return handle, "started"
        if not interrupt:
            handle.queue.append(message)
            handle.queued_application_requests.append(application_request)
            if handle.sidechain is not None:
                handle.sidechain.append_message("user", message, source="queued")
            return handle, "queued"
        if handle.control.paused:
            handle.control.resume()
        running_task = handle.task_obj
        if running_task is None:
            raise RuntimeError(f"子代理 {agent_id} 缺少运行任务")
        running_task.cancel()
        await asyncio.gather(running_task, return_exceptions=True)
        sanitize_history(handle.loop.history)
        if handle.loop.history and handle.loop.history[-1].role == "user":
            first = ((handle.loop.history[-1].content or "")
                     + INTERRUPT_MARKER + message)
            handle.loop.history.pop()
        else:
            first = message
        handle.kill_requested = False
        handle.task = message
        handle.application_request = application_request
        handle.status = STATUS_RUNNING
        handle.result = None
        handle.error = None
        handle.delegation_result = None
        handle.finished_at = 0.0
        if handle.sidechain is not None:
            handle.sidechain.append_message("user", message, source="interrupt")
        handle.task_obj = asyncio.create_task(self._run(handle, first))
        return handle, "interrupted"

    async def retry(
        self,
        agent_id: str,
        *,
        definition: str = "",
    ) -> AgentHandle:
        handle = self._require_handle(agent_id)
        if self.is_running(handle):
            raise RuntimeError(f"子代理 {agent_id} 仍在运行，不能重试")
        contract = handle.delegation_contract
        if contract is None:
            raise RuntimeError(f"子代理 {agent_id} 缺少 DelegationContract")
        if handle.retry_count >= contract.retry_policy.max_retries:
            raise RuntimeError(f"子代理 {agent_id} 重试预算已耗尽")
        requested_definition = str(definition or "").strip()
        if (
            requested_definition
            and requested_definition != contract.capabilities.definition
        ):
            reassigned = contract.with_retry_assignment(requested_definition)
            replacement = await self.spawn(
                reassigned.task,
                contract=reassigned,
                parent_context=reassigned.parent,
            )
            replacement.retry_count = handle.retry_count + 1
            return replacement
        await self._spawn_slots.acquire()
        handle.global_slot_acquired = True
        handle.retry_count += 1
        handle.kill_requested = False
        handle.result = None
        handle.error = None
        handle.delegation_result = None
        handle.finished_at = 0.0
        handle.status = STATUS_RUNNING
        if handle.sidechain is not None:
            handle.sidechain.append_state(
                STATUS_RUNNING,
                reason=f"retry:{handle.retry_count}",
                turns=handle.turns,
            )
        handle.task_obj = asyncio.create_task(self._run(handle, handle.task))
        return handle

    async def record_delegation_result(
        self,
        handle: AgentHandle,
        result: DelegationResult,
    ) -> None:
        handle.delegation_result = result
        if handle.sidechain is not None:
            handle.sidechain.write({
                "type": "delegation_result",
                "result": result.to_dict(),
            })
        await self.bus.emit(SubagentStateChanged(
            handle.id,
            DelegationStatus(result.status).value,
            (
                "delegation accepted"
                if result.accepted else
                "delegation rejected: " + ", ".join(result.validation_errors)
            ),
            delegation_id=result.delegation_id,
            responsibility_owner=handle.responsibility_owner,
        ))

    async def pause(self, agent_id: str) -> AgentHandle:
        handle = self._require_handle(agent_id)
        if not self.is_running(handle):
            raise ValueError(f"子代理 {agent_id} 当前未运行，无法暂停")
        if handle.control.pause():
            await self._set_state(handle, STATUS_PAUSED, "用户暂停")
        return handle

    async def resume(self, agent_id: str) -> AgentHandle:
        handle = self._require_handle(agent_id)
        if not handle.control.paused:
            raise ValueError(f"子代理 {agent_id} 当前未暂停")
        handle.control.resume()
        await self._set_state(handle, STATUS_RUNNING, "用户恢复")
        return handle

    async def kill(self, agent_id: str) -> AgentHandle:
        handle = self._require_handle(agent_id)
        if not self.is_running(handle):
            raise ValueError(f"子代理 {agent_id} 当前未运行，无法终止")
        handle.kill_requested = True
        handle.control.resume()
        await self._set_state(handle, STATUS_KILLED, "用户终止")
        assert handle.task_obj is not None
        handle.task_obj.cancel()
        await asyncio.gather(handle.task_obj, return_exceptions=True)
        if handle.worktree is not None:
            await self.worktrees.finalize(handle)
            await self.worktrees.remove(handle)
        return handle

    async def wait(
        self,
        agent_id: str,
        timeout: Optional[float] = None,
    ) -> AgentHandle:
        handle = self._require_handle(agent_id)
        await self._wait_handles([handle], timeout)
        return handle

    async def wait_many(
        self,
        agent_ids: List[str],
        timeout: Optional[float] = None,
    ) -> List[AgentHandle]:
        handles = [self._require_handle(agent_id) for agent_id in agent_ids]
        await self._wait_handles(handles, timeout)
        return handles

    @staticmethod
    async def _wait_handles(
        handles: List[AgentHandle],
        timeout: Optional[float],
    ) -> None:
        tasks = [
            handle.task_obj for handle in handles
            if handle.task_obj is not None and not handle.task_obj.done()
        ]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)


__all__ = ["SubagentControlMixin"]
