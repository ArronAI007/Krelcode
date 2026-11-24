"""Content-free inspection of the latest real context assembly.

The command layer and TUI both need to explain which context sources contributed
to a provider request. This module is intentionally read-only: it never loads
rules, scans memory files or invokes context sources. It only projects snapshots
already retained by ``AgentLoop``.

The public ``ContextInspectionService`` facade composes focused snapshot,
Prompt-plan and runtime-reporting mixins while preserving the original import
path and method surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from .inspection_core import ContextInspectionCoreMixin
from .inspection_helpers import (
    _bounded,
    _int_value,
    _is_memory_fragment,
    _join_provenance,
    _layer_memory_metadata,
    _short_hash,
    _signed,
    _yes_no,
    _ids,
)
from .inspection_prompt import ContextInspectionPromptMixin
from .inspection_runtime import ContextInspectionRuntimeMixin


@dataclass(frozen=True)
class ContextInspectionService(
    ContextInspectionCoreMixin,
    ContextInspectionPromptMixin,
    ContextInspectionRuntimeMixin,
):
    """Project the last request's snapshot, plan and assembly into text."""

    loop: object
