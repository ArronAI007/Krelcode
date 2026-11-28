"""Daemon RPC client and REPL-compatible manager adapters.

This compatibility facade keeps the historical import path while transport,
background-job adapters and subagent adapters live in focused sibling modules.
"""

from __future__ import annotations

from .client_agents import DaemonSubagentManager, RemoteAgentHandle
from .client_jobs import DaemonJobManager
from .client_tasks import DaemonTaskClient
from .client_transport import DaemonClient, DaemonStatus, DaemonUnavailable


__all__ = [
    "DaemonClient",
    "DaemonJobManager",
    "DaemonStatus",
    "DaemonSubagentManager",
    "DaemonTaskClient",
    "DaemonUnavailable",
    "RemoteAgentHandle",
]
