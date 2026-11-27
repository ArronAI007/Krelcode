"""Cohesive persistence ports for runtime facts and deliverables."""

from __future__ import annotations

from typing import Protocol

from .artifact import ArtifactStorePort
from .event_log import EventLogPort
from .state import StateStorePort


class StoragePorts(Protocol):
    """The three storage channels have different consistency contracts."""

    @property
    def event_log(self) -> EventLogPort: ...

    @property
    def state(self) -> StateStorePort: ...

    @property
    def artifacts(self) -> ArtifactStorePort: ...


__all__ = ["StoragePorts"]
