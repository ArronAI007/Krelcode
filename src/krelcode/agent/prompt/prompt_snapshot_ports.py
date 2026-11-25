"""Minimal host boundary for applying legacy Prompt registry snapshots."""

from __future__ import annotations

from typing import Generic, Optional, Protocol, TypeVar

from krelcode.core.messages import Message



SnapshotT = TypeVar("SnapshotT")
RegistryT = TypeVar("RegistryT")


class PromptSnapshotHostPort(Protocol, Generic[SnapshotT, RegistryT]):
    history: list[Message]
    prompt_snapshot: Optional[SnapshotT]
    skill_base_prompt: str
    prompt_registry: Optional[RegistryT]



__all__ = ["PromptSnapshotHostPort", "RegistryT", "SnapshotT"]
