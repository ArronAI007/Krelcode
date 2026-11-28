"""Provider-neutral external event subscription and resume contracts."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional, Protocol


@dataclass(frozen=True)
class ExternalEvent:
    event_id: str
    event_type: str
    resume_token: str
    payload: Mapping[str, object] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("event_id", "event_type", "resume_token"):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.resume_token:
            raise ValueError("ExternalEvent.resume_token 不能为空")
        object.__setattr__(
            self, "payload", MappingProxyType(dict(self.payload)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def as_record(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "resume_token": self.resume_token,
            "payload": dict(self.payload),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


class ExternalEventPort(Protocol):
    async def publish(self, event: ExternalEvent) -> int: ...

    async def wait(
        self,
        resume_token: str,
        *,
        event_type: str = "",
        timeout: Optional[float] = None,
    ) -> Optional[ExternalEvent]: ...

    def close(self) -> None: ...


__all__ = ["ExternalEvent", "ExternalEventPort"]
