"""Explicit runtime composition graph for a bootstrapped application.

The graph separates process-facing core services from hot-reloadable extension
surfaces.  It deliberately contains only runtime-ready objects and callables;
construction remains in ``bootstrap.application``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class CoreRuntime:
    """Core services required to run and close one application instance."""

    runtime: Any
    supervisor: Any
    checkpoints: Any
    usage: Any
    model_switch: Any
    redactor: Any
    input_processor: Any
    approval_broker: Any
    input_broker: Any
    hooks: Any
    telemetry_runtime: Any
    auditor: Any
    loop: Any


@dataclass(frozen=True)
class ExtensionRuntime:
    """Hot-reloadable extension surfaces exposed to command/tool consumers."""

    plugin_manager: Any
    skill_manager: Any
    agent_definitions: Any
    extension_registry: Any
    custom_commands: Any
    refresh: Callable[[], Any]
    reload_skills: Callable[[], Any]
    reload_plugins: Callable[[], Awaitable[Any]]
    reload_custom_commands: Callable[[], Any]


@dataclass(frozen=True)
class RuntimeGraph:
    """The complete runtime object graph owned by one application instance."""

    core: CoreRuntime
    extensions: ExtensionRuntime


__all__ = ["CoreRuntime", "ExtensionRuntime", "RuntimeGraph"]
