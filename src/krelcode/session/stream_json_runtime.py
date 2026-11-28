"""Encoding helpers for loop runtime control events."""

from __future__ import annotations

from typing import Any, Dict

from ..core.events import (
    CompactionRehydrated,
    ContextManifestCreated,
    MiddlewareEvaluated,
    RecoveryDecided,
)


def middleware_payload(event: MiddlewareEvaluated) -> Dict[str, Any]:
    return {
        "type": "middleware",
        "subtype": "evaluated",
        "lifecycle_point": event.lifecycle_point,
        "action": event.action,
        "reason": event.reason,
        "evaluations": [dict(item) for item in event.evaluations],
        "applied_patches": [dict(item) for item in event.applied_patches],
    }


def recovery_payload(event: RecoveryDecided) -> Dict[str, Any]:
    return {
        "type": "recovery",
        "subtype": "decided",
        "failure_class": event.failure_class,
        "strategy": event.strategy,
        "reason": event.reason,
        "retryable": event.retryable,
        "requires_replan": event.requires_replan,
        "requires_user": event.requires_user,
        "attempt": event.attempt,
        "evidence": dict(event.evidence),
    }


def compaction_rehydrated_payload(
    event: CompactionRehydrated,
) -> Dict[str, Any]:
    return {
        "type": "compact",
        "subtype": "rehydrated",
        "trigger": event.trigger,
        "generation": event.generation,
        "report_digest": event.report_digest,
        "files": list(event.files),
        "skills": list(event.skills),
        "omitted_files": [dict(item) for item in event.omitted_files],
        "omitted_skills": [dict(item) for item in event.omitted_skills],
        "file_tokens": event.file_tokens,
        "skill_tokens": event.skill_tokens,
        "total_tokens": event.total_tokens,
    }


def context_manifest_payload(event: ContextManifestCreated) -> Dict[str, Any]:
    return {
        "type": "context",
        "subtype": "manifest",
        "manifest": dict(event.manifest),
    }


__all__ = [
    "compaction_rehydrated_payload",
    "context_manifest_payload",
    "middleware_payload",
    "recovery_payload",
]
