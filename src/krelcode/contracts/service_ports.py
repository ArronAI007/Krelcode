"""Small provider-neutral service ports exposed through ``ToolContext``.

The contracts package must stay below concrete runtime layers.  These protocols
therefore describe only the capabilities tools consume, while return values stay
structural and opaque whenever importing the owning implementation would reverse
the dependency direction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, Sequence, Tuple, runtime_checkable

from .safety import SandboxPort


@runtime_checkable
class ShellJobStatusPort(Protocol):
    @property
    def value(self) -> str: ...


@runtime_checkable
class ShellJobPort(Protocol):
    @property
    def job_id(self) -> str: ...

    @property
    def status(self) -> ShellJobStatusPort: ...

    @property
    def pid(self) -> Optional[int]: ...

    @property
    def cwd(self) -> str: ...

    @property
    def log_path(self) -> Path: ...

    @property
    def command(self) -> str: ...

    @property
    def exit_code(self) -> Optional[int]: ...

    @property
    def latest_sequence(self) -> int: ...

    @property
    def running(self) -> bool: ...


@runtime_checkable
class ShellJobPagePort(Protocol):
    @property
    def job(self) -> ShellJobPort: ...

    @property
    def next_sequence(self) -> int: ...

    @property
    def truncated(self) -> bool: ...

    def render(self) -> str: ...


@runtime_checkable
class ShellJobManagerPort(Protocol):
    async def start(
        self,
        command: str,
        *,
        cwd: Path,
        sandbox: Optional[SandboxPort] = None,
        permission_profile: str | None = None,
    ) -> ShellJobPort: ...

    async def output(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        wait_seconds: float = 0,
    ) -> ShellJobPagePort: ...

    async def kill(self, job_id: str) -> ShellJobPort: ...

    def list_jobs(self) -> Sequence[ShellJobPort]: ...


@runtime_checkable
class PromptFragmentPort(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def source(self) -> str: ...


@runtime_checkable
class PromptFragmentRegistryPort(Protocol):
    @property
    def generation(self) -> int: ...

    def get(self, fragment_id: str) -> Optional[PromptFragmentPort]: ...

    def list(
        self,
        *,
        source: str = "",
        include_disabled: bool = True,
    ) -> Sequence[PromptFragmentPort]: ...

    def is_enabled(self, fragment: PromptFragmentPort | str) -> bool: ...


@runtime_checkable
class PromptReleaseProposalPort(Protocol):
    def as_dict(self) -> dict[str, object]: ...


@runtime_checkable
class PromptReleaseStorePort(Protocol):
    def load_proposal(self, proposal_id: str) -> PromptReleaseProposalPort: ...

    def list_proposals(
        self,
        limit: int = 50,
    ) -> Sequence[PromptReleaseProposalPort]: ...


@runtime_checkable
class PromptReleasePipelinePort(Protocol):
    store: Optional[PromptReleaseStorePort]

    async def prepare_async(self, request: object) -> PromptReleaseProposalPort: ...


@runtime_checkable
class PromptBenchmarkServicesPort(Protocol):
    """Marker for benchmark dependencies injected into Prompt tooling.

    Consumers deliberately receive an opaque service bundle.  Concrete
    benchmark runners remain in the benchmark layer and are not imported by the
    contracts or tool layers.
    """


@runtime_checkable
class EnterprisePolicyPort(Protocol):
    def validate_provider(self, provider: str) -> None: ...

    def validate_cli(self, args: object) -> None: ...


@runtime_checkable
class PrincipalPort(Protocol):
    @property
    def subject(self) -> str: ...

    @property
    def issuer(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    @property
    def email_hash(self) -> str: ...

    @property
    def expires_at(self) -> float: ...

    @property
    def auth_method(self) -> str: ...


__all__ = [
    "EnterprisePolicyPort",
    "PrincipalPort",
    "PromptBenchmarkServicesPort",
    "PromptFragmentPort",
    "PromptFragmentRegistryPort",
    "PromptReleasePipelinePort",
    "PromptReleaseProposalPort",
    "PromptReleaseStorePort",
    "ShellJobManagerPort",
    "ShellJobPagePort",
    "ShellJobPort",
    "ShellJobStatusPort",
]
