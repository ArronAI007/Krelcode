"""Minimal host boundary for applying legacy Prompt registry snapshots."""

from __future__ import annotations

from typing import Any, Generic, Optional, Protocol, TypeVar

from krelcode.core.messages import Message



SnapshotT = TypeVar("SnapshotT", covariant=True)
RegistryT = TypeVar("RegistryT", covariant=True)


class PromptSnapshotHostPort(Protocol, Generic[SnapshotT, RegistryT]):
    @property
    def history(self) -> list[Message]: ...

    @property
    def prompt_snapshot(self) -> Optional[SnapshotT]: ...

    @property
    def skill_base_prompt(self) -> str: ...

    @property
    def prompt_registry(self) -> Optional[RegistryT]: ...

    def update_runtime_state(self, **values: Any) -> None: ...


__all__ = ["PromptSnapshotHostPort", "RegistryT", "SnapshotT"]
