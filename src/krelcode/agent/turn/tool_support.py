"""Small pure helpers shared by tool execution."""

from __future__ import annotations

import json
import re
from typing import Callable

from krelcode.core.messages import ToolCall


def call_signature(tool_call: ToolCall) -> str:
    try:
        args = json.dumps(
            tool_call.arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        args = repr(tool_call.arguments)
    if len(args) > 200:
        args = args[:200] + "…"
    return f"{tool_call.name}({args})"


def redact_json(
    value: object,
    redact: Callable[[str], str],
) -> object:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_json(item, redact) for item in value]
    if isinstance(value, dict):
        return {
            str(redact(str(key))): redact_json(item, redact)
            for key, item in value.items()
        }
    return value


def persisted_output_path(content: str) -> str:
    match = re.search(r"全文已存盘:\s*([^\]\n]+)", content or "")
    return match.group(1).strip()[:1_000] if match else ""


__all__ = ["call_signature", "persisted_output_path", "redact_json"]
