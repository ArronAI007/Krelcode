"""Tool for waiting on a generic external event resume token."""

from __future__ import annotations

import json
from typing import Mapping

from ...contracts.json import JsonObject
from ...contracts.tool import ToolContext, ToolResult


class ExternalEventWaitTool:
    name = "wait_external_event"
    read_only = True
    description = (
        "Wait for an external event using a resume token. Returns the event "
        "payload after a webhook, scheduler, queue or remote system resumes it."
    )
    parameters: JsonObject = {
        "type": "object",
        "properties": {
            "resume_token": {"type": "string"},
            "event_type": {"type": "string"},
            "timeout": {"type": "number"},
        },
        "required": ["resume_token"],
    }

    async def execute(
        self,
        args: Mapping[str, object],
        ctx: ToolContext,
    ) -> ToolResult:
        broker = ctx.services.external_events
        if broker is None:
            return ToolResult(
                content="External event broker 未配置",
                is_error=True,
                data={"error": "external_events_unavailable"},
            )
        token = str(args.get("resume_token") or "").strip()
        if not token:
            return ToolResult(
                content="resume_token 不能为空",
                is_error=True,
            )
        event_type = str(args.get("event_type") or "").strip()
        timeout = min(600.0, max(0.001, _float_value(
            args.get("timeout"), 60.0)))
        event = await broker.wait(
            token,
            event_type=event_type,
            timeout=timeout,
        )
        if event is None:
            return ToolResult(
                content=f"等待外部事件超时: {token}",
                is_error=True,
                data={
                    "resume_token": token,
                    "event_type": event_type,
                    "timed_out": True,
                },
            )
        return ToolResult(
            content=json.dumps(
                event.as_record(),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
            data={
                "event_id": event.event_id,
                "event_type": event.event_type,
                "resume_token": event.resume_token,
            },
        )


__all__ = ["ExternalEventWaitTool"]


def _float_value(value: object, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default
