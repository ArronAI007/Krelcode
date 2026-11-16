"""多会话管理：列举/描述会话文件（REPL /sessions、/resume 的数据源）。"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional


@dataclass(frozen=True)
class SessionInfo:
    path: Path
    mtime: float
    num_messages: int      # 最后一份 history 快照的消息数
    num_runs: int          # run_finished 次数
    preview: str           # 第一条用户输入（截断）
    session_id: str = ""
    title: str = ""
    parent_id: str = ""
    provider: str = ""
    model: str = ""
    workspace: str = ""
    search_text: str = field(default="", repr=False, compare=False)

    @property
    def time_str(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.mtime))


def inspect_session(path: Path) -> Optional[SessionInfo]:
    """扫描一个 JSONL 会话文件；无法解析时返回 None。"""
    num_runs = 0
    preview = ""
    metadata: dict[str, Any] = {}
    searchable: list[str] = []
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
                    searchable.append(str(record.get("content", "")))
                elif rtype == "user_input":
                    searchable.append(str(record.get("content", "")))
                elif rtype == "session_start":
                    metadata.update({
                        key: value for key, value in record.items()
                        if key not in {"type", "ts"}
                    })
                elif rtype == "session_meta":
                    metadata.update({
                        key: value for key, value in record.items()
                        if key not in {"type", "ts"}
                    })
                elif rtype == "history":
                    messages = record.get("messages", [])
                    if isinstance(messages, list):
                        last_history_len = len(messages)
                        for message in messages:
                            if not isinstance(message, dict):
                                continue
                            if message.get("role") != "user":
                                continue
                            content = str(message.get("content") or "")
                            if not preview and content:
                                preview = content[:60]
                            searchable.append(content)
    except OSError:
        return None
    if num_runs == 0 and last_history_len == 0 and not preview:
        return None  # 空会话不列出
    return SessionInfo(
        path=path, mtime=path.stat().st_mtime,
        num_messages=last_history_len, num_runs=num_runs, preview=preview,
        session_id=str(metadata.get("session_id") or path.stem),
        title=str(metadata.get("title") or ""),
        parent_id=str(metadata.get("parent_id") or ""),
        provider=str(metadata.get("provider") or ""),
        model=str(metadata.get("model") or ""),
        workspace=str(metadata.get("workspace") or ""),
        search_text="\n".join(searchable),
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


def search_sessions(
    sessions_dir: Path,
    query: str,
) -> List[SessionInfo]:
    """按标题、首条输入及全部用户输入做大小写无关搜索。"""
    needle = query.strip().casefold()
    if not needle:
        return list_sessions(sessions_dir)
    matches = []
    for info in list_sessions(sessions_dir):
        haystack = "\n".join((
            info.title, info.preview, info.search_text, str(info.path)))
        if needle in haystack.casefold():
            matches.append(info)
    return matches


def format_sessions(infos: List[SessionInfo]) -> str:
    if not infos:
        return "  （暂无会话）"
    lines = []
    for idx, info in enumerate(infos, start=1):
        label = info.title or info.preview or "(无预览)"
        lines.append(
            f"  [{idx}] {info.time_str} · {info.num_runs} 轮 · "
            f"{info.num_messages} 条消息 · {label}"
        )
        lines.append(f"      {info.path}")
    return "\n".join(lines)



def resolve_resume_target(target: str, sessions_dir: Optional[Path]) -> Optional[Path]:
    """把 latest / 序号 / 文件路径解析为会话文件。"""
    if sessions_dir is None:
        return Path(target) if Path(target).is_file() else None
    if target == "latest":
        infos = list_sessions(sessions_dir)
        return infos[0].path if infos else None
    if target.isdigit():
        infos = list_sessions(sessions_dir)
        idx = int(target)
        if 1 <= idx <= len(infos):
            return infos[idx - 1].path
        return None
    path = Path(target)
    return path if path.is_file() else None


def resolve_session_target(
    target: str,
    sessions_dir: Optional[Path],
) -> Optional[Path]:
    """解析会话序号/路径；与 resume 不同，不特殊处理 latest。"""
    if target.isdigit() and sessions_dir is not None:
        infos = list_sessions(sessions_dir)
        index = int(target)
        if 1 <= index <= len(infos):
            return infos[index - 1].path
        return None
    path = Path(target).expanduser()
    return path if path.is_file() else None


def append_session_metadata(path: Path, **metadata: Any) -> None:
    """向已有 JSONL 追加元数据覆盖记录。"""
    record = {"type": "session_meta", "ts": time.time(), **metadata}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def archive_session(path: Path, sessions_dir: Path) -> Path:
    """把会话移到可恢复归档目录；避免不可逆删除。"""
    archive_dir = sessions_dir / ".archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = archive_dir / f"{stamp}-{path.name}"
    counter = 1
    while destination.exists():
        destination = archive_dir / f"{stamp}-{counter}-{path.name}"
        counter += 1
    shutil.move(str(path), str(destination))
    return destination
