"""Scheduling ports consumed by production runtimes."""

from __future__ import annotations

from typing import Protocol


class BackgroundSchedulerPort(Protocol):
    async def close(self) -> None: ...


class CompactSchedulerPort(BackgroundSchedulerPort, Protocol):
    async def on_event(self, event: object) -> None: ...


class MemoryDreamSchedulerPort(BackgroundSchedulerPort, Protocol):
    async def on_event(self, event: object) -> None: ...


__all__ = [
    "BackgroundSchedulerPort",
    "CompactSchedulerPort",
    "MemoryDreamSchedulerPort",
]
