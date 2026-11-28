"""Canonical session event contract shared by every runtime surface.

The agent emits concrete :mod:`krelcode.core.events` payloads.  A
``SessionEvent`` adds the transport identity every REPL, headless, SDK,
daemon, web, recorder and stream-json consumer needs.  ``QueryEvent`` remains
as a compatibility alias while callers migrate to the session terminology.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Mapping, TypeAlias, cast

from .events import (
    AgentEvent,
    ApprovalRequested,
    AcceptanceEvaluated,
    CompactCheckpointCreated,
    ContextAssembled,
    ContextCheckpointCreated,
    ContextCompressed,
    ContextDropped,
    ContextMicrocompacted,
    ErrorOccurred,
    HookTriggered,
    KnowledgeLoaded,
    LLMResponseReceived,
    LLMStreamDelta,
    LLMThinkingDelta,
    LoopPhaseChanged,
    MemoryCandidateObserved,
    MemoryLoaded,
    MemoryWritten,
    ObservationRecorded,
    PermissionDenied,
    PlanUpdated,
    QueryPhaseChanged,
    RunCancelled,
    RunFinished,
    RunFailed,
    RunStarted,
    SecurityDecisionRecorded,
    ShellJobFinished,
    ShellJobOutput,
    ShellJobStarted,
    StepStarted,
    SubagentFeedUpdated,
    SubagentFinished,
    SubagentSpawned,
    SubagentStateChanged,
    ToolCallCompleted,
    ToolCallStarted,
    ToolOutputDelta,
    ToolProgress,
    UserInputRequested,
    UserInputResolved,
    UserSteered,
)


class SessionEventKind(str, Enum):
    """Stable semantic family used for routing and observability."""

    RUN = "run"
    STEP = "step"
    MODEL = "model"
    QUERY = "query"
    LOOP = "loop"
    OBSERVATION = "observation"
    TOOL = "tool"
    PERMISSION = "permission"
    CONTEXT = "context"
    MEMORY = "memory"
    KNOWLEDGE = "knowledge"
    COMPACT = "compact"
    HOOK = "hook"
    JOB = "job"
    SUBAGENT = "subagent"
    USER = "user"
    PLAN = "plan"
    INPUT = "input"
    ERROR = "error"
    UNKNOWN = "unknown"


SessionEventPayload: TypeAlias = (
    RunStarted
    | StepStarted
    | LLMStreamDelta
    | LLMThinkingDelta
    | LLMResponseReceived
    | QueryPhaseChanged
    | LoopPhaseChanged
    | ObservationRecorded
    | ToolCallStarted
    | ToolProgress
    | ToolOutputDelta
    | ToolCallCompleted
    | HookTriggered
    | ApprovalRequested
    | AcceptanceEvaluated
    | PermissionDenied
    | SecurityDecisionRecorded
    | ContextMicrocompacted
    | ContextCompressed
    | ContextCheckpointCreated
    | ContextAssembled
    | ContextDropped
    | MemoryLoaded
    | KnowledgeLoaded
    | MemoryCandidateObserved
    | MemoryWritten
    | CompactCheckpointCreated
    | UserSteered
    | PlanUpdated
    | UserInputRequested
    | UserInputResolved
    | ShellJobStarted
    | ShellJobOutput
    | ShellJobFinished
    | SubagentSpawned
    | SubagentFeedUpdated
    | SubagentStateChanged
    | SubagentFinished
    | RunFinished
    | RunCancelled
    | RunFailed
    | ErrorOccurred
)


SESSION_EVENT_TYPE_TO_KIND: Mapping[type[AgentEvent], SessionEventKind] = {
    RunStarted: SessionEventKind.RUN,
    StepStarted: SessionEventKind.STEP,
    LLMStreamDelta: SessionEventKind.MODEL,
    LLMThinkingDelta: SessionEventKind.MODEL,
    LLMResponseReceived: SessionEventKind.MODEL,
    QueryPhaseChanged: SessionEventKind.QUERY,
    LoopPhaseChanged: SessionEventKind.LOOP,
    ObservationRecorded: SessionEventKind.OBSERVATION,
    ToolCallStarted: SessionEventKind.TOOL,
    ToolProgress: SessionEventKind.TOOL,
    ToolOutputDelta: SessionEventKind.TOOL,
    ToolCallCompleted: SessionEventKind.TOOL,
    HookTriggered: SessionEventKind.HOOK,
    ApprovalRequested: SessionEventKind.PERMISSION,
    AcceptanceEvaluated: SessionEventKind.RUN,
    PermissionDenied: SessionEventKind.PERMISSION,
    SecurityDecisionRecorded: SessionEventKind.PERMISSION,
    ContextMicrocompacted: SessionEventKind.CONTEXT,
    ContextCompressed: SessionEventKind.CONTEXT,
    ContextCheckpointCreated: SessionEventKind.CONTEXT,
    ContextAssembled: SessionEventKind.CONTEXT,
    ContextDropped: SessionEventKind.CONTEXT,
    MemoryLoaded: SessionEventKind.MEMORY,
    KnowledgeLoaded: SessionEventKind.KNOWLEDGE,
    MemoryCandidateObserved: SessionEventKind.MEMORY,
    MemoryWritten: SessionEventKind.MEMORY,
    CompactCheckpointCreated: SessionEventKind.COMPACT,
    UserSteered: SessionEventKind.USER,
    PlanUpdated: SessionEventKind.PLAN,
    UserInputRequested: SessionEventKind.INPUT,
    UserInputResolved: SessionEventKind.INPUT,
    ShellJobStarted: SessionEventKind.JOB,
    ShellJobOutput: SessionEventKind.JOB,
    ShellJobFinished: SessionEventKind.JOB,
    SubagentSpawned: SessionEventKind.SUBAGENT,
    SubagentFeedUpdated: SessionEventKind.SUBAGENT,
    SubagentStateChanged: SessionEventKind.SUBAGENT,
    SubagentFinished: SessionEventKind.SUBAGENT,
    RunFinished: SessionEventKind.RUN,
    RunCancelled: SessionEventKind.RUN,
    RunFailed: SessionEventKind.RUN,
    ErrorOccurred: SessionEventKind.ERROR,
}

SESSION_EVENT_TYPES: tuple[type[AgentEvent], ...] = tuple(
    SESSION_EVENT_TYPE_TO_KIND
)


def as_session_event_payload(event: AgentEvent) -> SessionEventPayload:
    """Validate and narrow one event-bus payload to the canonical union."""

    if type(event) not in SESSION_EVENT_TYPE_TO_KIND:
        raise TypeError(
            "unsupported session event payload: "
            f"{type(event).__module__}.{type(event).__qualname__}"
        )
    return cast(SessionEventPayload, event)


def session_event_kind(event: AgentEvent) -> SessionEventKind:
    """Return the stable semantic family for one event payload.

    Unknown event subclasses remain routable for forward compatibility, but
    the live query engine still validates its own emissions before queueing.
    """

    return SESSION_EVENT_TYPE_TO_KIND.get(type(event), SessionEventKind.UNKNOWN)


@dataclass(frozen=True)
class SessionEvent:
    """One ordered event in a conversation turn.

    ``QueryEvent`` is retained as an alias so existing imports keep working,
    while all new boundaries consume this canonical session contract.
    """

    turn_id: str
    sequence: int
    event: SessionEventPayload
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("session event sequence must be non-negative")

    @property
    def event_type(self) -> str:
        return type(self.event).__name__

    @property
    def kind(self) -> SessionEventKind:
        return session_event_kind(self.event)

    @property
    def is_terminal(self) -> bool:
        return isinstance(self.event, (RunFinished, RunCancelled, RunFailed))

    def with_session_id(self, session_id: str) -> SessionEvent:
        """Return a copy bound to a concrete session without mutating the event."""

        return replace(self, session_id=session_id)


__all__ = [
    "SESSION_EVENT_TYPES",
    "SESSION_EVENT_TYPE_TO_KIND",
    "SessionEvent",
    "SessionEventKind",
    "SessionEventPayload",
    "as_session_event_payload",
    "session_event_kind",
]
