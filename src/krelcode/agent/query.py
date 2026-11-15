"""查询引擎事件协议：把一次 Agent turn 包装为有序 QueryEvent 流。

REPL/headless/SDK 消费 ``QuerySession.events``；EventBus 仍作为持久化、用量、
审计等旁路订阅者的兼容通道。这样查询结果不再只有最终字符串，而是稳定的
增量事件协议。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from itertools import count
from typing import AsyncIterator, Optional

from ..core.events import AgentEvent
from .loop import AgentLoop


@dataclass(frozen=True)
class QueryEvent:
    """带 turn 身份和单调序号的统一查询事件。"""

    turn_id: str
    sequence: int
    event: AgentEvent


_DONE = object()
_CANCELLED = object()


class QuerySession:
    """一次正在执行的查询；拥有 task 与它的事件流。"""

    def __init__(
        self,
        turn_id: str,
        task: asyncio.Task,
        events: AsyncIterator[QueryEvent],
    ) -> None:
        self.turn_id = turn_id
        self.task = task
        self.events = events

    @property
    def done(self) -> bool:
        return self.task.done()

    async def wait(self) -> str:
        """等待查询结束并返回最终文本；不负责消费事件。"""
        return await self.task

    async def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class QueryEngine:
    """创建带 turn id、sequence 和完成/异常传播的查询会话。"""

    def __init__(self, loop: AgentLoop) -> None:
        self.loop = loop
        self._counter = count(1)

    def start(self, user_input: str, *, images=None) -> QuerySession:
        turn_id = f"turn-{next(self._counter)}"
        queue: asyncio.Queue = asyncio.Queue()
        sequence = count()
        unsubscribe_holder = []

        def on_event(event: AgentEvent) -> None:
            queue.put_nowait(QueryEvent(
                turn_id=turn_id,
                sequence=next(sequence),
                event=event,
            ))

        # 先订阅再启动 task，确保 RunStarted 不会丢失。
        unsubscribe = self.loop.bus.subscribe(on_event)
        unsubscribe_holder.append(unsubscribe)
        task = asyncio.create_task(
            self.loop.run_turn(user_input, images=images, turn_id=turn_id))

        def on_done(finished: asyncio.Task) -> None:
            try:
                unsubscribe()
            except Exception:
                pass
            if finished.cancelled():
                queue.put_nowait(_CANCELLED)
            elif finished.exception() is not None:
                queue.put_nowait(finished.exception())
            else:
                queue.put_nowait(_DONE)

        task.add_done_callback(on_done)
        events = self._iter_events(queue, task)
        session = QuerySession(turn_id=turn_id, task=task, events=events)
        return session

    async def _iter_events(
        self,
        queue: asyncio.Queue,
        task: asyncio.Task,
    ) -> AsyncIterator[QueryEvent]:
        try:
            while True:
                item = await queue.get()
                if item is _DONE or item is _CANCELLED:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
