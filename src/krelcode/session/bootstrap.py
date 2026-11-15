"""会话启动准备：恢复指定会话并挂载 JSONL 录制器。"""

from __future__ import annotations

from pathlib import Path

from ..agent.loop import AgentLoop
from .recorder import SessionRecorder, latest_session, new_session_path
from .restore import restore_session


def setup_session(args, loop: AgentLoop, log=print, redactor=None):
    """恢复历史（--resume）并挂载 JSONL 录制器；返回会话文件路径或 None。"""
    sessions_dir = loop.tool_context.workspace_root / ".krelcode" / "sessions"

    if args.resume:
        if args.resume == "latest":
            path = latest_session(sessions_dir)
            if path is None:
                log(f"（{sessions_dir} 下没有可恢复的会话，从空白开始）")
            else:
                restore_session(loop, path, log=log)
        else:
            restore_session(loop, Path(args.resume), log=log)

    if args.no_session:
        return None
    session_path = new_session_path(sessions_dir)
    recorder = SessionRecorder(session_path, lambda: loop.history, redactor=redactor)
    recorder.attach(loop.bus)
    return session_path
