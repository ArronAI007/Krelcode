"""Child-agent run loop separated from manager lifecycle/control."""

from __future__ import annotations

import asyncio
from typing import Protocol

from krelcode.contracts.application import (
    ApplicationRequestFactory,
    TaskShape,
)
from krelcode.core.events import (
    EventBus,
    SubagentFinished,
    SubagentStateChanged,
)
from .contracts import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_KILLED,
    STATUS_RUNNING,
)
from .models import AgentHandle


class SubagentRunHost(Protocol):
    bus: EventBus

    async def _set_state(
        self,
        handle: AgentHandle,
        status: str,
        reason: str = "",
    ) -> None: ...

    async def _emit_finished(self, handle: AgentHandle) -> None: ...

    async def _finalize_worktree(self, handle: AgentHandle) -> None: ...


async def run_handle(
    manager: SubagentRunHost,
    handle: AgentHandle,
    message: str,
) -> None:
    while True:
        handle.result = None
        handle.error = None
        await manager._set_state(handle, STATUS_RUNNING, "执行任务")
        try:
            result = await handle.loop.run(
                message,
                application_request=handle.application_request,
                agent_contract=handle.agent_contract,
            )
            handle.result = result
            handle.turns += 1
            await manager._set_state(handle, STATUS_COMPLETED, "本轮完成")
        except asyncio.CancelledError:
            killed = handle.kill_requested
            handle.status = STATUS_KILLED if killed else STATUS_CANCELLED
            handle.error = "用户终止子代理" if killed else "任务被打断/取消"
            if handle.sidechain is not None:
                handle.sidechain.append_state(
                    handle.status, reason=handle.error, turns=handle.turns,
                    error=handle.error)
                handle.sidechain.append_history(
                    handle.loop.history, turn=handle.turns)
            await manager.bus.emit(SubagentStateChanged(
                handle.id, handle.status, handle.error))
            await manager.bus.emit(SubagentFinished(
                handle.id, handle.status, handle.preview()))
            raise
        except Exception as exc:
            handle.error = f"{type(exc).__name__}: {exc}"
            handle.turns += 1
            await manager._set_state(handle, STATUS_FAILED, handle.error)
        await manager._emit_finished(handle)

        if handle.queue:
            message = handle.queue.pop(0)
            handle.application_request = (
                handle.queued_application_requests.pop(0)
                if handle.queued_application_requests else None
            )
            if handle.application_request is None:
                handle.application_request = (
                    ApplicationRequestFactory.from_input_source(
                        "subagent",
                        goal=message or "[subagent task]",
                        task_shape=TaskShape.MULTI_AGENT,
                    ))
            if handle.agent_contract is not None:
                handle.agent_contract = handle.agent_contract.with_intent(
                    handle.application_request)
            if handle.sidechain is not None:
                handle.sidechain.append_message(
                    "user", message, source="queue")
            continue
        if handle.worktree is not None:
            await manager._finalize_worktree(handle)
        return


__all__ = ["run_handle"]
