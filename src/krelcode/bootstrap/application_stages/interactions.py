"""Session interaction brokers required before the agent loop is built."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ...contracts.tool import RunMode
from ...session.interactions import UserInputBroker
from ...ui.approval import ApprovalBroker
from .context import ApplicationBuildContext


@dataclass(frozen=True)
class InteractionServices:
    """Foreground rendezvous ports shared by tools and transports."""

    approval_broker: Optional[ApprovalBroker]
    input_broker: Optional[UserInputBroker]


def build_interaction_services(
    context: ApplicationBuildContext,
) -> InteractionServices:
    editor_stream = context.input_format == "stream-json"
    approval_broker = (
        ApprovalBroker()
        if (
            context.run_mode is RunMode.REPL or editor_stream
        )
        and not getattr(context.args, "no_sandbox", False)
        else None
    )
    input_broker = (
        UserInputBroker()
        if context.run_mode is RunMode.REPL or editor_stream
        else None
    )
    return InteractionServices(
        approval_broker=approval_broker,
        input_broker=input_broker,
    )


__all__ = ["InteractionServices", "build_interaction_services"]
