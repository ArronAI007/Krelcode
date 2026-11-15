"""外部能力 → slash command 的适配器。"""

from __future__ import annotations

from typing import Any, Iterable

from .registry import CommandRegistry
from .types import CommandKind, CommandSource, CommandSpec


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
