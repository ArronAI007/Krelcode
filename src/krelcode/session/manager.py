"""多会话管理：列举/描述会话文件（REPL /sessions、/resume 的数据源）。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass(frozen=True)
class SessionInfo:
    path: Path
    mtime: float
    num_messages: int      # 最后一份 history 快照的消息数
    num_runs: int          # run_finished 次数
    preview: str           # 第一条用户输入（截断）

    @property
    def time_str(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.mtime))


def inspect_session(path: Path) -> Optional[SessionInfo]:
    """扫描一个 JSONL 会话文件；无法解析时返回 None。"""
    num_runs = 0
    preview = ""
    last_history_len = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = record.get("type")
                if rtype == "run_finished":
                    num_runs += 1
                elif rtype == "user_input" and not preview:
                    preview = str(record.get("content", ""))[:60]
                elif rtype == "history":
                    last_history_len = len(record.get("messages", []))
    except OSError:
        return None
    if num_runs == 0 and last_history_len == 0 and not preview:
        return None  # 空会话不列出
    return SessionInfo(
        path=path, mtime=path.stat().st_mtime,
        num_messages=last_history_len, num_runs=num_runs, preview=preview,
    )


def list_sessions(sessions_dir: Path) -> List[SessionInfo]:
    """按最近使用排序（新→旧）。目录不存在返回空表。"""
    if not sessions_dir.is_dir():
        return []
    infos = []
    for path in sessions_dir.glob("*.jsonl"):
        info = inspect_session(path)
        if info is not None:
            infos.append(info)
    infos.sort(key=lambda i: i.mtime, reverse=True)
    return infos


def format_sessions(infos: List[SessionInfo]) -> str:
    if not infos:
        return "  （暂无会话）"
    lines = []
    for idx, info in enumerate(infos, start=1):
        lines.append(
            f"  [{idx}] {info.time_str} · {info.num_runs} 轮 · "
            f"{info.num_messages} 条消息 · {info.preview or '(无预览)'}"
        )
        lines.append(f"      {info.path}")
    return "\n".join(lines)
