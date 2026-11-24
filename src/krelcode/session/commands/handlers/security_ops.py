"""Built-in command for the read-only Security Review workflow."""

from __future__ import annotations

import shlex

from ....security_review.prompt import (
    build_security_review_input,
    security_review_command_prompt,
)
from ....security_review.tools import SECURITY_REVIEW_TOOL_NAMES
from ..context import CommandContext
from ..template import CommandTemplate
from ..types import CommandResult, CommandSource


def _parse_arguments(arguments: str) -> tuple[bool, str, str]:
    try:
        tokens = shlex.split(arguments)
    except ValueError as exc:
        return False, "", str(exc)
    staged = False
    focus: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--staged":
            staged = True
            index += 1
            continue
        if token == "--focus":
            focus.extend(tokens[index + 1:])
            break
        if token.startswith("--"):
            return False, "", f"未知参数: {token}"
        focus.append(token)
        index += 1
    return staged, " ".join(focus).strip(), ""


async def handle_security_review(ctx: CommandContext) -> CommandResult:
    staged, focus, error = _parse_arguments(ctx.request.arguments)
    if error:
        return CommandResult(
            handled=True,
            output=(f"  [security-review] {error}",),
        )
    review_input = await build_security_review_input(
        ctx.workspace_root,
        staged=staged,
        focus=focus,
        redactor=ctx.loop.services.core.redactor,
    )
    if review_input is None:
        return CommandResult(
            handled=True,
            output=("  没有可审查的 git 变更（工作区干净）。",),
        )
    spec = ctx.command_registry.resolve(ctx.request.name)
    template = (
        CommandTemplate.from_spec(spec)
        if spec is not None
        else CommandTemplate.for_source(
            CommandSource.BUILTIN,
            allowed_tools=tuple(sorted(SECURITY_REVIEW_TOOL_NAMES)),
        )
    )
    rendered = template.bind(
        allowed_tools=tuple(sorted(SECURITY_REVIEW_TOOL_NAMES)),
    ).render(
        name=ctx.request.name,
        body=security_review_command_prompt(review_input),
    )
    return CommandResult(
        handled=True,
        run_prompt=rendered.prompt,
        tool_allowlist=rendered.tool_allowlist,
        command_source=rendered.source,
        command_trust=rendered.trust,
        reminder_ids=rendered.reminder_ids,
        template_hash=rendered.template_hash,
    )


__all__ = ["handle_security_review"]
