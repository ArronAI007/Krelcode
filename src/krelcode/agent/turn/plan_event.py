"""Build redacted structured plan events for the event bus."""

from __future__ import annotations

from typing import Callable, Optional

from krelcode.agent.turn.tool_support import redact_json
from krelcode.contracts.plan import PlanControl
from krelcode.contracts.plan_ops import plan_snapshot_views
from krelcode.core.events import PlanUpdated
from krelcode.core.redact import SecretRedactor


def build_plan_control_event(
    control: PlanControl,
    *,
    turn_id: str,
    redactor: Optional[SecretRedactor],
) -> tuple[
    PlanUpdated,
    tuple[str, ...],
    tuple[str, ...],
    int,
    str,
]:
    redact: Callable[[str], str] = (
        (lambda text: redactor.redact(text) or "")
        if redactor is not None else lambda text: text
    )
    items, statuses, active_index, explanation = plan_snapshot_views(control)
    payload = control.as_dict()
    if redactor is not None:
        redacted = redact_json(payload, redactor.redact)
        if isinstance(redacted, dict):
            payload = redacted
    event = PlanUpdated(
        items=tuple(redact(item) for item in items),
        active_index=active_index,
        statuses=tuple(redact(status) for status in statuses),
        explanation=redact(explanation),
        turn_id=turn_id,
        plan_control=payload,
    )
    return event, items, statuses, active_index, redact(explanation)


__all__ = ["build_plan_control_event"]
