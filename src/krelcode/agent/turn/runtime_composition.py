"""Construction helpers for the typed agent runtime services.

``AgentLoop`` remains the orchestration facade.  Service construction is kept
here so initialization cannot quietly turn back into a set of ad-hoc loop
attributes and the facade stays within the architecture size gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from .runtime_ports import CompactCheckpointStorePort
from .runtime_services import (
    AgentServices,
    ContextRuntimeServices,
    CoreRuntimeServices,
    ExtensionRuntimeServices,
    PromptRuntimeServices,
    SessionRuntimeServices,
)

if TYPE_CHECKING:
    from krelcode.agent.context.compression import ContextManager
    from krelcode.agent.context.context_assembler import ContextAssembler
    from krelcode.agent.context.tool_microcompact import ToolMicrocompactor
    from krelcode.agent.context.working_context import WorkingContextStore
    from krelcode.agent.prompt.fragments import PromptFragmentRegistry
    from krelcode.agent.prompt.prompt_assembler import PromptAssembler
    from krelcode.agent.prompt.prompt_surface import PromptSurfaceRegistry
    from krelcode.agent.turn.reminders import ReminderCatalog
    from krelcode.core.events import EventBus
    from krelcode.core.hooks import HookPort
    from krelcode.core.interactions import UserInputPort
    from krelcode.core.redact import SecretRedactor
    from krelcode.contracts.tool import ToolContext
    from krelcode.llm.base import LLMProvider
    from krelcode.safety.policy import ApprovalPolicy
    from krelcode.tools.registry import ToolRegistry


def build_agent_services(
    *,
    llm: "LLMProvider",
    registry: "ToolRegistry",
    tool_context: "ToolContext",
    policy: Optional["ApprovalPolicy"],
    bus: Optional["EventBus"],
    redactor: Optional["SecretRedactor"],
    hook_port: Optional["HookPort"],
    user_input_port: Optional["UserInputPort"],
    system_prompt: str,
    context_manager: Optional["ContextManager"],
    context_assembler: Optional["ContextAssembler"],
    prompt_assembler: Optional["PromptAssembler"],
    working_context_store: Optional["WorkingContextStore"],
    tool_microcompactor: Optional["ToolMicrocompactor"],
    compact_checkpoint_store: Optional[CompactCheckpointStorePort],
    reminder_catalog: Optional["ReminderCatalog"],
    registry_provider: Callable[[], Optional["PromptFragmentRegistry"]],
) -> AgentServices:
    """Build the stable service graph shared by all turn executors."""
    # Import concrete agent-layer implementations lazily to keep this helper out
    # of import cycles while retaining the same construction semantics as before.
    from krelcode.agent.context.context_assembler import ContextAssembler
    from krelcode.agent.context.context_source import (
        RetrievedMemoryContextSource,
        default_context_sources,
    )
    from krelcode.agent.context.tool_microcompact import ToolMicrocompactor
    from krelcode.agent.context.working_context import WorkingContextStore
    from krelcode.agent.context.working_context_source import WorkingContextSource
    from krelcode.agent.prompt.prompt_assembler import PromptAssembler
    from krelcode.agent.prompt.prompt_surface import (
        PromptSurfaceRegistry,
        main_prompt_surface,
    )
    from krelcode.agent.turn.reminders import default_reminder_catalog
    from krelcode.core.events import EventBus

    working_context = working_context_store or WorkingContextStore(
        tool_context.workspace_root,
        redactor=redactor.redact if redactor is not None else None,
    )
    if working_context.redactor is None and redactor is not None:
        working_context.redactor = redactor.redact
    if tool_microcompactor is not None and context_manager is not None:
        context_manager.microcompactor = tool_microcompactor
    if context_manager is not None:
        if context_manager.working_context_provider is None:
            context_manager.working_context_provider = (
                lambda: working_context.snapshot())
        if redactor is not None:
            context_manager.redactor = redactor
    microcompactor = (
        tool_microcompactor
        or (context_manager.microcompactor if context_manager is not None else None)
        or ToolMicrocompactor()
    )
    if context_assembler is None:
        effective_context_assembler = ContextAssembler(
            default_context_sources(working_context_store=working_context))
    else:
        effective_context_assembler = context_assembler
        if (
            working_context_store is not None
            and "working_context" not in effective_context_assembler.source_names
        ):
            effective_context_assembler.register(
                WorkingContextSource(working_context))
        if "memory_retrieval" not in effective_context_assembler.source_names:
            effective_context_assembler.register(RetrievedMemoryContextSource())
    effective_prompt_assembler = prompt_assembler or PromptAssembler(
        effective_context_assembler,
        registry_provider=registry_provider,
    )
    surface_registry: PromptSurfaceRegistry = PromptSurfaceRegistry({
        "main": (main_prompt_surface(system_prompt),),
    })
    return AgentServices(
        core=CoreRuntimeServices(
            llm=llm,
            registry=registry,
            tool_context=tool_context,
            bus=bus or EventBus(),
            policy=policy,
            redactor=redactor,
            hook_port=hook_port,
            user_input_port=user_input_port,
        ),
        context=ContextRuntimeServices(
            manager=context_manager,
            assembler=effective_context_assembler,
            working_context=working_context,
            tool_microcompactor=microcompactor,
            reminder_catalog=reminder_catalog or default_reminder_catalog(),
            compact_checkpoint=compact_checkpoint_store,
        ),
        prompt=PromptRuntimeServices(
            assembler=effective_prompt_assembler,
            surface_registry=surface_registry,
        ),
        extensions=ExtensionRuntimeServices(),
        session=SessionRuntimeServices(),
    )


__all__ = ["build_agent_services"]
