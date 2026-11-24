"""Hooks and input-processing services for the interactive context plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional

from ...core.messages import Message
from ...hooks import HookRunner, HookSpec
from ...llm.base import collect
from ...session.input import InputProcessor
from .context import ApplicationBuildContext
from .core import CoreServices


@dataclass(frozen=True)
class ContextServices:
    """Context-plane adapters shared by REPL, commands, tools and subagents."""

    hooks: Optional[HookRunner]
    input_processor: InputProcessor
    refresh_hooks: Callable[[], None]


def build_context_services(
    context: ApplicationBuildContext,
    core: CoreServices,
) -> ContextServices:
    """Build hook execution and the normalized input pipeline."""
    loop = core.loop
    hooks: Optional[HookRunner] = None

    async def run_prompt_hook(
        prompt: str,
        _payload: Dict[str, object],
    ) -> str:
        finished = await collect(loop.llm, [Message.user(prompt)])
        return finished.message.content or ""

    def ensure_hooks(specs: list[HookSpec]) -> None:
        nonlocal hooks
        if hooks is None:
            hooks = HookRunner(
                loop.tool_context.workspace_root,
                specs,
                log=context.note,
                redactor=core.redactor,
                prompt_runner=run_prompt_hook,
                prompt_surface_registry=loop.prompt_surface_registry,
            )
            hooks.attach(loop.bus)
        else:
            hooks.replace_specs(specs)
        loop.set_hook_port(hooks)
        core.subagents.hook_port = hooks
        if specs:
            context.note(
                f"[hooks] 已加载 {len(specs)} 个钩子: "
                + ", ".join(sorted({hook.on for hook in specs})))

    def refresh_hooks() -> None:
        ensure_hooks(core.config.merged_hook_specs())

    refresh_hooks()

    async def collect_hook_context(prompt: str) -> tuple[str, ...]:
        if hooks is None:
            return ()
        return await hooks.collect_input_context(prompt)

    input_processor = InputProcessor(
        loop.tool_context.workspace_root,
        hook_context_provider=collect_hook_context,
    )
    return ContextServices(
        hooks=hooks,
        input_processor=input_processor,
        refresh_hooks=refresh_hooks,
    )


__all__ = ["ContextServices", "build_context_services"]
