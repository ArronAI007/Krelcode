"""Shared result-building helpers for built-in command handlers."""

from __future__ import annotations

from ..context import CommandContext
from ..template import CommandRenderResult, CommandTemplate
from ..types import CommandResult, CommandSource


def _ok(*output: str) -> CommandResult:
    return CommandResult(handled=True, output=tuple(output))


def _result_from_render(
    rendered: CommandRenderResult,
    *,
    output: str,
) -> CommandResult:
    return CommandResult(
        handled=True,
        output=(output,),
        run_prompt=rendered.prompt,
        tool_allowlist=rendered.tool_allowlist,
        command_source=rendered.source,
        command_trust=rendered.trust,
        reminder_ids=rendered.reminder_ids,
        template_hash=rendered.template_hash,
    )


def _template_for_command(ctx: CommandContext, name: str) -> CommandTemplate:
    spec = ctx.command_registry.resolve(name)
    if spec is not None:
        return CommandTemplate.from_spec(spec)
    return CommandTemplate.for_source(CommandSource.BUILTIN)

