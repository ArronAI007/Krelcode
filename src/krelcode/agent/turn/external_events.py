"""Local external-event broker used to resume WAITING_EVENT Loop states."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Optional

from krelcode.contracts.external_event import ExternalEvent, ExternalEventPort


class LocalExternalEventBroker(ExternalEventPort):
    def __init__(self, *, max_buffered_per_token: int = 32) -> None:
        self.max_buffered_per_token = max(1, int(max_buffered_per_token))
        self._buffers: dict[
            str, deque[ExternalEvent]] = defaultdict(deque)
        self._waiters: dict[
            str, list[tuple[str, asyncio.Future[ExternalEvent]]]
        ] = defaultdict(list)
        self._closed = False

    async def publish(self, event: ExternalEvent) -> int:
        if self._closed:
            raise RuntimeError("ExternalEventBroker 已关闭")
        waiters = self._waiters.get(event.resume_token, [])
        for index, (event_type, future) in enumerate(waiters):
            if event_type and event_type != event.event_type:
                continue
            waiters.pop(index)
            if not future.done():
                future.set_result(event)
            return 1
        buffer = self._buffers[event.resume_token]
        buffer.append(event)
        while len(buffer) > self.max_buffered_per_token:
            buffer.popleft()
        return 0

    async def wait(
        self,
        resume_token: str,
        *,
        event_type: str = "",
        timeout: Optional[float] = None,
    ) -> Optional[ExternalEvent]:
        if self._closed:
            raise RuntimeError("ExternalEventBroker 已关闭")
        token = str(resume_token or "").strip()
        if not token:
            raise ValueError("resume_token 不能为空")
        buffered = self._buffers.get(token)
        if buffered:
            if not event_type:
                return buffered.popleft()
            for index, event in enumerate(buffered):
                if event.event_type == event_type:
                    del buffered[index]
                    return event
        future: asyncio.Future[ExternalEvent] = (
            asyncio.get_running_loop().create_future())
        waiter = (str(event_type or "").strip(), future)
        self._waiters[token].append(waiter)
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=max(0.001, timeout))
        except asyncio.TimeoutError:
            return None
        finally:
            waiters = self._waiters.get(token)
            if waiters is not None:
                try:
                    waiters.remove(waiter)
                except ValueError:
                    pass
                if not waiters:
                    self._waiters.pop(token, None)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for waiters in self._waiters.values():
            for _event_type, future in waiters:
                if not future.done():
                    future.cancel()
        self._waiters.clear()
        self._buffers.clear()


__all__ = ["LocalExternalEventBroker"]
