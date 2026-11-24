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

    backend: str
    network: bool
    enabled: bool
    network_allowed: bool
    network_controlled: bool
    os_enforced: bool
    shell_write_scope: str
    file_write_scope: str
    protected_paths_enforced: bool
    sensitive_reads_protected: bool

    def audit_command(self, command: str, cwd: Path) -> str | None:
        ...

    def prepare_shell(self, command: str, cwd: Path) -> object:
        ...


__all__ = ["SandboxPort"]
