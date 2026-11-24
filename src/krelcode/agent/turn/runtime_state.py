"""Explicit mutable state owned by one ``AgentLoop`` instance.

The loop historically exposed a large number of ad-hoc attributes.  That made
it easy for a command, a subagent or a composition helper to mutate runtime
state without any visible ownership boundary.  This module gives those values
stable names and groups them by concern while keeping the old attribute access
as a compatibility view during the migration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Optional, cast

from ...core.hooks import HookPort
from .runtime_services import AgentServices


class StateGroup:
    """A small attribute view over one concern's mutable values.

    Values are intentionally stored in a dictionary at this migration stage.
    Different concerns own very different runtime types (providers, queues,
    prompt plans, checkpoints), so forcing a universal generic type would
    weaken the boundary instead of making it clearer.
    """

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        object.__setattr__(self, "values", dict(values or {}))

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def set(self, name: str, value: Any) -> None:
        self.values[name] = value

    def has(self, name: str) -> bool:
        return name in self.values

    def __getattribute__(self, name: str) -> Any:
        if name != "values":
            values = object.__getattribute__(self, "__dict__").get("values")
            if values is not None and name in values:
                return values[name]
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "values":
            object.__setattr__(self, name, value)
            return
        values = self.__dict__.get("values")
        if values is None:
            values = {}
            object.__setattr__(self, "values", values)
        values[name] = value

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self.values))


@dataclass
class TurnRuntimeState(StateGroup):
    """Per-turn execution state and the current model/tool history."""

    history: Any = field(default_factory=list)
    runtime_context: Any = field(default_factory=list)
    token_total: int = 0
    failure_counts: Any = field(default_factory=dict)
    turn_intent: Any = None
    turn_workflow: Any = None
    active_tool_pool: Any = None
    turn_tool_context: Any = None
    turn_abort_signal: Any = None
    turn_counter: Any = field(default_factory=lambda: count(1))
    direct_counter: Any = field(default_factory=lambda: count(1))
    current_turn_id: str = ""
    input_counter: Any = field(default_factory=lambda: count(1))
    turn_prepared: bool = False
    turn_reminder_selection: Any = None
    turn_requested_reminder_ids: tuple[str, ...] = ()
    last_reminder_selection: Any = None
    steering_queue: Any = field(default_factory=asyncio.Queue)

    def __post_init__(self) -> None:
        # Keep the historical private spellings available through the loop
        # compatibility view without duplicating state storage.
        self.values.update({
            "_token_total": self.token_total,
            "_failure_counts": self.failure_counts,
            "_turn_intent": self.turn_intent,
            "_turn_workflow": self.turn_workflow,
            "_active_tool_pool": self.active_tool_pool,
            "_turn_tool_context": self.turn_tool_context,
            "_turn_abort_signal": self.turn_abort_signal,
            "_turn_counter": self.turn_counter,
            "_direct_counter": self.direct_counter,
            "_current_turn_id": self.current_turn_id,
            "_input_counter": self.input_counter,
            "_turn_prepared": self.turn_prepared,
            "_turn_reminder_selection": self.turn_reminder_selection,
            "_turn_requested_reminder_ids": self.turn_requested_reminder_ids,
            "last_reminder_selection": self.last_reminder_selection,
        })


@dataclass
class ContextRuntimeState(StateGroup):
    """Context, prompt-request observability and checkpoint state."""

    pending_context_refresh: bool = False
    workspace_context: Any = None
    context_options: Any = None
    context_engine: str = "v3"
    context_enabled: bool = True
    context_integrity: Any = None
    last_compact_checkpoint: Any = None
    last_context_restore: Any = None
    last_context_plan: Any = None
    last_context_snapshot: Any = None
    last_context_assembly: Any = None
    last_context_cache_ledger: Any = None
    previous_context_snapshot: Any = None
    previous_context_cache_ledger: Any = None
    last_mode_permission_contract: Any = None
    context_attachments: Any = None
    last_attachment_diff: Any = None
    last_microcompact_report: Any = None
    context_workspace_fingerprint: Any = None
    restored_workspace_fingerprint: Any = None
    restored_context_checkpoint_id: str = ""
    restored_memory_context: tuple[Any, ...] = ()
    restored_relevant_memory_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.values.update({
            "pending_context_refresh": self.pending_context_refresh,
            "workspace_context": self.workspace_context,
            "context_options": self.context_options,
            "context_engine": self.context_engine,
            "context_enabled": self.context_enabled,
            "context_integrity": self.context_integrity,
            "last_compact_checkpoint": self.last_compact_checkpoint,
            "last_context_restore": self.last_context_restore,
            "last_context_plan": self.last_context_plan,
            "last_context_snapshot": self.last_context_snapshot,
            "last_context_assembly": self.last_context_assembly,
            "last_context_cache_ledger": self.last_context_cache_ledger,
            "previous_context_snapshot": self.previous_context_snapshot,
            "previous_context_cache_ledger": self.previous_context_cache_ledger,
            "last_mode_permission_contract": self.last_mode_permission_contract,
            "context_attachments": self.context_attachments,
            "last_attachment_diff": self.last_attachment_diff,
            "last_microcompact_report": self.last_microcompact_report,
            "context_workspace_fingerprint": self.context_workspace_fingerprint,
            "restored_workspace_fingerprint": self.restored_workspace_fingerprint,
            "restored_context_checkpoint_id": self.restored_context_checkpoint_id,
            "restored_memory_context": self.restored_memory_context,
            "restored_relevant_memory_ids": self.restored_relevant_memory_ids,
        })


@dataclass
class PromptRuntimeState(StateGroup):
    """Prompt registry/profile and provider-message observability."""

    prompt_snapshot: Any = None
    prompt_profile: Any = None
    prompt_profile_resolution: Any = None
    prompt_profile_name: str = ""
    prompt_profile_config: Any = "auto"
    prompt_authoring_enabled: bool = False
    auxiliary_prompt_surfaces: tuple[Any, ...] = ()
    last_prompt_plan: Any = None
    last_provider_messages: Any = None
    cache_ledger_owner: str = "main"
    parent_policy_instruction_hash: str = ""
    skill_base_prompt: str = ""
    inherited_memory_ids: tuple[str, ...] = ()
    parent_history_inherited: bool = False
    subagent_definition_name: str = ""
    subagent_instruction_layers: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        self.values.update({
            "prompt_snapshot": self.prompt_snapshot,
            "prompt_profile": self.prompt_profile,
            "prompt_profile_resolution": self.prompt_profile_resolution,
            "prompt_profile_name": self.prompt_profile_name,
            "prompt_profile_config": self.prompt_profile_config,
            "prompt_authoring_enabled": self.prompt_authoring_enabled,
            "auxiliary_prompt_surfaces": self.auxiliary_prompt_surfaces,
            "last_prompt_plan": self.last_prompt_plan,
            "last_provider_messages": self.last_provider_messages,
            "cache_ledger_owner": self.cache_ledger_owner,
            "parent_policy_instruction_hash": self.parent_policy_instruction_hash,
            "skill_base_prompt": self.skill_base_prompt,
            "inherited_memory_ids": self.inherited_memory_ids,
            "parent_history_inherited": self.parent_history_inherited,
            "subagent_definition_name": self.subagent_definition_name,
            "subagent_instruction_layers": self.subagent_instruction_layers,
        })


@dataclass
class ExtensionRuntimeState(StateGroup):
    """Reserved compatibility group; extension services are typed services."""

    def __post_init__(self) -> None:
        StateGroup.__init__(self)


@dataclass
class SessionRuntimeState(StateGroup):
    """Session-scoped mutable state reserved for future use."""

    def __post_init__(self) -> None:
        StateGroup.__init__(self)


@dataclass
class AgentRuntimeState:
    """Grouped mutable state owned by an ``AgentLoop``."""

    turn: TurnRuntimeState = field(default_factory=TurnRuntimeState)
    context: ContextRuntimeState = field(default_factory=ContextRuntimeState)
    prompt: PromptRuntimeState = field(default_factory=PromptRuntimeState)
    extensions: ExtensionRuntimeState = field(default_factory=ExtensionRuntimeState)
    session: SessionRuntimeState = field(default_factory=SessionRuntimeState)

    _FIELD_GROUPS: dict[str, StateGroup] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        grouped: dict[str, StateGroup] = {}
        for name in TurnRuntimeState().values:
            grouped[name] = self.turn
        for name in ContextRuntimeState().values:
            grouped[name] = self.context
        for name in PromptRuntimeState().values:
            grouped[name] = self.prompt
        for name in ExtensionRuntimeState().values:
            grouped[name] = self.extensions
        for name in SessionRuntimeState().values:
            grouped[name] = self.session
        object.__setattr__(self, "_FIELD_GROUPS", grouped)

    def has_field(self, name: str) -> bool:
        return name in self._FIELD_GROUPS

    def get_field(self, name: str, default: Any = None) -> Any:
        group = self._FIELD_GROUPS.get(name)
        return default if group is None else group.get(name, default)

    def set_field(self, name: str, value: Any) -> None:
        group = self._FIELD_GROUPS.get(name)
        if group is None:
            raise AttributeError(name)
        group.set(name, value)

    def update(self, **values: Any) -> None:
        for name, value in values.items():
            self.set_field(name, value)


__all__ = [
    "AgentRuntimeState",
    "ContextRuntimeState",
    "ExtensionRuntimeState",
    "PromptRuntimeState",
    "RuntimeStateHost",
    "SessionRuntimeState",
    "StateGroup",
    "TurnRuntimeState",
]

class RuntimeStateHost:
    """Compatibility view over service and mutable-state ownership.

    ``services`` is checked first, then ``runtime_state``.  This precedence
    prevents a stable provider/store/registry from ever falling back into the
    turn-state dictionary.
    """

    _DEPENDENCY_ATTRS: frozenset[str] = frozenset()
    services: AgentServices
    hook_port: Optional[HookPort]

    def __setattr__(self, name: str, value: Any) -> None:
        services = self.__dict__.get("services")
        if services is not None and services.has_field(name):
            services.set_field(name, value)
            dependencies = self.__dict__.get("dependencies")
            if dependencies is not None and name in self._DEPENDENCY_ATTRS:
                setattr(dependencies, name, value)
            return
        state = self.__dict__.get("_runtime_state")
        if state is not None and state.has_field(name):
            state.set_field(name, value)
            return
        object.__setattr__(self, name, value)
        dependencies = self.__dict__.get("dependencies")
        if dependencies is not None and name in self._DEPENDENCY_ATTRS:
            setattr(dependencies, name, value)

    def __getattr__(self, name: str) -> Any:
        services = self.__dict__.get("services")
        if services is not None and services.has_field(name):
            return services.get_field(name)
        state = self.__dict__.get("_runtime_state")
        if state is not None and state.has_field(name):
            return state.get_field(name)
        raise AttributeError(name)

    @property
    def runtime_state(self) -> AgentRuntimeState:
        """Return the explicit, grouped mutable state owned by this host."""
        return cast(AgentRuntimeState, self._runtime_state)

    def update_runtime_state(self, **values: Any) -> None:
        """Route an explicit update through the ownership boundary.

        Service names are checked first, then mutable turn state.  Unknown
        names are rejected instead of silently creating ad-hoc attributes.
        Routing through ``__setattr__`` also keeps ``AgentDependencies`` in
        sync when a live service is replaced at the composition root.
        """
        services = self.__dict__.get("services")
        state = self._runtime_state
        for name, value in values.items():
            if not ((services is not None and services.has_field(name))
                    or state.has_field(name)):
                raise AttributeError(name)
            setattr(self, name, value)

    def set_hook_port(self, hook_port: Optional[HookPort]) -> None:
        """Bind or replace a dependency-owner hook without direct caller writes."""
        self.hook_port = hook_port
