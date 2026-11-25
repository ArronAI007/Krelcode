"""Stable facade for grouped AgentLoop runtime state views.

The implementation is split into leaf state models, a service view and a
mutable-state view.  Existing imports keep using this module while the
architecture gate keeps each implementation file below the size limit.
"""

from __future__ import annotations

from typing import Optional

from krelcode.core.hooks import HookPort

from .runtime_service_view import RuntimeServiceView
from .runtime_state_models import (
    AgentRuntimeState,
    ContextRuntimeState,
    ExtensionRuntimeState,
    InstructionLayerPort,
    PromptRuntimeState,
    SessionRuntimeState,
    TurnRuntimeState,
)
from .runtime_state_view import RuntimeStateView


class RuntimeStateHost(RuntimeServiceView, RuntimeStateView):
    """Explicit typed view over services and mutable turn state."""

    def set_hook_port(self, hook_port: Optional[HookPort]) -> None:
        self.services.core.hook_port = hook_port
        self.dependencies.hook_port = hook_port


__all__ = [
    "AgentRuntimeState",
    "ContextRuntimeState",
    "ExtensionRuntimeState",
    "InstructionLayerPort",
    "PromptRuntimeState",
    "RuntimeStateHost",
    "SessionRuntimeState",
    "TurnRuntimeState",
]
