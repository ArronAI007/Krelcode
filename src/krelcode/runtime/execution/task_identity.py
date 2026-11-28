"""Stable idempotency fingerprints for submitted runtime tasks."""

from __future__ import annotations

import hashlib
import json

from ...contracts.runtime import RuntimeTask


def task_request_digest(task: RuntimeTask) -> str:
    payload = {
        "message": task.message,
        "application": task.application.to_dict(),
        "parent_task_id": task.parent_task_id,
        "tool_allowlist": list(task.tool_allowlist or ()),
        "reminder_ids": list(task.reminder_ids),
        "images": [image.data_b64 for image in task.images],
        "continuation": (
            task.continuation.to_dict()
            if task.continuation is not None else None),
    }
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()


__all__ = ["task_request_digest"]
