"""Call-level identity separated from session and task lifetimes."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from .runtime import RuntimeIdentity


@dataclass(frozen=True)
class CallContext:
    call_id: str
    session_id: str = ""
    task_id: str = ""
    principal: RuntimeIdentity = field(default_factory=RuntimeIdentity)
    channel: str = ""
    input_refs: tuple[str, ...] = ()
    event_cursor: str = ""
    deadline_at: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "call_id", "session_id", "task_id", "channel", "event_cursor",
        ):
            object.__setattr__(
                self, name, str(getattr(self, name) or "").strip())
        if not self.call_id:
            raise ValueError("CallContext.call_id 不能为空")
        object.__setattr__(
            self,
            "input_refs",
            tuple(dict.fromkeys(
                str(item).strip()
                for item in self.input_refs
                if str(item).strip()
            )),
        )
        object.__setattr__(
            self,
            "deadline_at",
            float(self.deadline_at) if self.deadline_at is not None else None,
        )
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "call_id": self.call_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "principal_id": self.principal.principal_id,
            "tenant_id": self.principal.tenant_id,
            "organization_id": self.principal.organization_id,
            "channel": self.channel,
            "input_refs": list(self.input_refs),
            "event_cursor": self.event_cursor,
            "deadline_at": self.deadline_at,
            "metadata": dict(self.metadata),
        }


__all__ = ["CallContext"]
