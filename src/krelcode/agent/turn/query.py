"""查询引擎事件协议：把一次 Harness turn 包装为有序 SessionEvent 流。

Task scheduling, leases and physical cancellation belong to a
``RuntimeTaskRuntimePort``.  This module only adapts the runtime handle to the
stable query event protocol consumed by REPL/headless/SDK.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, fields, is_dataclass, replace
from itertools import count
import time
import uuid
from typing import AsyncIterator, Callable, Mapping, Optional, Sequence

from krelcode.core.events import (
    AgentEvent,
    LLMStreamDelta,
    LLMThinkingDelta,
    ToolOutputDelta,
)
from krelcode.core.messages import ImageRef
from krelcode.core.session_events import SessionEvent, as_session_event_payload
from krelcode.contracts.application import (
    ApplicationRequest,
    ApplicationRequestFactory,
)
from krelcode.contracts.agent import AgentContract, AgentContractFactory
from krelcode.contracts.runtime import (
    HarnessReleaseIdentity,
    RuntimeCheckpointRef,
    HarnessTurnControlPort,
    RuntimeIdentity,
    RuntimeTask,
    RuntimeTaskExecutorPort,
    RuntimeTaskHandle,
    RuntimeTaskRuntimePort,
    RuntimeContinuationPort,
    RuntimeRetryPolicy,
    RuntimeSandboxBinding,
    RuntimeStateRef,
    RuntimeTaskState,
    RuntimeTaskStatus,
)
from krelcode.contracts.call import CallContext
from krelcode.contracts.task_lifecycle import (
    SideEffectStatus,
    TaskAttachment,
    TaskCancelMode,
    TaskContinuation,
    TaskEventCursor,
    TaskResultLocation,
    TaskSideEffectRecord,
)
from krelcode.contracts.loop import WaitCondition
from krelcode.contracts.loop import WaitKind
from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.turn.runtime_adapter import as_turn_control
from krelcode.agent.turn.query_session import DetachControl, QuerySession


QueryEvent = SessionEvent


@dataclass(frozen=True)
class _QueryEnd:
    cancelled: bool = False
    error: Optional[BaseException] = None


class _LoopTurnExecutor(RuntimeTaskExecutorPort):
    """Adapt the semantic Agent turn to a runtime task executor."""

    def __init__(
        self,
        turn: HarnessTurnControlPort,
        continuation: Optional[RuntimeContinuationPort] = None,
    ) -> None:
        self.turn = turn
        self.continuation = continuation

    async def execute(self, task: RuntimeTask) -> str:
        self.turn.bind_runtime_identity(task.identity)
        self.turn.validate_harness_release(task.harness_release)
        self.turn.validate_sandbox_binding(task.sandbox)
        if task.resume_from is not None:
            if self.continuation is None:
                raise RuntimeError(
                    "Runtime task 要求恢复 checkpoint，但 ContinuationPort 未配置")
            self.continuation.resume(task.resume_from)
        return await self.turn.run_turn(
            task.message,
            images=list(task.images) or None,
            turn_id=task.turn_id,
            tool_allowlist=(
                list(task.tool_allowlist)
                if task.tool_allowlist is not None else None
            ),
            reminder_ids=list(task.reminder_ids) or None,
            application_request=task.application,
            agent_contract=task.agent_contract,
        )


class _InlineRuntimeHandle(RuntimeTaskHandle):
    """Compatibility handle when callers construct QueryEngine directly."""

    def __init__(
        self,
        runtime: "_InlineTaskRuntime",
        task_id: str,
        turn_id: str,
        task: asyncio.Task[str],
    ) -> None:
        self._runtime = runtime
        self._task_id = task_id
        self._turn_id = turn_id
        self._task = task

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def turn_id(self) -> str:
        return self._turn_id

    @property
    def state(self) -> RuntimeTaskState:
        return self._runtime.inspect(self._task_id)

    @property
    def done(self) -> bool:
        return self._task.done()

    async def wait(self) -> str:
        return await self._task

    async def cancel(self) -> None:
        await self._runtime.cancel(self._task_id)

    async def pause(self) -> bool:
        return False

    async def resume(self) -> bool:
        return False


class _InlineTaskRuntime:
    """Small fallback for direct QueryEngine users.

    Production composition injects ``LocalTaskRuntime`` instead.  The fallback
    keeps the historical ``QueryEngine(loop)`` API working without making the
    production runtime path own AgentLoop internals.
    """

    def __init__(self) -> None:
        self._counter = count(1)
        self._states: dict[str, RuntimeTaskState] = {}
        self._tasks: dict[str, asyncio.Task[str]] = {}
        self._events: dict[str, list[Mapping[str, object]]] = {}

    def submit(
        self,
        task: RuntimeTask,
        executor: RuntimeTaskExecutorPort,
    ) -> RuntimeTaskHandle:
        task_id = task.task_id or f"inline-{next(self._counter)}"
        asyncio_task = asyncio.get_running_loop().create_task(
            executor.execute(task),
            name=f"krelcode-inline-{task_id}",
        )
        self._tasks[task_id] = asyncio_task
        self._states[task_id] = RuntimeTaskState(
            task_id=task_id,
            turn_id=task.turn_id,
            status=RuntimeTaskStatus.RUNNING,
            sequence=1,
            parent_task_id=task.parent_task_id,
            root_task_id=task.root_task_id or task_id,
            creator=task.creator or task.identity.principal_id,
            executor=task.executor or "inline",
            continuation=task.continuation,
            cancel_mode=task.cancel_mode,
            updated_at=time.time(),
        )
        self._events[task_id] = []
        asyncio_task.add_done_callback(
            lambda finished: self._finish(task_id, finished))
        return _InlineRuntimeHandle(
            self,
            task_id=task_id,
            turn_id=task.turn_id,
            task=asyncio_task,
        )

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def pause(self, task_id: str) -> bool:
        return False

    async def resume(self, task_id: str) -> bool:
        return False

    def inspect(self, task_id: str) -> RuntimeTaskState:
        return self._states[task_id]

    def state_ref(self, task_id: str) -> RuntimeStateRef:
        state = self.inspect(task_id)
        return RuntimeStateRef(
            state_id=f"{task_id}:{state.sequence}",
            task_id=task_id,
            turn_id=state.turn_id,
            sequence=state.sequence,
            updated_at=state.updated_at,
            checkpoint=state.checkpoint,
        )

    def checkpoint(self, task_id: str) -> Optional[RuntimeCheckpointRef]:
        return self.inspect(task_id).checkpoint

    def record_checkpoint(
        self,
        task_id: str,
        checkpoint: RuntimeCheckpointRef,
    ) -> RuntimeTaskState:
        current = self.inspect(task_id)
        updated = replace(
            current,
            checkpoint=checkpoint,
            sequence=current.sequence + 1,
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        return updated

    def record_waiting(
        self,
        task_id: str,
        condition: WaitCondition,
    ) -> RuntimeTaskState:
        current = self.inspect(task_id)
        updated = replace(
            current,
            status=RuntimeTaskStatus.WAITING,
            sequence=current.sequence + 1,
            wait_reason=condition.reason,
            wait_kind=WaitKind(condition.kind).value,
            resume_token=condition.resume_token or condition.request_id,
            waiting_since=time.time(),
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        return updated

    def record_resumed(self, task_id: str) -> RuntimeTaskState:
        current = self.inspect(task_id)
        if current.status is not RuntimeTaskStatus.WAITING:
            return current
        updated = replace(
            current,
            status=RuntimeTaskStatus.RUNNING,
            sequence=current.sequence + 1,
            resumed_at=time.time(),
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        return updated

    async def recover(
        self,
        task_id: str,
        executor: RuntimeTaskExecutorPort,
    ) -> RuntimeTaskHandle:
        raise RuntimeError("inline runtime 不支持 durable recovery")

    def record_event(
        self,
        task_id: str,
        payload: Mapping[str, object],
        *,
        event_type: str = "",
    ) -> TaskEventCursor:
        current = self.inspect(task_id)
        sequence = current.event_cursor.sequence + 1
        cursor = TaskEventCursor(
            sequence=sequence,
            event_id=f"inline-{task_id}-{sequence}",
            updated_at=time.time(),
        )
        self._events.setdefault(task_id, []).append({
            "task_id": task_id,
            "sequence": sequence,
            "event_id": cursor.event_id,
            "event_type": event_type,
            "payload": dict(payload),
            "timestamp": cursor.updated_at,
        })
        self._states[task_id] = replace(
            current,
            event_cursor=cursor,
            sequence=current.sequence + 1,
            updated_at=time.time(),
        )
        return cursor

    def read_events(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1_000,
    ) -> tuple[Mapping[str, object], ...]:
        return tuple(
            item for item in self._events.get(task_id, ())
            if int(str(item.get("sequence") or 0)) > after_sequence
        )[:limit]

    def attach(self, task_id: str) -> TaskAttachment:
        state = self.inspect(task_id)
        return TaskAttachment(
            task_id=task_id,
            cursor=state.event_cursor,
            status=state.status.value,
            result=state.result,
            error=state.error_info,
            continuation=state.continuation,
            side_effects=state.side_effects,
            recovery_decision=state.recovery_decision,
        )

    def record_result(
        self,
        task_id: str,
        result: TaskResultLocation,
    ) -> RuntimeTaskState:
        current = self.inspect(task_id)
        updated = replace(
            current,
            result=result,
            sequence=current.sequence + 1,
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        return updated

    def record_side_effect(
        self,
        task_id: str,
        side_effect: TaskSideEffectRecord,
    ) -> RuntimeTaskState:
        current = self.inspect(task_id)
        effects = tuple(
            side_effect if item.effect_id == side_effect.effect_id else item
            for item in current.side_effects
        )
        if not any(
            item.effect_id == side_effect.effect_id
            for item in current.side_effects
        ):
            effects = (*current.side_effects, side_effect)
        updated = replace(
            current,
            side_effects=effects,
            sequence=current.sequence + 1,
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        return updated

    def update_side_effect(
        self,
        task_id: str,
        effect_id: str,
        status: SideEffectStatus | str,
        *,
        compensation: str = "",
        evidence: Optional[Mapping[str, object]] = None,
        preflight: Optional[Mapping[str, object]] = None,
    ) -> RuntimeTaskState:
        current = self.inspect(task_id)
        effects = tuple(
            replace(
                item,
                status=SideEffectStatus(status),
                compensation=compensation or item.compensation,
                preflight={
                    **dict(item.preflight),
                    **dict(preflight or {}),
                },
                evidence={
                    **dict(item.evidence),
                    **dict(evidence or {}),
                },
            )
            if item.effect_id == effect_id else item
            for item in current.side_effects
        )
        if not any(item.effect_id == effect_id for item in effects):
            raise KeyError(f"未知 side effect: {effect_id}")
        updated = replace(
            current,
            side_effects=effects,
            sequence=current.sequence + 1,
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        return updated

    def record_continuation(
        self,
        task_id: str,
        continuation: TaskContinuation,
    ) -> RuntimeTaskState:
        current = self.inspect(task_id)
        updated = replace(
            current,
            continuation=continuation,
            sequence=current.sequence + 1,
            updated_at=time.time(),
        )
        self._states[task_id] = updated
        return updated

    def _finish(self, task_id: str, finished: asyncio.Task[str]) -> None:
        current = self._states[task_id]
        if finished.cancelled():
            status = RuntimeTaskStatus.CANCELLED
        elif finished.exception() is not None:
            status = RuntimeTaskStatus.FAILED
        else:
            status = RuntimeTaskStatus.COMPLETED
        self._states[task_id] = replace(
            current,
            status=status,
            sequence=current.sequence + 1,
            error=(
                f"{type(finished.exception()).__name__}: {finished.exception()}"
                if finished.exception() is not None else ""
            ),
            updated_at=time.time(),
        )


class QueryEngine:
    """创建带 turn id、sequence 和完成/异常传播的查询会话。"""

    def __init__(
        self,
        loop: AgentLoop | HarnessTurnControlPort,
        *,
        queue_size: int = 256,
        task_runtime: Optional[RuntimeTaskRuntimePort] = None,
        require_application_request: bool = False,
        runtime_identity: Optional[RuntimeIdentity] = None,
        harness_release: Optional[HarnessReleaseIdentity] = None,
        retry_policy: Optional[RuntimeRetryPolicy] = None,
        sandbox_binding: Optional[RuntimeSandboxBinding] = None,
        continuation: Optional[RuntimeContinuationPort] = None,
    ) -> None:
        self.turn = as_turn_control(loop)
        if queue_size < 1:
            raise ValueError("QueryEvent 队列容量必须大于 0")
        self.queue_size = queue_size
        self._counter = count(1)
        self._task_runtime = task_runtime or _InlineTaskRuntime()
        self.require_application_request = require_application_request
        self.runtime_identity = runtime_identity or RuntimeIdentity()
        self.harness_release = harness_release or HarnessReleaseIdentity()
        self.retry_policy = retry_policy or RuntimeRetryPolicy()
        self.sandbox_binding = sandbox_binding or RuntimeSandboxBinding()
        self.continuation = continuation

    def start(
        self,
        user_input: str,
        *,
        images: Optional[list[ImageRef]] = None,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[list[str]] = None,
        application_request: Optional[ApplicationRequest] = None,
        agent_contract: Optional[AgentContract] = None,
        task_id: str = "",
        parent_task_id: str = "",
        idempotency_key: str = "",
        call_id: str = "",
        continuation: Optional[TaskContinuation] = None,
        cancel_mode: TaskCancelMode | str = TaskCancelMode.COOPERATIVE,
    ) -> QuerySession:
        turn_id = f"turn-{next(self._counter)}"
        if (
            application_request is None
            and agent_contract is None
            and self.require_application_request
        ):
            raise ValueError(
                "生产 QueryEngine 必须显式提供 ApplicationRequest "
                "或 AgentContract")
        if (
            agent_contract is not None
            and application_request is not None
            and agent_contract.intent != application_request
        ):
            raise ValueError(
                "ApplicationRequest 与 AgentContract.intent 不一致")
        resolved_application = (
            agent_contract.intent if agent_contract is not None
            else application_request
        ) or ApplicationRequestFactory.from_input_source(
            "headless",
            goal=user_input or "[image attachment]",
        )
        resolved_contract = agent_contract or AgentContractFactory.from_application(
            resolved_application,
            principal=self.runtime_identity,
            harness_release=self.harness_release,
            sandbox=self.sandbox_binding,
            tool_allowlist=tool_allowlist,
        )
        input_errors = resolved_contract.validate_inputs(
            user_input, tuple(images or ()))
        if input_errors:
            raise ValueError("AgentContract 输入校验失败: " + "; ".join(input_errors))
        resolved_tool_allowlist = (
            tuple(tool_allowlist)
            if tool_allowlist is not None
            else resolved_contract.authority.allowed_tools
        )
        resolved_task_id = (
            str(task_id or "").strip()
            or f"task-{uuid.uuid4().hex[:16]}"
        )
        resolved_call_id = (
            str(call_id or idempotency_key).strip()
            or f"call-{uuid.uuid4().hex[:16]}"
        )
        call_context = CallContext(
            call_id=resolved_call_id,
            task_id=resolved_task_id,
            principal=resolved_contract.principal,
            channel=resolved_application.trigger.value,
            metadata={"entrypoint": resolved_application.trigger.value},
        )
        runtime_task = RuntimeTask(
            turn_id=turn_id,
            message=user_input,
            application=resolved_application,
            images=tuple(images or ()),
            tool_allowlist=resolved_tool_allowlist,
            reminder_ids=tuple(reminder_ids or ()),
            identity=resolved_contract.principal,
            harness_release=resolved_contract.harness_release,
            retry_policy=self.retry_policy,
            sandbox=resolved_contract.sandbox,
            metadata={"agent_contract_id": resolved_contract.contract_id},
            agent_contract=resolved_contract,
            task_id=resolved_task_id,
            parent_task_id=parent_task_id,
            idempotency_key=idempotency_key,
            creator=resolved_contract.principal.principal_id,
            continuation=continuation,
            cancel_mode=cancel_mode,
        )
        queue: asyncio.Queue[QueryEvent | _QueryEnd] = asyncio.Queue(
            maxsize=self.queue_size)
        sequence = count()
        marker_tasks: list[asyncio.Task[None]] = []
        detach_control = DetachControl()
        task_id_ref: list[str] = [resolved_task_id]
        parent_task_id_ref: list[str] = [parent_task_id]
        pending_journal: list[tuple[int, QueryEvent]] = []

        def journal_event(task_id: str, item: QueryEvent) -> None:
            if isinstance(item.event, (
                LLMStreamDelta, LLMThinkingDelta, ToolOutputDelta,
            )):
                return
            self._task_runtime.record_event(
                task_id,
                {
                    "session_event": _jsonable(item.event),
                    "session_id": item.session_id,
                    "call_id": item.call_id,
                    "timestamp": item.timestamp,
                },
                event_type=item.event_type,
            )

        async def on_event(event: AgentEvent) -> None:
            # 有界队列形成对 AgentLoop 的真实背压：renderer/消费者跟不上时，
            # 查询推进层不会继续无限产生事件并吞掉内存。
            item = QueryEvent(
                turn_id=turn_id,
                sequence=next(sequence),
                event=as_session_event_payload(event),
                task_id=(
                    task_id_ref[0] if task_id_ref else ""),
                parent_task_id=(
                    parent_task_id_ref[0]
                    if parent_task_id_ref else ""),
                call_id=call_context.call_id,
            )
            if task_id_ref:
                journal_event(task_id_ref[0], item)
            else:
                pending_journal.append((item.sequence, item))
            if not detach_control.detached:
                await queue.put(item)

        # 先订阅再启动 task，确保 RunStarted 不会丢失。
        unsubscribe: Callable[[], None] = self.turn.bus.subscribe(on_event)
        handle = self._task_runtime.submit(
            runtime_task,
            _LoopTurnExecutor(self.turn, self.continuation),
        )
        task_id_ref[0] = handle.task_id
        parent_task_id_ref[0] = handle.state.parent_task_id
        for _sequence, pending in pending_journal:
            journal_event(handle.task_id, pending)
        pending_journal.clear()
        task = asyncio.create_task(handle.wait())

        def on_done(finished: asyncio.Task[str]) -> None:
            try:
                unsubscribe()
            except Exception:
                pass
            if finished.cancelled():
                end = _QueryEnd(cancelled=True)
            else:
                end = _QueryEnd(error=finished.exception())
            # done callback 不能 await。单独排队结束标记；它严格排在所有已
            # 产生事件之后，消费者由此先 drain 再传播取消/异常。
            try:
                marker_tasks.append(asyncio.create_task(queue.put(end)))
            except RuntimeError:
                pass  # 事件循环已关闭

        task.add_done_callback(on_done)
        events = self._iter_events(
            queue, handle, task, marker_tasks, detach_control)
        session = QuerySession(
            handle=handle,
            task=task,
            events=events,
            detach_control=detach_control,
            read_events=lambda after, limit: self._task_runtime.read_events(
                handle.task_id,
                after_sequence=after,
                limit=limit,
            ),
            attach=self._task_runtime.attach,
            call_context=call_context,
        )
        return session

    async def recover(
        self,
        task_id: str,
        *,
        detach: bool = True,
    ) -> QuerySession:
        """Recover one interrupted/paused task and journal its new events."""
        handle = await self._task_runtime.recover(
            task_id,
            _LoopTurnExecutor(self.turn, self.continuation),
        )
        recovered_call = CallContext(
            call_id=f"call-recover-{uuid.uuid4().hex[:16]}",
            task_id=handle.task_id,
            principal=self.runtime_identity,
            channel="recover",
        )
        queue: asyncio.Queue[QueryEvent | _QueryEnd] = asyncio.Queue(
            maxsize=self.queue_size)
        detach_control = DetachControl()
        detach_control.detached = bool(detach)
        sequence = count()
        marker_tasks: list[asyncio.Task[None]] = []

        async def on_event(event: AgentEvent) -> None:
            item = QueryEvent(
                turn_id=handle.turn_id,
                sequence=next(sequence),
                event=as_session_event_payload(event),
                task_id=handle.task_id,
                parent_task_id=handle.state.parent_task_id,
                call_id=recovered_call.call_id,
            )
            if not isinstance(item.event, (
                LLMStreamDelta, LLMThinkingDelta, ToolOutputDelta,
            )):
                self._task_runtime.record_event(
                    handle.task_id,
                    {
                        "session_event": _jsonable(item.event),
                        "session_id": item.session_id,
                        "call_id": item.call_id,
                        "timestamp": item.timestamp,
                    },
                    event_type=item.event_type,
                )
            if not detach_control.detached:
                await queue.put(item)

        unsubscribe = self.turn.bus.subscribe(on_event)
        task = asyncio.create_task(handle.wait())

        def on_done(finished: asyncio.Task[str]) -> None:
            try:
                unsubscribe()
            except Exception:
                pass
            end = (
                _QueryEnd(cancelled=True) if finished.cancelled()
                else _QueryEnd(error=finished.exception())
            )
            try:
                marker_tasks.append(asyncio.create_task(queue.put(end)))
            except RuntimeError:
                pass

        task.add_done_callback(on_done)
        events = self._iter_events(
            queue, handle, task, marker_tasks, detach_control)
        return QuerySession(
            handle=handle,
            task=task,
            events=events,
            detach_control=detach_control,
            read_events=lambda after, limit: self._task_runtime.read_events(
                handle.task_id,
                after_sequence=after,
                limit=limit,
            ),
            attach=self._task_runtime.attach,
            call_context=recovered_call,
        )

    async def _iter_events(
        self,
        queue: asyncio.Queue[QueryEvent | _QueryEnd],
        handle: RuntimeTaskHandle,
        task: asyncio.Task[str],
        marker_tasks: list[asyncio.Task[None]],
        detach_control: DetachControl,
    ) -> AsyncIterator[QueryEvent]:
        try:
            while True:
                item = await queue.get()
                if isinstance(item, _QueryEnd):
                    if item.error is not None:
                        raise item.error
                    return
                yield item
        finally:
            if not task.done() and not detach_control.detached:
                await handle.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for marker in marker_tasks:
                if not marker.done():
                    marker.cancel()
            if marker_tasks:
                await asyncio.gather(*marker_tasks, return_exceptions=True)


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = ["QueryEngine", "QueryEvent", "QuerySession"]
