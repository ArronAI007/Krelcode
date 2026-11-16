"""会话启动准备：恢复指定会话并挂载 JSONL 录制器。"""

from __future__ import annotations

import time
from pathlib import Path

from ..agent.loop import AgentLoop
from .memory import sync_memory_fragment
from .recorder import SessionRecorder, latest_session, new_session_path
from .restore import restore_session


def setup_session(args, loop: AgentLoop, log=print, redactor=None):
    """恢复历史（--resume）并挂载 JSONL 录制器；返回会话文件路径或 None。"""
    sessions_dir = loop.tool_context.workspace_root / ".krelcode" / "sessions"
    resumed_from = ""

    if args.resume:
        if args.resume == "latest":
            path = latest_session(sessions_dir)
            if path is None:
                log(f"（{sessions_dir} 下没有可恢复的会话，从空白开始）")
            else:
                if restore_session(loop, path, log=log):
                    resumed_from = path.stem
        else:
            resumed_path = Path(args.resume)
            if restore_session(loop, resumed_path, log=log):
                resumed_from = resumed_path.stem

    if args.no_session:
        return None
    session_path = new_session_path(sessions_dir)
    metadata = {
        "session_id": session_path.stem,
        "title": "",
        "parent_id": resumed_from,
        "provider": str(getattr(loop.llm, "name", "") or ""),
        "model": str(getattr(loop.llm, "model", "") or ""),
        "workspace": str(loop.tool_context.workspace_root),
        "created_at": time.time(),
    }
    recorder = SessionRecorder(
        session_path,
        lambda: loop.history,
        redactor=redactor,
        metadata=metadata,
    )
    recorder.attach(loop.bus)
    loop.session_recorder = recorder
    memory_store = getattr(loop, "memory_store", None)
    if memory_store is not None:
        memory_store.bind_session(session_path.stem)
        sync_memory_fragment(loop, memory_store)
    return session_path
