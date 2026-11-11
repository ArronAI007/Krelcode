"""会话导出：history → Markdown（分享/存档/给其他工具消费）。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Optional

from ..core.messages import Message

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
                   export_dir: Optional[Path] = None) -> Path:
    """写出 Markdown；path 缺省时落到 export_dir/session-<ts>.md。"""
    if path is None:
        directory = Path(export_dir or Path("."))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"session-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path = Path(path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(history), encoding="utf-8")
    return path
