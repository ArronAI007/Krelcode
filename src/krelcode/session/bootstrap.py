"""会话启动准备：恢复指定会话并挂载 JSONL 录制器。"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Callable, Optional, cast

from ..agent.turn.loop import AgentLoop
from ..core.redact import SecretRedactor
from ..contracts.runtime import RuntimeCheckpointRef
from .continuation import SessionContinuationAdapter
from .memory import MemoryStore, sync_memory_fragment
from .recorder import SessionRecorder, latest_session, new_session_path


def setup_session(
    args: argparse.Namespace,
    loop: AgentLoop,
    log: Callable[[str], None] = print,
    redactor: Optional[SecretRedactor] = None,
) -> Optional[Path]:
    """恢复历史（--resume）并挂载 JSONL 录制器；返回会话文件路径或 None。"""
    sessions_dir = loop.tool_context.workspace_root / ".krelcode" / "sessions"
    continuation = SessionContinuationAdapter(loop)
    resumed_from = ""
    resumed_path = None

    if args.resume:
        if args.resume == "latest":
            path = latest_session(sessions_dir)
            if path is None:
                log(f"（{sessions_dir} 下没有可恢复的会话，从空白开始）")
            else:
                if continuation.resume_session(path, log=log) is not None:
                    resumed_from = path.stem
                    resumed_path = path
        else:
            resumed_path = Path(args.resume)
            if continuation.resume_session(resumed_path, log=log) is not None:
                resumed_from = resumed_path.stem

    if args.no_session:
        return None
    session_path = new_session_path(sessions_dir)
    memory_store = loop.services.session.memory
    if memory_store is not None and resumed_from:
        memory_store.copy_session(resumed_from, session_path.stem)
    metadata = {
        "session_id": session_path.stem,
        "title": "",
        "parent_id": resumed_from,
        "provider": str(getattr(loop.services.core.llm, "name", "") or ""),
        "model": str(getattr(loop.services.core.llm, "model", "") or ""),
        "workspace": str(loop.tool_context.workspace_root),
        "created_at": time.time(),
    }
    recorder = SessionRecorder(
        session_path,
        lambda: loop.history,
        redactor=redactor,
        metadata=metadata,
    )
    recorder.attach(loop.services.core.bus)
    loop.services.session.recorder = recorder
    if memory_store is not None:
        memory_store.bind_session(session_path.stem)
        sync_memory_fragment(loop, cast(MemoryStore, memory_store))
    context_store = loop.services.context.checkpoint
    if context_store is not None and resumed_path is not None:
        source_checkpoint = context_store.latest_for_session(resumed_from)
        if source_checkpoint is not None:
            copied = context_store.copy_reference(
                source_checkpoint,
                session_id=session_path.stem,
                parent_checkpoint_id=source_checkpoint.id,
            )
            if copied is not None:
                recorder.record_context_checkpoint(copied.metadata_record())
                continuation.resume(RuntimeCheckpointRef(
                    checkpoint_id=copied.id,
                    kind="context",
                    session_id=session_path.stem,
                    turn_id=copied.turn_id,
                ))
    return session_path
