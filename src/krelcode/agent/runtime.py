"""Agent 执行推进层的统一门面。

外层（会话控制、headless、未来 API）通过 AgentRuntime 启动/停止任务、
提交 steering、运行单次 prompt，并在结束时统一释放后台资源。它不负责
终端渲染、斜杠命令或依赖装配；那些边界分别属于 UI、session 和 app。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from ..core.messages import SteeringInput
from ..tools.base import RuntimeSnapshot
from .loop import AgentLoop
from .query import QueryEngine, QuerySession


class AgentRuntime:
    """拥有一次进程内的 Agent 执行生命周期。"""

    def __init__(
        self,
        loop: AgentLoop,
        *,
        subagents=None,
        mcp_supervisor=None,
        mcp_reload=None,
        shell_jobs=None,
    ) -> None:
        self.loop = loop
        self.subagents = subagents
        self.mcp_supervisor = mcp_supervisor
        self.mcp_reload = mcp_reload
        self.shell_jobs = (
            shell_jobs
            if shell_jobs is not None
            else getattr(loop.tool_context, "shell_jobs", None)
        )
        self._task: Optional[asyncio.Task] = None
        self._query: Optional[QuerySession] = None
        self._closed = False
        self.query_engine = QueryEngine(loop)
        loop.runtime_snapshot_provider = self._runtime_snapshot

    def _runtime_snapshot(self, base: RuntimeSnapshot) -> RuntimeSnapshot:
        """每轮开始时刷新 MCP 健康状态，其余配置保持不可变快照。"""
        alive = frozenset()
        health_known = self.mcp_supervisor is not None
        if self.mcp_supervisor is not None:
            alive = frozenset(
                item["name"] for item in self.mcp_supervisor.status()
                if item.get("alive"))
        return replace(
            base,
            alive_mcp_servers=alive,
            mcp_health_known=health_known,
        )

    @property
    def bus(self):
        return self.loop.bus

    @property
    def workspace_root(self) -> Path:
        return self.loop.tool_context.workspace_root

    @property
    def tool_names(self) -> list[str]:
        return self.loop.registry.names()

    @property
    def active_task(self) -> Optional[asyncio.Task]:
        return self._task

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run_once(
        self,
        message: str,
        *,
        images=None,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[list[str]] = None,
    ) -> str:
        """执行一次完整 turn；供 headless/API 复用。"""
        query = self.start_query(
            message,
            images=images,
            tool_allowlist=tool_allowlist,
            reminder_ids=reminder_ids,
        )
        async for _ in query.events:
            pass
        return await query.wait()

    def bind_session(self, session_id: Optional[str]) -> None:
        """绑定持久会话 id；每轮由 AgentLoop 生成新的 turn snapshot。"""
        runtime = replace(self.loop.tool_context.runtime, session_id=session_id)
        self.loop.tool_context.runtime = runtime

    def start_query(
        self,
        message: str,
        *,
        images=None,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[list[str]] = None,
    ) -> QuerySession:
        """启动一条可消费 QueryEvent 流；同一 Runtime 不允许并发两轮。"""
        if self.is_running:
            raise RuntimeError("已有任务正在运行")
        self._query = self.query_engine.start(
            message,
            images=images,
            tool_allowlist=tool_allowlist,
            reminder_ids=reminder_ids,
        )
        self._task = self._query.task
        return self._query

    def start_turn(self, message: str, *, images=None) -> asyncio.Task:
        """兼容入口：启动后台 turn 并返回底层 task。"""
        return self.start_query(message, images=images).task

    def steer(self, message: str, *, images=None) -> str:
        """向运行中的 turn 注入用户补充要求及可选附件。"""
        text = message.strip()
        if not text:
            return ""
        item = SteeringInput(text, tuple(images or ())) if images else text
        self.loop.steering_queue.put_nowait(item)
        return text if len(text) <= 40 else text[:40] + "…"

    def drain_steering(self) -> list[Any]:
        """取走任务结束后未送达的 steering 项。"""
        pending: list[Any] = []
        while not self.loop.steering_queue.empty():
            try:
                pending.append(self.loop.steering_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return pending

    async def stop(self) -> bool:
        """取消当前 turn 并等待清理；无活动任务返回 False。"""
        task = self._task
        if task is None or task.done():
            self._task = None
            return False
        if self._query is not None:
            self.loop.abort_current_turn()
            await self._query.cancel()
        else:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._query = None
        self._task = None
        return True

    async def reload_mcp(self):
        if self.mcp_reload is None:
            return []
        return await self.mcp_reload()

    async def close(self) -> None:
        """释放子代理与 MCP 后台资源；可重复调用。"""
        if self._closed:
            return
        self._closed = True
        await self.stop()
        if self.shell_jobs is not None:
            await self.shell_jobs.shutdown()
        if self.subagents is not None:
            await self.subagents.shutdown()
        if self.mcp_supervisor is not None:
            await self.mcp_supervisor.close_all()
