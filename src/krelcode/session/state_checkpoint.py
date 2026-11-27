"""Session compatibility facade for transactional state snapshots."""

from __future__ import annotations

from ..storage.state_store import (
    STATE_LEGACY_VERSION,
    STATE_TRANSACTION_VERSION,
    LocalTransactionalStateStore,
)

# Kept for callers that persisted or inspected the pre-transaction schema.
STATE_CHECKPOINT_VERSION = STATE_LEGACY_VERSION


class LocalStateCheckpointStore(LocalTransactionalStateStore):
    """Historical import path for the transactional state store."""


__all__ = [
    "LocalStateCheckpointStore",
    "STATE_CHECKPOINT_VERSION",
    "STATE_LEGACY_VERSION",
    "STATE_TRANSACTION_VERSION",
]
