"""Explicit dependency container for the agent turn runtime.

``AgentLoop`` is the composition point for a turn.  The concrete executors
receive this container instead of reaching into unrelated global state.  The
container is mutable only where the application legitimately swaps a live
resource (for example the provider after model configuration changes).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from ..core.events import EventBus
from ..core.hooks import HookPort
from ..core.interactions import UserInputPort
from ..core.redact import SecretRedactor
from ..contracts.tool import ToolContext
from ..llm.base import LLMProvider
from ..tools.registry import ToolRegistry


@dataclass
class AgentDependencies:
    """Resources shared by the model, context and tool turn executors."""

    llm: LLMProvider
    registry: ToolRegistry
    tool_context: ToolContext
    bus: EventBus
    context_manager: Any = None
    context_assembler: Any = None
    prompt_assembler: Any = None
    working_context_store: Any = None
    tool_microcompactor: Any = None
    compact_checkpoint_store: Any = None
    reminder_catalog: Any = None
    policy: Any = None
    redactor: Optional[SecretRedactor] = None
    runtime_snapshot_provider: Optional[
        Callable[[Any], Any]
    ] = None
    hook_port: Optional[HookPort] = None
    user_input_port: Optional[UserInputPort] = None
    step_gate: Optional[Callable[[], Awaitable[None]]] = None
    today_provider: Optional[Callable[[], str]] = None
    tool_output_inline_limit: int = 12_000
    repeat_failure_limit: int = 3
    max_steps: int = 64
    token_budget: Optional[int] = None

    @property
    def workspace_root(self):
        return self.tool_context.workspace_root


__all__ = ["AgentDependencies"]
