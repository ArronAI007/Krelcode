"""共享 SSE 行解析器（SSETransport 与 StreamableHTTPTransport 复用）。

按 SSE 规范：空行为事件边界；event:/data: 字段；':' 开头为注释；
data 多行以 \n 连接；无 event 字段时事件名默认 "message"。
同时解析 `id:`（last event ID，供断线重连 `Last-Event-ID` 恢复）与
`retry:`（服务器建议的重连毫秒数），旧调用方仍只消费 (event, data)。
"""

from __future__ import annotations

from typing import List, Optional, Tuple


class SSEEventParser:
    def __init__(self) -> None:
        self._event = ""
        self._data: List[str] = []
        # SSE 规范：id 字段出现即更新 last event ID（跨事件保留）
        self.last_event_id: Optional[str] = None
        # 服务器建议的重连间隔（毫秒）；None = 未下发
        self.retry_ms: Optional[int] = None

    def feed(self, line: str) -> Optional[Tuple[str, str]]:
        """喂一行（不含换行符）；到达事件边界时返回 (event_name, data)。"""
        if line == "":
            if not self._data:
                self._event = ""
                return None
            data = "\n".join(self._data)
            event = self._event or "message"
            self._event = ""
            self._data = []
            return event, data
        if line.startswith(":"):
            return None  # 注释/心跳
        if line.startswith("event:"):
            self._event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            self._data.append(line[len("data:"):].lstrip())
        elif line.startswith("id:"):
            self.last_event_id = line[len("id:"):].lstrip()
        elif line.startswith("retry:"):
            raw = line[len("retry:"):].strip()
            try:
                self.retry_ms = max(0, int(raw))
            except ValueError:
                pass
        return None

    def flush(self) -> Optional[Tuple[str, str]]:
        """流结束时若有未派发事件则补发（无尾随空行的场景）。"""
        return self.feed("")
