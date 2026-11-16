"""主会话生命周期：分支、恢复、重命名、归档与录制器切换。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .memory import sync_memory_fragment
from .manager import (
    SessionInfo,
    append_session_metadata,
    archive_session,
    inspect_session,
    resolve_session_target,
)
from .recorder import SessionRecorder, new_session_path
from .restore import restore_session


class SessionLifecycleError(RuntimeError):
    """会话操作无法完成。"""


class SessionConfirmationRequired(SessionLifecycleError):
    """删除会话需要明确确认。"""

    def __init__(self, target: str) -> None:
        super().__init__(f"删除会话需要确认: {target}")
        self.target = target


@dataclass(frozen=True)
class DeletedSession:
    source: Path
    archive_path: Path


class SessionLifecycle:
    """独占当前会话文件切换，保证 loop、recorder 与 runtime id 一致。"""

    def __init__(
        self,
        loop,
        sessions_dir: Optional[Path],
        session_path: Optional[str] = None,
        *,
        runtime=None,
        redactor=None,
    ) -> None:
        self.loop = loop
        self.sessions_dir = Path(sessions_dir) if sessions_dir else None
        self.session_path = Path(session_path) if session_path else None
        self.runtime = runtime
        self.redactor = redactor
        self.on_switch: Optional[Callable[[], None]] = None
        memory_store = getattr(loop, "memory_store", None)
        if memory_store is not None and self.session_path is not None:
            memory_store.bind_session(self.session_path.stem)
            sync_memory_fragment(loop, memory_store)

    @property
    def metadata(self) -> dict[str, Any]:
        recorder = getattr(self.loop, "session_recorder", None)
        if recorder is not None and Path(recorder.path) == self.session_path:
            return dict(recorder.metadata)
        return {}

    def _new_metadata(
        self,
        path: Path,
        *,
        title: str,
        parent_id: str = "",
    ) -> dict[str, Any]:
        return {
            "session_id": path.stem,
            "title": title,
            "parent_id": parent_id,
            "provider": str(getattr(self.loop.llm, "name", "") or ""),
            "model": str(getattr(self.loop.llm, "model", "") or ""),
            "workspace": str(self.loop.tool_context.workspace_root),
            "created_at": time.time(),
        }

    def _flush_current(self) -> None:
        recorder = getattr(self.loop, "session_recorder", None)
        if recorder is not None and Path(recorder.path) == self.session_path:
            recorder.flush_history()

    def _activate(
        self,
        path: Path,
        *,
        title: str,
        parent_id: str,
    ) -> None:
        if self.sessions_dir is None:
            raise SessionLifecycleError("会话目录未知，无法切换会话")
        old = getattr(self.loop, "session_recorder", None)
        if old is not None:
            old.detach()
        metadata = self._new_metadata(
            path, title=title, parent_id=parent_id)
        recorder = SessionRecorder(
            path,
            lambda: self.loop.history,
            redactor=self.redactor,
            metadata=metadata,
        )
        recorder.attach(self.loop.bus)
        recorder.flush_history()
        self.loop.session_recorder = recorder
        self.session_path = path
        if self.runtime is not None:
            self.runtime.bind_session(path.stem)
        memory_store = getattr(self.loop, "memory_store", None)
        if memory_store is not None:
            memory_store.bind_session(path.stem)
            sync_memory_fragment(self.loop, memory_store)
        if self.on_switch is not None:
            self.on_switch()

    def fork(
        self,
        title: str = "",
        *,
        history: Optional[list] = None,
    ) -> Path:
        """建立独立会话；可指定历史边界，原会话与工作区保持不变。"""
        if self.sessions_dir is None:
            raise SessionLifecycleError("会话目录未知，无法 fork")
        title = title.strip() or f"Fork · {time.strftime('%Y-%m-%d %H:%M:%S')}"
        self._validate_title(title)
        self._flush_current()
        parent_id = self.session_path.stem if self.session_path else ""
        if history is not None:
            self.loop.history = list(history)
        rebuild = getattr(self.loop, "rebuild_working_context", None)
        if callable(rebuild):
            rebuild()
        path = new_session_path(self.sessions_dir)
        self._activate(path, title=title, parent_id=parent_id)
        return path

    def resume(self, path: Path, log: Callable[[str], None] = print) -> bool:
        """恢复历史后建立新分支，避免覆盖被恢复的原始会话。"""
        path = Path(path)
        old_path = self.session_path
        if old_path is not None and path.resolve() == old_path.resolve():
            restored = restore_session(self.loop, path, log=log)
            if restored and self.on_switch is not None:
                self.on_switch()
            return restored
        info = inspect_session(path)
        parent_id = info.session_id if info is not None else path.stem
        title_source = (info.title or info.preview) if info is not None else ""
        title = f"Resumed · {title_source}" if title_source else "Resumed session"
        self._flush_current()
        if not restore_session(self.loop, path, log=log):
            return False
        if self.sessions_dir is not None:
            self._activate(new_session_path(self.sessions_dir),
                           title=title[:80], parent_id=parent_id)
        return True

    def rename(self, title: str) -> Path:
        title = title.strip()
        self._validate_title(title)
        if self.session_path is None or not self.session_path.is_file():
            raise SessionLifecycleError("当前没有可重命名的会话")
        recorder = getattr(self.loop, "session_recorder", None)
        if recorder is not None and Path(recorder.path) == self.session_path:
            recorder.update_metadata(title=title)
        else:
            append_session_metadata(self.session_path, title=title)
        return self.session_path

    def delete(self, target: str, *, confirmed: bool = False) -> DeletedSession:
        if self.sessions_dir is None:
            raise SessionLifecycleError("会话目录未知，无法删除")
        path = resolve_session_target(target, self.sessions_dir)
        if path is None:
            raise SessionLifecycleError(f"找不到会话: {target}")
        path = path.resolve()
        root = self.sessions_dir.resolve()
        if path.parent != root:
            raise SessionLifecycleError("只能删除当前工作区会话目录中的会话")
        if self.session_path is not None and path == self.session_path.resolve():
            raise SessionLifecycleError("不能删除当前活动会话；先 /fork 或 /resume 切换")
        if not confirmed:
            raise SessionConfirmationRequired(target)
        return DeletedSession(path, archive_session(path, self.sessions_dir))

    @staticmethod
    def _validate_title(title: str) -> None:
        if not title or "\n" in title or "\r" in title:
            raise SessionLifecycleError("会话名称不能为空或包含换行")
        if len(title) > 80:
            raise SessionLifecycleError("会话名称最多 80 个字符")
