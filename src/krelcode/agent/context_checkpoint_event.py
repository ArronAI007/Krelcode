"""Translate a checkpoint record into the public event contract."""

from __future__ import annotations

from typing import Protocol, Sequence

from ..core.events import ContextCheckpointCreated


class _Plan(Protocol):
    @property
    def full_hash(self) -> str: ...


class CheckpointRecord(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def session_id(self) -> str: ...
    @property
    def turn_id(self) -> str: ...
    @property
    def phase(self) -> str: ...
    @property
    def history_hash(self) -> str: ...
    @property
    def file_hashes(self) -> Sequence[object]: ...
    @property
    def memory_refs(self) -> Sequence[object]: ...
    @property
    def compact_checkpoint_id(self) -> str: ...
    @property
    def context_plan(self) -> _Plan: ...


def checkpoint_created_event(checkpoint: CheckpointRecord) -> ContextCheckpointCreated:
    return ContextCheckpointCreated(
        checkpoint_id=checkpoint.id, session_id=checkpoint.session_id,
        turn_id=checkpoint.turn_id, phase=checkpoint.phase,
        history_hash=checkpoint.history_hash, file_count=len(checkpoint.file_hashes),
        memory_count=len(checkpoint.memory_refs),
        compact_checkpoint_id=checkpoint.compact_checkpoint_id,
        context_plan_hash=checkpoint.context_plan.full_hash,
    )


__all__ = ["CheckpointRecord", "checkpoint_created_event"]
