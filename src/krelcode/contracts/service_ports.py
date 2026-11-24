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
    value: str


@runtime_checkable
class ShellJobPort(Protocol):
    job_id: str
    status: ShellJobStatusPort
    pid: Optional[int]
    cwd: str
    log_path: Path
    command: str
    exit_code: Optional[int]
    latest_sequence: int
    running: bool


@runtime_checkable
class ShellJobPagePort(Protocol):
    job: ShellJobPort
    next_sequence: int
    truncated: bool

    def render(self) -> str: ...


@runtime_checkable
class ShellJobManagerPort(Protocol):
    async def start(
        self,
        command: str,
        *,
        cwd: Path,
        sandbox: Optional[SandboxPort] = None,
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
    id: str
    source: str


@runtime_checkable
class PromptFragmentRegistryPort(Protocol):
    generation: int

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
    subject: str
    issuer: str
    display_name: str
    email_hash: str
    expires_at: float
    auth_method: str


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
