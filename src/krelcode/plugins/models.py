"""Plugin-facing records and lifecycle result DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

MAX_INSTALL_FILES = 5_000
MAX_INSTALL_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class PluginIssue:
    path: Path
    message: str


@dataclass(frozen=True)
class PluginRecord:
    name: str
    status: str
    path: Path
    version: str = ""
    description: str = ""
    entrypoint: str = ""
    tools_count: int = 0
    error: str = ""
    scope: str = ""
    manifest_version: int = 1
    skills_count: int = 0
    agents_count: int = 0
    commands_count: int = 0
    hooks_count: int = 0
    mcp_servers_count: int = 0
    trust_status: str = ""
    signer: str = ""
    key_id: str = ""
    registry: str = ""
    integrity_digest: str = ""
    dependencies: Tuple[str, ...] = ()
    dependents: Tuple[str, ...] = ()
    optional_dependencies: Tuple[str, ...] = ()
    blocked_reason: str = ""

    @property
    def components_count(self) -> int:
        return (
            self.tools_count + self.skills_count + self.agents_count
            + self.commands_count + self.hooks_count + self.mcp_servers_count
        )


@dataclass(frozen=True)
class PluginLifecycleResult:
    action: str
    name: str
    scope: str
    path: Path
    message: str = ""

