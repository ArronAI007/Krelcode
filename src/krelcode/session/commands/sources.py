"""外部能力 → slash command 的适配器。"""

from __future__ import annotations

from typing import Any, Iterable

from .registry import CommandRegistry
from .types import CommandKind, CommandSource, CommandSpec


def custom_command_specs(manager: Any) -> tuple[CommandSpec, ...]:
    """Expose user/project Markdown commands with provenance metadata."""
    if manager is None or not hasattr(manager, "list"):
        return ()
    specs: list[CommandSpec] = []
    for command in manager.list():
        specs.append(CommandSpec(
            name=command.name,
            description=command.description,
            kind=CommandKind.PROMPT,
            source=CommandSource.CUSTOM,
            argument_hint=command.argument_hint,
            help=f"[{command.source}] {command.description}",
            metadata={
                "source": command.source,
                "path": str(command.path),
                "allowed_tools": tuple(command.allowed_tools),
            },
        ))
    return tuple(specs)


def sync_custom_commands(registry: CommandRegistry, manager: Any) -> None:
    registry.replace_source(CommandSource.CUSTOM, custom_command_specs(manager))


def skill_command_specs(skills: Any) -> tuple[CommandSpec, ...]:
    """仅把允许用户调用的 Skill 暴露为 slash command。"""
    if skills is None or not getattr(skills, "enabled", False):
        return ()
    specs: list[CommandSpec] = []
    for skill in skills.list(user_invocable_only=True):
        hint = " ".join(str(skill.argument_hint or "").split())
        specs.append(CommandSpec(
            name=skill.name,
            description=skill.description,
            kind=CommandKind.PROMPT,
            source=CommandSource.SKILL,
            argument_hint=hint,
            help=f"调用 Skill：{skill.description}",
        ))
    return tuple(specs)


def sync_skill_commands(registry: CommandRegistry, skills: Any) -> None:
    registry.replace_source(CommandSource.SKILL, skill_command_specs(skills))


def mcp_prompt_command_specs(supervisor: Any) -> tuple[CommandSpec, ...]:
    if supervisor is None or not hasattr(supervisor, "prompt_records"):
        return ()
    specs: list[CommandSpec] = []
    for record in supervisor.prompt_records():
        names = tuple(record.get("argument_names") or ())
        hint = " ".join(f"<{name}>" for name in names)
        description = " ".join(str(
            record.get("description") or f"MCP Prompt：{record['prompt_name']}"
        ).split())[:320]
        specs.append(CommandSpec(
            name=str(record["command_name"]),
            description=description,
            kind=CommandKind.PROMPT,
            source=CommandSource.MCP,
            argument_hint=hint,
            help=f"[{record['server']}] {description}",
            metadata={
                "server": record["server"],
                "prompt_name": record["prompt_name"],
                "argument_names": names,
            },
        ))
    return tuple(specs)


def sync_mcp_prompt_commands(
    registry: CommandRegistry, supervisor: Any,
) -> None:
    registry.replace_source(CommandSource.MCP, mcp_prompt_command_specs(supervisor))


def plugin_command_specs(manager: Any) -> tuple[CommandSpec, ...]:
    """Expose Plugin v2 Markdown commands with package namespacing."""
    if manager is None or not getattr(manager, "enabled", False):
        return ()
    specs: list[CommandSpec] = []
    for command in getattr(manager, "commands", ()):
        specs.append(CommandSpec(
            name=command.name,
            description=command.description,
            kind=CommandKind.PROMPT,
            source=CommandSource.PLUGIN,
            argument_hint=command.argument_hint,
            help=f"[plugin:{command.plugin_name}] {command.description}",
            metadata={
                "plugin": command.plugin_name,
                "command": command.name,
                "path": str(command.path),
            },
        ))
    return tuple(specs)


def sync_plugin_commands(registry: CommandRegistry, manager: Any) -> None:
    registry.replace_source(CommandSource.PLUGIN, plugin_command_specs(manager))
