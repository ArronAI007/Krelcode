"""Agent-to-application acceptance evidence adapter."""

from __future__ import annotations

from typing import Iterable

from ...contracts.acceptance import AcceptanceEvidence
from ...core.messages import Message


def acceptance_evidence_from_history(
    history: Iterable[Message],
) -> tuple[AcceptanceEvidence, ...]:
    evidence: list[AcceptanceEvidence] = []
    for message in history:
        if message.role != "tool":
            continue
        content = message.content or ""
        name = message.tool_name or "tool"
        evidence.append(AcceptanceEvidence(
            kind="tool-result",
            source=name,
            passed=not bool(message.tool_is_error),
            summary=f"{len(content)} chars",
            metadata={
                "tool_call_id": message.tool_call_id or "",
                "tool_name": name,
            },
        ))
        if name in {"run_command", "job_output"} and not message.tool_is_error:
            evidence.append(AcceptanceEvidence(
                kind="test-result",
                source=name,
                passed=True,
                summary=content[:240],
                metadata={"tool_call_id": message.tool_call_id or ""},
            ))
    return tuple(evidence)


__all__ = [
    "acceptance_evidence_from_history",
]
