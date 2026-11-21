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


@dataclass(frozen=True)
class _QueryEnd:
    cancelled: bool = False
    error: Optional[BaseException] = None


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

    def __init__(self, loop: AgentLoop, *, queue_size: int = 256) -> None:
        self.loop = loop
        if queue_size < 1:
            raise ValueError("QueryEvent 队列容量必须大于 0")
        self.queue_size = queue_size
        self._counter = count(1)

    def start(
        self,
        user_input: str,
        *,
        images=None,
        tool_allowlist: Optional[list[str]] = None,
        reminder_ids: Optional[list[str]] = None,
    ) -> QuerySession:
        turn_id = f"turn-{next(self._counter)}"
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        sequence = count()
        unsubscribe_holder = []
        marker_tasks: list[asyncio.Task] = []

        async def on_event(event: AgentEvent) -> None:
            # 有界队列形成对 AgentLoop 的真实背压：renderer/消费者跟不上时，
            # 查询推进层不会继续无限产生事件并吞掉内存。
            await queue.put(QueryEvent(
                turn_id=turn_id,
                sequence=next(sequence),
                event=event,
            ))

        # 先订阅再启动 task，确保 RunStarted 不会丢失。
        unsubscribe = self.loop.bus.subscribe(on_event)
        unsubscribe_holder.append(unsubscribe)
        task = asyncio.create_task(
            self.loop.run_turn(
                user_input,
                images=images,
                turn_id=turn_id,
                tool_allowlist=tool_allowlist,
                reminder_ids=reminder_ids,
            ))

        def on_done(finished: asyncio.Task) -> None:
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
        events = self._iter_events(queue, task, marker_tasks)
        session = QuerySession(turn_id=turn_id, task=task, events=events)
        return session

    async def _iter_events(
        self,
        queue: asyncio.Queue,
        task: asyncio.Task,
        marker_tasks: list[asyncio.Task],
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
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for marker in marker_tasks:
                if not marker.done():
                    marker.cancel()
            if marker_tasks:
                await asyncio.gather(*marker_tasks, return_exceptions=True)
