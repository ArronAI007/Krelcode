"""Built-in command for the read-only Security Review workflow."""

from __future__ import annotations

import shlex

from ....security_review.prompt import (
    build_security_review_input,
    security_review_command_prompt,
)
from ....security_review.tools import SECURITY_REVIEW_TOOL_NAMES
from ..context import CommandContext
from ..types import CommandResult


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
        redactor=getattr(ctx.loop, "redactor", None),
    )
    if review_input is None:
        return CommandResult(
            handled=True,
            output=("  没有可审查的 git 变更（工作区干净）。",),
        )
    return CommandResult(
        handled=True,
        run_prompt=security_review_command_prompt(review_input),
        tool_allowlist=tuple(sorted(SECURITY_REVIEW_TOOL_NAMES)),
    )


__all__ = ["handle_security_review"]
