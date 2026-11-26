"""Stable tuning boundaries between benchmarks and production runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from .change_admission import ChangeCandidate, EvaluationReport


@dataclass(frozen=True)
class BehaviorFixture:
    fixture_id: str
    task: str
    expected: Mapping[str, object] = field(default_factory=dict)
    workspace: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "fixture_id", str(self.fixture_id or "").strip())
        object.__setattr__(self, "task", str(self.task or "").strip())
        object.__setattr__(self, "workspace", str(self.workspace or "").strip())
        object.__setattr__(
            self, "expected", MappingProxyType(dict(self.expected)))
        object.__setattr__(
            self, "metadata", MappingProxyType(dict(self.metadata)))


class BehaviorFixturePort(Protocol):
    def list_fixtures(self) -> Sequence[BehaviorFixture]: ...

    def get_fixture(self, fixture_id: str) -> BehaviorFixture: ...


class EvaluationHarnessPort(Protocol):
    async def evaluate(
        self,
        fixture: BehaviorFixture,
        candidate: ChangeCandidate,
    ) -> EvaluationReport: ...


class TraceReplayPort(Protocol):
    async def replay(
        self,
        trace: Mapping[str, object],
        fixture: BehaviorFixture,
    ) -> Mapping[str, object]: ...


class VersionManifestPort(Protocol):
    def current(self) -> Mapping[str, object]: ...

    def diff(
        self,
        left_release_id: str,
        right_release_id: str,
    ) -> Mapping[str, object]: ...


__all__ = [
    "BehaviorFixture",
    "BehaviorFixturePort",
    "EvaluationHarnessPort",
    "TraceReplayPort",
    "VersionManifestPort",
]
