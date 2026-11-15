"""Agent 执行推进层的统一门面。

外层（会话控制、headless、未来 API）通过 AgentRuntime 启动/停止任务、
提交 steering、运行单次 prompt，并在结束时统一释放后台资源。它不负责
终端渲染、斜杠命令或依赖装配；那些边界分别属于 UI、session 和 app。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from .loop import AgentLoop


class AgentRuntime:
    """拥有一次进程内的 Agent 执行生命周期。"""

    def __init__(
        self,
        loop: AgentLoop,
        *,
        subagents=None,
        mcp_supervisor=None,
        mcp_reload=None,
    ) -> None:
        self.loop = loop
        self.subagents = subagents
        self.mcp_supervisor = mcp_supervisor
        self.mcp_reload = mcp_reload
        self._task: Optional[asyncio.Task] = None
        self._closed = False

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

    async def run_once(self, message: str, *, images=None) -> str:
        """执行一次完整 turn；供 headless/API 复用。"""
        return await self.loop.run(message, images=images)

    def start_turn(self, message: str, *, images=None) -> asyncio.Task:
        """启动一个后台 turn；同一 Runtime 不允许并发两轮。"""
        if self.is_running:
            raise RuntimeError("已有任务正在运行")
        self._task = asyncio.create_task(self.loop.run(message, images=images))
        return self._task

    def steer(self, message: str) -> str:
        """向运行中的 turn 注入用户补充要求。"""
        text = message.strip()
        if not text:
            return ""
        self.loop.steering_queue.put_nowait(text)
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
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
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
        if self.subagents is not None:
            await self.subagents.shutdown()
        if self.mcp_supervisor is not None:
            await self.mcp_supervisor.close_all()
