"""协作式子代理运行控制。

Pause 不取消 Task：gate 在每个 AgentLoop step 边界等待，当前模型调用或
工具执行完成后停在安全点。Kill 才通过 Task.cancel 终止整个子代理。
"""

from __future__ import annotations

import asyncio


class AgentControl:
    def __init__(self) -> None:
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> bool:
        """请求暂停；返回状态是否发生变化。"""
        changed = not self._paused
        self._paused = True
        self._resume_event.clear()
        return changed

    def resume(self) -> bool:
        """恢复运行；返回状态是否发生变化。"""
        changed = self._paused
        self._paused = False
        self._resume_event.set()
        return changed

    async def wait_if_paused(self) -> None:
        await self._resume_event.wait()
