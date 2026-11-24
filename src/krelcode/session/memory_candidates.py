"""Project-local Auto Memory candidate extraction and review workflow.

This compatibility module keeps the historical import path while the bounded
DTOs, queue persistence, extraction rules and event-bus integration live in
focused sibling modules.
"""

from __future__ import annotations

from .memory_candidate_coordinator import AutoMemoryCoordinator, AutoMemoryReport
from .memory_candidate_extractor import MemoryCandidateExtractor, MemoryTurnSnapshot
from .memory_candidate_models import (
    DEFAULT_AUTO_THRESHOLD,
    DEFAULT_SUGGEST_THRESHOLD,
    EVIDENCE_CONFIDENCE_BOOST,
    MAX_CANDIDATES,
    MAX_CANDIDATE_CHARS,
    MAX_EVIDENCE_CHARS,
    TRIGGER_LABELS,
    AutoMemoryMode,
    MemoryCandidate,
    MemoryCandidateDraft,
    MemoryCandidateStatus,
    MemoryCandidateTrigger,
    MemoryEvidence,
    coerce_auto_memory_mode,
)
from .memory_candidate_queue import MemoryCandidateQueue


__all__ = [
    "DEFAULT_AUTO_THRESHOLD",
    "DEFAULT_SUGGEST_THRESHOLD",
    "EVIDENCE_CONFIDENCE_BOOST",
    "MAX_CANDIDATES",
    "MAX_CANDIDATE_CHARS",
    "MAX_EVIDENCE_CHARS",
    "TRIGGER_LABELS",
    "AutoMemoryCoordinator",
    "AutoMemoryMode",
    "AutoMemoryReport",
    "MemoryCandidate",
    "MemoryCandidateDraft",
    "MemoryCandidateExtractor",
    "MemoryCandidateQueue",
    "MemoryCandidateStatus",
    "MemoryCandidateTrigger",
    "MemoryEvidence",
    "MemoryTurnSnapshot",
    "coerce_auto_memory_mode",
]
