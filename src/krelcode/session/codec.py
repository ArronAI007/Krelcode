"""会话存储的消息编解码（session 私有格式，与 provider wire format 无关）。"""

from __future__ import annotations

from typing import Any, Dict, List

from ..core.messages import Message, ToolCall


def message_to_dict(m: Message) -> Dict[str, Any]:
    d: Dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        d["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in m.tool_calls
        ]
    if m.tool_call_id is not None:
        d["tool_call_id"] = m.tool_call_id
    return d


def message_from_dict(d: Dict[str, Any]) -> Message:
    tool_calls = None
    if d.get("tool_calls"):
        tool_calls = [
            ToolCall(
                id=tc.get("id", ""),
                name=tc.get("name", ""),
                arguments=dict(tc.get("arguments") or {}),
            )
            for tc in d["tool_calls"]
        ]
    return Message(
        role=d.get("role", "assistant"),
        content=d.get("content"),
        tool_calls=tool_calls,
        tool_call_id=d.get("tool_call_id"),
    )


def history_to_dicts(history: List[Message]) -> List[Dict[str, Any]]:
    return [message_to_dict(m) for m in history]


def history_from_dicts(raw: List[Dict[str, Any]]) -> List[Message]:
    return [message_from_dict(d) for d in raw]
