"""End-to-end runtime lifecycle identity.

It links the application request, runtime release, governance admission and
subsequent tuning candidate into one traceable chain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional

from .application import ApplicationRequest
from .change_admission import ChangeCandidate
from .governance import GovernanceDecision
from .runtime import RuntimeReleaseManifest


@dataclass(frozen=True)
class RuntimeLifecycleEnvelope:
    application_request: ApplicationRequest
    runtime_release: RuntimeReleaseManifest
    governance_decision: GovernanceDecision
    trace_id: str
    change_candidate: Optional[ChangeCandidate] = None
    next_runtime_release: Optional[RuntimeReleaseManifest] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        trace_id = str(self.trace_id or "").strip()
        if not trace_id:
            trace_id = self.governance_decision.audit.correlation_id
        if not trace_id:
            raise ValueError("RuntimeLifecycleEnvelope.trace_id 不能为空")
        object.__setattr__(self, "trace_id", trace_id)
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def complete(self) -> bool:
        return self.next_runtime_release is not None


__all__ = ["RuntimeLifecycleEnvelope"]
