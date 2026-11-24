"""Configuration resolution for daemon-owned jobs and subagents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

DEFAULT_IDLE_TIMEOUT = 1_800.0


@dataclass(frozen=True)
class DaemonOptions:
    enabled: bool = False
    mode: str = "off"
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT
    config_path: Optional[Path] = None


def resolve_daemon_options(
    args: Any,
    config: Mapping[str, Any],
) -> DaemonOptions:
    """Resolve CLI override > [daemon] > off.

    ``auto`` means a client starts a daemon on the first spawn/start request.
    It does not eagerly start a process during ordinary REPL startup.
    """
    raw = (config or {}).get("daemon") if isinstance(config, Mapping) else None
    section: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    configured = section.get("enabled", False)
    if isinstance(configured, str):
        mode = configured.strip().lower()
    elif isinstance(configured, bool):
        mode = "auto" if configured else "off"
    else:
        mode = "off"
    if mode not in {"off", "auto", "on"}:
        mode = "off"

    cli = getattr(args, "daemon_enabled", None)
    if cli is True:
        mode = "auto"
    elif cli is False:
        mode = "off"

    idle = getattr(args, "daemon_idle_timeout", None)
    if idle is None:
        idle = section.get("idle_timeout", DEFAULT_IDLE_TIMEOUT)
    if not isinstance(idle, (str, int, float)):
        idle = DEFAULT_IDLE_TIMEOUT
    try:
        idle_timeout = max(0.0, float(idle))
    except (TypeError, ValueError):
        idle_timeout = DEFAULT_IDLE_TIMEOUT
    config_path = getattr(args, "config", None)
    return DaemonOptions(
        enabled=mode in {"auto", "on"},
        mode=mode,
        idle_timeout=idle_timeout,
        config_path=(Path(config_path).expanduser() if config_path else None),
    )


__all__ = ["DEFAULT_IDLE_TIMEOUT", "DaemonOptions", "resolve_daemon_options"]
