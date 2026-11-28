"""Input contract for one Context Builder invocation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Optional, Tuple

from ...contracts.runtime import RuntimeIdentity
from ...core.messages import Message

if TYPE_CHECKING:
    from ...contracts.agent import AgentContract
    from ...contracts.application import ApplicationRequest
    from ...agent.prompt.fragments import PromptSnapshot
    from ...agent.turn.tool_pool import TurnToolPool
    from ...agent.turn.workflows import TaskWorkflowSelection
    from .working_context import WorkingContextSnapshot
    from ...agent.turn.intent import TurnIntent


@dataclass(frozen=True)
class ContextRequest:
    """Everything a context source may inspect for one model request."""

    turn_id: str
    step: int
    history: Tuple[Message, ...]
    workspace_root: Path
    system_prompt: str = ""
    system_provider_meta: Optional[Mapping[str, object]] = None
    prompt_snapshot: Optional["PromptSnapshot"] = None
    tool_pool: Optional["TurnToolPool"] = None
    intent: Optional["TurnIntent"] = None
    task_workflow: Optional["TaskWorkflowSelection"] = None
    runtime_context: Tuple[str, ...] = ()
    working_context: Optional["WorkingContextSnapshot"] = None
    relevant_memory: Tuple[str, ...] = ()
    application_request: Optional["ApplicationRequest"] = None
    agent_contract: Optional["AgentContract"] = None
    principal: RuntimeIdentity = field(default_factory=RuntimeIdentity)
    project_id: str = ""
    session_id: str = ""
    task_id: str = ""


def resolve_runtime_identity(value: object) -> RuntimeIdentity:
    if isinstance(value, RuntimeIdentity):
        return value
    return RuntimeIdentity(
        principal_id=str(
            getattr(value, "principal_id", "local") or "local"),
        tenant_id=str(getattr(value, "tenant_id", "") or ""),
        organization_id=str(
            getattr(value, "organization_id", "") or ""),
    )


__all__ = ["ContextRequest", "resolve_runtime_identity"]
