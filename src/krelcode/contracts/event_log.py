"""Append-only Event Log contract with sequence and hash-chain metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class EventLogEntry:
    sequence: int
    event_id: str
    event_type: str
    session_id: str
    turn_id: str
    timestamp: float
    payload: Mapping[str, object] = field(default_factory=dict)
    previous_hash: str = ""
    digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class EventLogAppendResult:
    sequence: int
    event_id: str
    digest: str


class EventLogPort(Protocol):
    @property
    def latest_sequence(self) -> int: ...

    def append(
        self,
        payload: Mapping[str, object],
        *,
        event_type: str = "",
        session_id: str = "",
        turn_id: str = "",
    ) -> EventLogAppendResult: ...

    def read(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> Sequence[EventLogEntry]: ...

    def close(self) -> None: ...


__all__ = ["EventLogAppendResult", "EventLogEntry", "EventLogPort"]
