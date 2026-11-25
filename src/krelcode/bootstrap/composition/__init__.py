"""Composition root package with a stable public facade."""

from __future__ import annotations

from .daemon import build_daemon_agent_runtime, build_parser
from .graph import CoreRuntime, ExtensionRuntime, RuntimeGraph
from .extensions import (
    build_custom_command_manager,
    build_plugin_manager,
    build_skill_manager,
    compose_hook_specs,
    configured_skill_paths,
)
from .mcp import build_mcp_roots, build_mcp_sampler, compose_mcp_config
from .memory import resolve_auto_memory_mode, resolve_auto_memory_thresholds
from .model_switcher import build_model_switcher
from .providers import build_llm
from .runtime import build_loop
from .services import (
    PersistenceServices,
    RuntimeServiceBinder,
    RuntimeServices,
)
from .security import (
    build_execution_sandbox,
    build_policy,
    resolve_permission_mode,
    resolve_run_mode,
)
from .subagents import build_subagent_llm_resolver
from .tools import default_tools
from krelcode.llm.registry import create_provider, resolve_settings
from krelcode.session.restore import restore_session

__all__ = [
    "CoreRuntime",
    "ExtensionRuntime",
    "PersistenceServices",
    "RuntimeGraph",
    "RuntimeServiceBinder",
    "RuntimeServices",
    "build_daemon_agent_runtime",
    "build_execution_sandbox",
    "build_llm",
    "build_loop",
    "build_mcp_roots",
    "build_mcp_sampler",
    "build_model_switcher",
    "build_parser",
    "build_policy",
    "build_subagent_llm_resolver",
    "compose_hook_specs",
    "compose_mcp_config",
    "configured_skill_paths",
    "create_provider",
    "default_tools",
    "resolve_auto_memory_mode",
    "resolve_auto_memory_thresholds",
    "resolve_permission_mode",
    "resolve_run_mode",
    "resolve_settings",
    "restore_session",
]
