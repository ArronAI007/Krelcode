"""Service-boundary ports keep dynamic command/tool dependencies explicit."""

from __future__ import annotations

import collections.abc
import typing

from krelcode.contracts.service_ports import (
    EnterprisePolicyPort,
    PrincipalPort,
    PromptBenchmarkServicesPort,
    PromptFragmentRegistryPort,
    PromptReleasePipelinePort,
    ShellJobManagerPort,
)
from krelcode.contracts.tool import ToolContext
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.ports import (
    CheckpointManagerPort,
    CommandLoopPort,
    MCPSupervisorPort,
    PermissionManagerPort,
    PlanManagerPort,
    PluginManagerPort,
    RewindPort,
    SessionLifecyclePort,
    ShellJobCommandPort,
    SkillManagerPort,
    SubagentManagerCommandPort,
    UsageTrackerPort,
)


TOOL_SERVICE_PORTS = {
    "shell_jobs": ShellJobManagerPort,
    "prompt_registry": PromptFragmentRegistryPort,
    "prompt_release_pipeline": PromptReleasePipelinePort,
    "prompt_benchmark_services": PromptBenchmarkServicesPort,
    "enterprise_policy": EnterprisePolicyPort,
    "principal": PrincipalPort,
}

COMMAND_SERVICE_PORTS = {
    "loop": CommandLoopPort,
    "mcp_supervisor": MCPSupervisorPort,
    "subagents": SubagentManagerCommandPort,
    "checkpoints": CheckpointManagerPort,
    "usage": UsageTrackerPort,
    "skills": SkillManagerPort,
    "plugins": PluginManagerPort,
    "session_lifecycle": SessionLifecyclePort,
    "permission_manager": PermissionManagerPort,
    "plan_manager": PlanManagerPort,
    "rewind": RewindPort,
    "jobs": ShellJobCommandPort,
}


def _unwrap_optional(annotation: object) -> tuple[object, ...]:
    return typing.get_args(annotation) or (annotation,)


def test_tool_context_service_fields_are_ports_not_any():
    annotations = typing.get_type_hints(ToolContext)
    for field, port in TOOL_SERVICE_PORTS.items():
        members = _unwrap_optional(annotations[field])
        assert port in members, (field, annotations[field])
        assert typing.Any not in members, (field, annotations[field])
        assert getattr(port, "_is_protocol", False), field


def test_command_context_service_fields_are_ports_not_any():
    annotations = typing.get_type_hints(CommandContext)
    for field, port in COMMAND_SERVICE_PORTS.items():
        members = _unwrap_optional(annotations[field])
        assert port in members, (field, annotations[field])
        assert typing.Any not in members, (field, annotations[field])
        assert getattr(port, "_is_protocol", False), field


def test_command_context_callback_fields_are_typed_callables():
    annotations = typing.get_type_hints(CommandContext)
    for field in ("mcp_reload", "model_switch", "skills_reload", "plugins_reload"):
        members = _unwrap_optional(annotations[field])
        assert typing.Any not in members, (field, annotations[field])
        assert any(
            typing.get_origin(member) is collections.abc.Callable
            for member in members
        ), (field, annotations[field])
