"""查询引擎事件协议：把一次 Harness turn 包装为有序 SessionEvent 流。

Task scheduling, leases and physical cancellation belong to a
``RuntimeTaskRuntimePort``.  This module only adapts the runtime handle to the
stable query event protocol consumed by REPL/headless/SDK.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from itertools import count
import time
from typing import AsyncIterator, Callable, Optional

from krelcode.core.events import AgentEvent
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
from krelcode.contracts.loop import WaitCondition
from krelcode.contracts.loop import WaitKind
from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.turn.runtime_adapter import as_turn_control


QueryEvent = SessionEvent


@dataclass(frozen=True)
class _QueryEnd:
    cancelled: bool = False
    error: Optional[BaseException] = None


class QuerySession:
    """一次正在执行的查询；拥有 task 与它的事件流。"""

    def __init__(
        self,
        handle: RuntimeTaskHandle,
        task: asyncio.Task[str],
        events: AsyncIterator[QueryEvent],
    ) -> None:
        self.runtime_handle = handle
        self.task = task
        self.events = events

    @property
    def task_id(self) -> str:
        return self.runtime_handle.task_id

    @property
    def turn_id(self) -> str:
        return self.runtime_handle.turn_id

    @property
    def state(self) -> RuntimeTaskState:
        return self.runtime_handle.state

    @property
    def done(self) -> bool:
        return self.task.done()

    async def wait(self) -> str:
        """等待查询结束并返回最终文本；不负责消费事件。"""
        return await self.task

    async def cancel(self) -> None:
        await self.runtime_handle.cancel()
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def pause(self) -> bool:
        return await self.runtime_handle.pause()

    async def resume(self) -> bool:
        return await self.runtime_handle.resume()


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

    def submit(
        self,
        task: RuntimeTask,
        executor: RuntimeTaskExecutorPort,
    ) -> RuntimeTaskHandle:
        task_id = f"inline-{next(self._counter)}"
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
            updated_at=time.time(),
        )
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
        updated = RuntimeTaskState(
            task_id=current.task_id,
            turn_id=current.turn_id,
            status=current.status,
            checkpoint=checkpoint,
            sequence=current.sequence + 1,
            error=current.error,
            attempts=current.attempts,
            deadline_at=current.deadline_at,
            started_at=current.started_at,
            paused_at=current.paused_at,
            resume_from_checkpoint_id=current.resume_from_checkpoint_id,
            wait_reason=current.wait_reason,
            wait_kind=current.wait_kind,
            resume_token=current.resume_token,
            waiting_since=current.waiting_since,
            resumed_at=current.resumed_at,
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
        updated = RuntimeTaskState(
            task_id=current.task_id,
            turn_id=current.turn_id,
            status=RuntimeTaskStatus.WAITING,
            checkpoint=current.checkpoint,
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
        updated = RuntimeTaskState(
            task_id=current.task_id,
            turn_id=current.turn_id,
            status=RuntimeTaskStatus.RUNNING,
            checkpoint=current.checkpoint,
            sequence=current.sequence + 1,
            wait_reason=current.wait_reason,
            wait_kind=current.wait_kind,
            resume_token=current.resume_token,
            waiting_since=current.waiting_since,
            resumed_at=time.time(),
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
        self._states[task_id] = RuntimeTaskState(
            task_id=current.task_id,
            turn_id=current.turn_id,
            status=status,
            checkpoint=current.checkpoint,
            sequence=current.sequence + 1,
            error=(
                f"{type(finished.exception()).__name__}: {finished.exception()}"
                if finished.exception() is not None else ""
            ),
            wait_reason=current.wait_reason,
            wait_kind=current.wait_kind,
            resume_token=current.resume_token,
            waiting_since=current.waiting_since,
            resumed_at=current.resumed_at,
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
        )
        queue: asyncio.Queue[QueryEvent | _QueryEnd] = asyncio.Queue(
            maxsize=self.queue_size)
        sequence = count()
        marker_tasks: list[asyncio.Task[None]] = []

        async def on_event(event: AgentEvent) -> None:
            # 有界队列形成对 AgentLoop 的真实背压：renderer/消费者跟不上时，
            # 查询推进层不会继续无限产生事件并吞掉内存。
            await queue.put(QueryEvent(
                turn_id=turn_id,
                sequence=next(sequence),
                event=as_session_event_payload(event),
            ))

        # 先订阅再启动 task，确保 RunStarted 不会丢失。
        unsubscribe: Callable[[], None] = self.turn.bus.subscribe(on_event)
        handle = self._task_runtime.submit(
            runtime_task,
            _LoopTurnExecutor(self.turn, self.continuation),
        )
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
        events = self._iter_events(queue, handle, task, marker_tasks)
        session = QuerySession(handle=handle, task=task, events=events)
        return session

    async def _iter_events(
        self,
        queue: asyncio.Queue[QueryEvent | _QueryEnd],
        handle: RuntimeTaskHandle,
        task: asyncio.Task[str],
        marker_tasks: list[asyncio.Task[None]],
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
            if not task.done():
                await handle.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for marker in marker_tasks:
                if not marker.done():
                    marker.cancel()
            if marker_tasks:
                await asyncio.gather(*marker_tasks, return_exceptions=True)
