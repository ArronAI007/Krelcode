"""Narrow safety ports shared by tools and policy layers."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class SandboxPort(Protocol):
    """The read-only capability surface tools need from a concrete Sandbox.

    Execution preparation remains owned by ``safety.sandbox``. Tool DTOs only
    need identity/capability attributes and must not import the concrete
    implementation.
    """

    @property
    def backend(self) -> str: ...

    @property
    def network(self) -> bool: ...

    def audit_command(
        self,
        command: str,
        cwd: Path,
        *,
        permission_profile: str | None = None,
    ) -> str | None:
        ...

    def prepare_shell(
        self,
        command: str,
        cwd: Path,
        *,
        permission_profile: str | None = None,
    ) -> object:
        ...


__all__ = ["SandboxPort"]
