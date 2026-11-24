"""Project child Agent events into bounded interactive feed entries."""

from __future__ import annotations

import json
from typing import Dict, Optional, Tuple

from ..core.events import (
    ContextCompressed,
    ErrorOccurred,
    LLMStreamDelta,
    LLMThinkingDelta,
    QueryPhaseChanged,
    RunCancelled,
    RunFinished,
    RunStarted,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    ToolOutputDelta,
    ToolProgress,
    UserSteered,
)


def project_feed_event(
    event: object,
) -> Optional[Tuple[str, str, Dict[str, object]]]:
    """Return ``(kind, text, data)`` for one user-visible child event."""
    if isinstance(event, RunStarted):
        return "user_message", event.user_input, {
            "turn_id": event.turn_id, "session_id": event.session_id}
    if isinstance(event, StepStarted):
        return "step_started", f"step {event.step}", {"step": event.step}
    if isinstance(event, QueryPhaseChanged):
        return "phase", event.detail or event.phase, {"phase": event.phase}
    if isinstance(event, LLMStreamDelta):
        return "assistant_delta", event.text, {}
    if isinstance(event, LLMThinkingDelta):
        return "thinking_delta", event.text, {}
    if isinstance(event, ToolCallStarted):
        args = json.dumps(
            event.tool_call.arguments, ensure_ascii=False, sort_keys=True)
        return "tool_started", f"{event.tool_call.name} {args}", {
            "tool_call_id": event.tool_call.id,
            "tool_name": event.tool_call.name,
            "arguments": event.tool_call.arguments,
        }
    if isinstance(event, ToolProgress):
        return "tool_progress", event.message, {
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            **dict(event.data),
        }
    if isinstance(event, ToolOutputDelta):
        return "tool_output", event.text, {
            "tool_call_id": event.tool_call_id,
            "tool_name": event.tool_name,
            "stream": event.stream,
        }
    if isinstance(event, ToolCallCompleted):
        return "tool_completed", event.content, {
            "tool_call_id": event.tool_call.id,
            "tool_name": event.tool_call.name,
            "is_error": event.is_error,
            **dict(event.data),
        }
    if isinstance(event, RunFinished):
        return "run_finished", event.final_text, {"turn_id": event.turn_id}
    if isinstance(event, RunCancelled):
        return "run_cancelled", event.reason, {"turn_id": event.turn_id}
    if isinstance(event, UserSteered):
        return "user_steered", event.text, {}
    if isinstance(event, ContextCompressed):
        return "context_compressed", "history compressed", {
            "before_tokens": event.before_tokens,
            "after_tokens": event.after_tokens,
            "strategy": event.strategy,
            "summary_source": event.summary_source,
            "generation": event.generation,
            "high_watermark_tokens": event.high_watermark_tokens,
            "low_watermark_tokens": event.low_watermark_tokens,
            "source_savings": dict(event.source_savings),
            "checkpoint_id": event.checkpoint_id,
        }
    if isinstance(event, ErrorOccurred):
        return "error", event.error, {"context": event.context}
    return None


__all__ = ["project_feed_event"]
