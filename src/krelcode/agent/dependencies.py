"""Explicit dependency container for the agent turn runtime.

``AgentLoop`` is the composition point for a turn.  The concrete executors
receive this container instead of reaching into unrelated global state.  The
container is mutable only where the application legitimately swaps a live
resource (for example the provider after model configuration changes).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..core.events import EventBus
from ..core.hooks import HookPort
from ..core.interactions import UserInputPort
from ..core.redact import SecretRedactor
from ..contracts.tool import RuntimeSnapshot, ToolContext
from ..llm.base import LLMProvider
from ..safety.policy import ApprovalPolicy
from ..tools.registry import ToolRegistry
from .runtime_ports import (
    CompactCheckpointStorePort,
    ContextAssemblerPort,
    ContextManagerPort,
    PromptAssemblerPort,
    ReminderCatalogPort,
    ToolMicrocompactorPort,
    WorkingContextPort,
)


@dataclass
class AgentDependencies:
    """Resources shared by the model, context and tool turn executors."""

    llm: LLMProvider
    registry: ToolRegistry
    tool_context: ToolContext
    bus: EventBus
    context_assembler: ContextAssemblerPort
    prompt_assembler: PromptAssemblerPort
    working_context_store: WorkingContextPort
    tool_microcompactor: ToolMicrocompactorPort
    reminder_catalog: ReminderCatalogPort
    context_manager: Optional[ContextManagerPort] = None
    compact_checkpoint_store: Optional[CompactCheckpointStorePort] = None
    policy: Optional[ApprovalPolicy] = None
    redactor: Optional[SecretRedactor] = None
    runtime_snapshot_provider: Optional[
        Callable[[RuntimeSnapshot], RuntimeSnapshot]
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
    def workspace_root(self) -> Path:
        return self.tool_context.workspace_root


__all__ = ["AgentDependencies"]
