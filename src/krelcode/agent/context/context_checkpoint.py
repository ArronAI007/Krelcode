"""Persistent full-context checkpoints for resume, fork and rewind.

This compatibility facade keeps the historical import path while checkpoint
models, persistence, state hashing, restore logic and event coordination live
in focused sibling modules.  Historical fingerprints remain evidence only;
resume always refreshes volatile workspace facts from live sources.
"""

from __future__ import annotations

from krelcode.agent.context.context_checkpoint_coordinator import ContextCheckpointCoordinator
from krelcode.agent.context.context_checkpoint_models import (
    CONTEXT_CHECKPOINT_DIRNAME,
    CONTEXT_CHECKPOINT_VERSION,
    ContextCheckpoint,
    ContextRestoreResult,
    FileHash,
    FileIntegrity,
    MemoryReference,
    WorkspaceFingerprint,
    collect_tracked_files,
    hash_tracked_files,
    verify_file_hashes,
)
from krelcode.agent.context.context_checkpoint_restore import restore_context_checkpoint
from krelcode.agent.context.context_checkpoint_store import ContextCheckpointStore
from krelcode.agent.context.context_checkpoint_workspace import workspace_fingerprint
from krelcode.agent.context.memory_context_render import render_memory_context


__all__ = [
    "CONTEXT_CHECKPOINT_DIRNAME",
    "CONTEXT_CHECKPOINT_VERSION",
    "ContextCheckpoint",
    "ContextCheckpointCoordinator",
    "ContextCheckpointStore",
    "ContextRestoreResult",
    "FileHash",
    "FileIntegrity",
    "MemoryReference",
    "WorkspaceFingerprint",
    "collect_tracked_files",
    "hash_tracked_files",
    "render_memory_context",
    "restore_context_checkpoint",
    "verify_file_hashes",
    "workspace_fingerprint",
]
