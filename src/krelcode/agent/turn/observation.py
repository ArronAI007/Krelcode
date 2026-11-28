"""Factories that normalize heterogeneous feedback into Observation."""

from __future__ import annotations

import hashlib

from krelcode.contracts.loop import (
    Observation,
    ObservationSource,
    ObservationStatus,
    WaitKind,
)
from krelcode.contracts.tool_execution import ToolExecutionResult
from krelcode.core.messages import ToolCall


def tool_observation(
    *,
    turn_id: str,
    tool_call: ToolCall,
    result: ToolExecutionResult,
) -> Observation:
    payload = result.to_tool_result()
    source = (
        ObservationSource.MCP
        if result.runtime.value == "mcp"
        else ObservationSource.SUBAGENT
        if tool_call.name in {"spawn_agent", "wait_agent", "send_input"}
        else ObservationSource.JOB
        if tool_call.name.startswith("job_")
        else ObservationSource.TOOL
    )
    digest = hashlib.sha256(payload.content.encode("utf-8")).hexdigest()
    evidence_ids = tuple(
        str(payload.data.get(key) or "")
        for key in ("artifact_id", "checkpoint_id")
        if payload.data.get(key)
    )
    return Observation(
        observation_id=f"obs-tool-{tool_call.id}-{digest[:12]}",
        turn_id=turn_id,
        source=source,
        kind=f"tool:{tool_call.name}",
        status=(
            ObservationStatus.ERROR
            if payload.is_error else ObservationStatus.OK),
        summary=(
            f"{tool_call.name} failed"
            if payload.is_error else f"{tool_call.name} completed"
        ),
        data={
            "tool_call_id": tool_call.id,
            "tool_name": tool_call.name,
            "runtime": result.runtime.value,
            "content_chars": len(payload.content),
            "content_hash": digest,
            "diagnostics": dict(result.diagnostics),
            "result_data": dict(payload.data),
        },
        trust="untrusted" if source in {
            ObservationSource.MCP,
            ObservationSource.SUBAGENT,
        } else "trusted",
        requires_validation=source in {
            ObservationSource.MCP,
            ObservationSource.SUBAGENT,
        },
        evidence_ids=evidence_ids,
    )


def steering_observation(
    *,
    turn_id: str,
    text: str,
) -> Observation:
    normalized = str(text or "").strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return Observation(
        observation_id=f"obs-user-{digest[:16]}",
        turn_id=turn_id,
        source=ObservationSource.USER,
        kind="user.steering",
        summary="user steering received",
        data={
            "chars": len(normalized),
            "content_hash": digest,
        },
        trust="trusted",
    )


def user_input_observation(
    *,
    turn_id: str,
    request_id: str,
    answered: bool,
    answer_chars: int,
) -> Observation:
    return Observation(
        observation_id=f"obs-input-{request_id}",
        turn_id=turn_id,
        source=ObservationSource.USER_INPUT,
        kind="user.input",
        status=(
            ObservationStatus.OK
            if answered else ObservationStatus.PENDING),
        summary=(
            f"user input answered ({answer_chars} chars)"
            if answered else "user input not provided"
        ),
        data={
            "request_id": request_id,
            "answered": answered,
            "answer_chars": max(0, int(answer_chars)),
        },
        trust="trusted",
    )


def tool_wait_kind(tool_name: str) -> WaitKind | None:
    if tool_name in {"wait_agent", "send_input"}:
        return WaitKind.SUBAGENT
    if tool_name in {"job_output", "job_wait"}:
        return WaitKind.JOB
    return None


__all__ = [
    "steering_observation",
    "tool_observation",
    "tool_wait_kind",
    "user_input_observation",
]
