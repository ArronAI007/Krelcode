"""Agent 执行推进层的统一门面。

外层（会话控制、headless、未来 API）通过 AgentRuntime 启动/停止任务、
提交 steering、运行单次 prompt，并在结束时统一释放后台资源。它不负责
终端渲染、斜杠命令或依赖装配；那些边界分别属于 UI、session 和 app。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import (
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    cast,
)

from krelcode.core.events import (
    AgentEvent,
    CompactCheckpointCreated,
    ContextCheckpointCreated,
    EventBus,
)
from krelcode.core.messages import ImageRef, SteeringInput
from krelcode.contracts.application import ApplicationRequest
from krelcode.contracts.acceptance import AcceptanceEvaluatorPort
from krelcode.contracts.governance import ControlPlanePort
from krelcode.contracts.runtime import (
    HarnessTurnControlPort,
    RuntimeCheckpointRef,
    RuntimeSnapshot,
    RuntimeResourceRegistryPort,
    RuntimeStateRef,
    RuntimeTaskRuntimePort,
    RuntimeTaskState,
)
from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.turn.query import QueryEngine, QuerySession
from krelcode.agent.turn.runtime_adapter import (
    AsyncCloseResource,
    InlineRuntimeResourceRegistry,
    as_turn_control,
)


class _SubagentRuntimePort(Protocol):
    async def shutdown(self) -> None: ...


class _MCPSupervisorPort(Protocol):
    def status(self) -> Sequence[Mapping[str, object]]: ...
    async def close_all(self) -> None: ...


class _ShellJobsRuntimePort(Protocol):
    async def shutdown(self) -> None: ...


class HarnessRuntimeFacade:
    """Runtime-facing facade for one semantic Agent session.

    Physical task scheduling and leases live in ``RuntimeTaskRuntimePort``
    implementations such as ``LocalTaskRuntime``.  This facade only translates
    Harness controls and Query events to the runtime boundary.
    """

    def __init__(
        self,
        loop: AgentLoop | HarnessTurnControlPort,
        *,
        subagents: Optional[_SubagentRuntimePort] = None,
        mcp_supervisor: Optional[_MCPSupervisorPort] = None,
        mcp_reload: Optional[Callable[[], Awaitable[Sequence[str]]]] = None,
        shell_jobs: Optional[_ShellJobsRuntimePort] = None,
        acceptance_evaluator: Optional[AcceptanceEvaluatorPort] = None,
        control_plane: Optional[ControlPlanePort] = None,
        task_runtime: Optional[RuntimeTaskRuntimePort] = None,
        resource_registry: Optional[RuntimeResourceRegistryPort] = None,
        strict_application_request: bool = False,
    ) -> None:
        self._turn = as_turn_control(loop)
        self.subagents = subagents
        self.mcp_supervisor = mcp_supervisor
        self.mcp_reload = mcp_reload
        self.shell_jobs = (
            shell_jobs
            if shell_jobs is not None
            else cast(Optional[_ShellJobsRuntimePort], self._turn.shell_jobs)
        )
        self._task: Optional[asyncio.Task[str]] = None
        self._query: Optional[QuerySession] = None
        self._session_id = ""
        self._closed = False
        self.last_close_errors: tuple[str, ...] = ()
        self.task_runtime = task_runtime
        self.resource_registry = (
            resource_registry or InlineRuntimeResourceRegistry())
        self.query_engine = QueryEngine(
            self._turn,
            task_runtime=task_runtime,
            require_application_request=strict_application_request,
        )
        self._turn.set_acceptance_evaluator(acceptance_evaluator)
        self._turn.set_control_plane(control_plane)
        self._turn.set_runtime_snapshot_provider(self._runtime_snapshot)
        self.resource_registry.register(AsyncCloseResource(
            "harness",
            self._turn.close_runtime_resources,
        ))
        if self.subagents is not None:
            self.resource_registry.register(AsyncCloseResource(
                "subagents",
                self.subagents.shutdown,
            ))
        if self.shell_jobs is not None:
            self.resource_registry.register(AsyncCloseResource(
                "shell_jobs",
                self.shell_jobs.shutdown,
            ))
        if self.mcp_supervisor is not None:
            self.resource_registry.register(AsyncCloseResource(
                "mcp",
                self.mcp_supervisor.close_all,
            ))
        self._unsubscribe_checkpoints = self.bus.subscribe(
            self._record_checkpoint_event)

    @property
    def loop(self) -> AgentLoop:
        """Legacy AgentLoop view retained for session command adapters."""
        loop = getattr(self._turn, "loop", None)
        if not isinstance(loop, AgentLoop):
            raise RuntimeError("当前 Harness 端口不暴露 AgentLoop 兼容视图")
        return loop

    @property
    def turn_host(self) -> HarnessTurnControlPort:
        return self._turn

    def _runtime_snapshot(self, base: RuntimeSnapshot) -> RuntimeSnapshot:
        """每轮开始时刷新 MCP 健康状态，其余配置保持不可变快照。"""
        alive: frozenset[str] = frozenset()
        health_known = self.mcp_supervisor is not None
        if self.mcp_supervisor is not None:
            alive = frozenset(
                str(item["name"]) for item in self.mcp_supervisor.status()
                if item.get("alive"))
        return replace(
            base,
            alive_mcp_servers=alive,
            mcp_health_known=health_known,
        )

    @property
    def bus(self) -> EventBus:
        return self._turn.bus

    @property
    def workspace_root(self) -> Path:
        return self._turn.workspace_root

    @property
    def tool_names(self) -> list[str]:
        return list(self._turn.tool_names)

    @property
    def active_task(self) -> Optional[asyncio.Task[str]]:
        return self._task

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_once(
        self,
        message: str,
        *,
        images: Optional[list[ImageRef]] = None,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[list[str]] = None,
        application_request: Optional[ApplicationRequest] = None,
    ) -> str:
        """执行一次完整 turn；供 headless/API 复用。"""
        query = self.start_query(
            message,
            images=images,
            tool_allowlist=tool_allowlist,
            reminder_ids=reminder_ids,
            application_request=application_request,
        )
        async for _ in query.events:
            pass
        return await query.wait()

    def bind_session(self, session_id: Optional[str]) -> None:
        """绑定持久会话 id；每轮由 AgentLoop 生成新的 turn snapshot。"""
        self._session_id = session_id or ""
        self._turn.bind_session(session_id)

    def _record_checkpoint_event(self, event: AgentEvent) -> None:
        """Project Harness checkpoint events into physical task state."""
        if self.task_runtime is None or self._query is None:
            return
        if isinstance(event, ContextCheckpointCreated):
            checkpoint = RuntimeCheckpointRef(
                checkpoint_id=event.checkpoint_id,
                kind="context",
                session_id=event.session_id or self._session_id,
                turn_id=event.turn_id or self._query.turn_id,
                task_id=self._query.task_id,
                digest=event.history_hash,
            )
        elif isinstance(event, CompactCheckpointCreated):
            checkpoint = RuntimeCheckpointRef(
                checkpoint_id=event.checkpoint_id,
                kind="compact",
                session_id=self._session_id,
                turn_id=self._query.turn_id,
                task_id=self._query.task_id,
                digest=event.summary_digest,
            )
        else:
            return
        self.task_runtime.record_checkpoint(
            self._query.task_id, checkpoint)

    def start_query(
        self,
        message: str,
        *,
        images: Optional[list[ImageRef]] = None,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[list[str]] = None,
        application_request: Optional[ApplicationRequest] = None,
    ) -> QuerySession:
        """启动一条可消费 QueryEvent 流；同一 Runtime 不允许并发两轮。"""
        if self.is_running:
            raise RuntimeError("已有任务正在运行")
        self._query = self.query_engine.start(
            message,
            images=images,
            tool_allowlist=tool_allowlist,
            reminder_ids=reminder_ids,
            application_request=application_request,
        )
        self._task = self._query.task
        return self._query

    def start_turn(
        self,
        message: str,
        *,
        images: Optional[list[ImageRef]] = None,
        application_request: Optional[ApplicationRequest] = None,
    ) -> asyncio.Task[str]:
        """兼容入口：启动后台 turn 并返回底层 task。"""
        return self.start_query(
            message,
            images=images,
            application_request=application_request,
        ).task

    def steer(
        self,
        message: str,
        *,
        images: Optional[list[ImageRef]] = None,
    ) -> str:
        """向运行中的 turn 注入用户补充要求及可选附件。"""
        text = message.strip()
        if not text:
            return ""
        item: SteeringInput | str = (
            SteeringInput(text, tuple(images or ())) if images else text
        )
        self._turn.submit_steering(item)
        return text if len(text) <= 40 else text[:40] + "…"

    def drain_steering(self) -> list[SteeringInput | str]:
        """取走任务结束后未送达的 steering 项。"""
        return self._turn.drain_steering()

    async def stop(self) -> bool:
        """取消当前 turn 并等待清理；无活动任务返回 False。"""
        task = self._task
        if task is None or task.done():
            self._task = None
            return False
        if self._query is not None:
            self._turn.abort_current_turn()
            await self._query.cancel()
        else:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._query = None
        self._task = None
        return True

    async def reload_mcp(self) -> Sequence[str]:
        if self.mcp_reload is None:
            return []
        return await self.mcp_reload()

    async def close(self) -> None:
        """释放子代理与 MCP 后台资源；可重复调用。"""
        if self._closed:
            return
        self._closed = True
        await self.stop()
        self._unsubscribe_checkpoints()
        self.last_close_errors = await self.resource_registry.close_all()

    def inspect_task(self, task_id: Optional[str] = None) -> Optional[RuntimeTaskState]:
        """Return physical state without exposing the underlying asyncio task."""
        if self.task_runtime is None:
            return None
        resolved = task_id or (
            self._query.task_id if self._query is not None else "")
        if not resolved:
            return None
        return self.task_runtime.inspect(resolved)

    def task_state_ref(
        self,
        task_id: Optional[str] = None,
    ) -> Optional[RuntimeStateRef]:
        """Return an opaque immutable reference to the current task state."""
        if self.task_runtime is None:
            return None
        resolved = task_id or (
            self._query.task_id if self._query is not None else "")
        if not resolved:
            return None
        return self.task_runtime.state_ref(resolved)

    def checkpoint_task(
        self,
        task_id: Optional[str] = None,
    ) -> Optional[RuntimeCheckpointRef]:
        """Return the task's latest durable checkpoint reference."""
        if self.task_runtime is None:
            return None
        resolved = task_id or (
            self._query.task_id if self._query is not None else "")
        if not resolved:
            return None
        return self.task_runtime.checkpoint(resolved)


# Public compatibility name retained while callers migrate to the explicit
# Harness/Production Runtime split.
AgentRuntime = HarnessRuntimeFacade


__all__ = ["AgentRuntime", "HarnessRuntimeFacade"]
