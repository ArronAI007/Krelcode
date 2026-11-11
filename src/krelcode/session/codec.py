"""会话存储的消息编解码（session 私有格式，与 provider wire format 无关）。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.messages import ImageRef, Message, ToolCall


def message_to_dict(m: Message) -> Dict[str, Any]:
    d: Dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        d["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in m.tool_calls
        ]
    if m.tool_call_id is not None:
        d["tool_call_id"] = m.tool_call_id
    if m.provider_meta:
        d["provider_meta"] = m.provider_meta
    if m.images:
        # 只存源路径不存 base64（防每轮快照把 JSONL 撑爆）；恢复时按路径重载，
        # 源文件已删则丢弃该图（可接受的退化，UI 有占位文本）
        d["images"] = [
            {"media_type": i.media_type, "source_path": i.source_path}
            for i in m.images
        ]
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
    meta = d.get("provider_meta")
    return Message(
        role=d.get("role", "assistant"),
        content=d.get("content"),
        tool_calls=tool_calls,
        tool_call_id=d.get("tool_call_id"),
        provider_meta=meta if isinstance(meta, dict) else None,
        images=_reload_images(d.get("images")),
    )


def _reload_images(raw: Any) -> Optional[List[ImageRef]]:
    """按 source_path 重载图片字节；缺失/不可读的单张丢弃。"""
    if not isinstance(raw, list):
        return None
    images: List[ImageRef] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = item.get("source_path")
        if not source:
            continue
        path = Path(source)
        if not path.is_file():
            continue
        try:
            data_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            continue
        images.append(ImageRef(
            media_type=str(item.get("media_type") or "image/png"),
            data_b64=data_b64,
            source_path=source,
        ))
    return images or None


def history_to_dicts(history: List[Message]) -> List[Dict[str, Any]]:
    return [message_to_dict(m) for m in history]


def history_from_dicts(raw: List[Dict[str, Any]]) -> List[Message]:
    return [message_from_dict(d) for d in raw]
