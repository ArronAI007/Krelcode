"""Runtime task control and recovery exceptions."""

from __future__ import annotations

from ...contracts.task_lifecycle import TaskRecoveryDecision


class RuntimeTaskTimeout(TimeoutError):
    """Raised when a task exhausts its wall-clock deadline."""


class RuntimeTaskConflict(RuntimeError):
    """Stable task/idempotency identity conflicts with an existing task."""


class RuntimeTaskRecoveryBlocked(RuntimeError):
    """Recovery preflight rejected task continuation."""

    def __init__(self, decision: TaskRecoveryDecision) -> None:
        self.decision = decision
        failed = ", ".join(
            item.code for item in decision.failed_checks) or "unknown"
        super().__init__(f"task recovery blocked: {failed}")


__all__ = [
    "RuntimeTaskConflict",
    "RuntimeTaskRecoveryBlocked",
    "RuntimeTaskTimeout",
]
