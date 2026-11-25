"""会话导出：history → Markdown（分享/存档/给其他工具消费）。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..core.redact import SecretRedactor
from ..core.messages import Message
from ..core.codec import history_to_dicts

TOOL_RESULT_PREVIEW = 2_000
ARGS_PREVIEW = 300


def render_markdown(history: List[Message], title: str = "Krelcode 会话") -> str:
    lines: List[str] = [f"# {title}", "",
                        f"_导出时间: {time.strftime('%Y-%m-%d %H:%M:%S')}_", ""]
    for m in history:
        if m.role == "system":
            lines += ["## System", "", "```", m.content or "", "```", ""]
        elif m.role == "user":
            lines += ["## 👤 User", "", m.content or "", ""]
            for img in m.images or []:
                lines += [f"🖼 _图片附件: {img.source_path or '(内联)'} ({img.media_type})_", ""]
        elif m.role == "assistant":
            lines += ["## 🤖 Assistant", ""]
            if m.content:
                lines += [m.content, ""]
            for tc in m.tool_calls or []:
                try:
                    args = json.dumps(tc.arguments, ensure_ascii=False)
                except (TypeError, ValueError):
                    args = repr(tc.arguments)
                if len(args) > ARGS_PREVIEW:
                    args = args[:ARGS_PREVIEW] + "…"
                lines.append(f"- 🔧 `{tc.name}` `{args}`")
            if m.tool_calls:
                lines.append("")
        elif m.role == "tool":
            content = m.content or ""
            if len(content) > TOOL_RESULT_PREVIEW:
                content = content[:TOOL_RESULT_PREVIEW] + "\n…[截断]"
            lines += [f"### 📎 Tool Result ({m.tool_call_id})", "",
                      "```", content, "```", ""]
    return "\n".join(lines)


def export_session(history: List[Message], path: Optional[Path] = None,
                   export_dir: Optional[Path] = None, *,
                   redactor: Optional[SecretRedactor] = None) -> Path:
    """写出 Markdown；path 缺省时落到 export_dir/session-<ts>.md。"""
    if path is None:
        directory = Path(export_dir or Path("."))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"session-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    text = render_markdown(history)
    if redactor is not None:
        text = redactor.redact(text)
    path.write_text(text, encoding="utf-8")
    return path


def export_native_session(
    history: List[Message],
    path: Optional[Path] = None,
    *,
    metadata: Optional[Mapping[str, Any]] = None,
    export_dir: Optional[Path] = None,
    redactor: Optional[SecretRedactor] = None,
) -> Path:
    """写出可重新导入的 JSON 快照（/resume 可直接加载）。"""
    if path is None:
        directory = Path(export_dir or Path("."))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"session-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path = Path(path)
    if path.suffix.lower() != ".json":
        path = path.with_suffix(path.suffix + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "format": "krelcode-session",
        "format_version": 1,
        "exported_at": time.time(),
        "metadata": dict(metadata or {}),
        "messages": history_to_dicts(history),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if redactor is not None:
        text = redactor.redact(text)
    path.write_text(text, encoding="utf-8")
    return path
