"""Read-only subagent observation and history projection."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from ..core.messages import Message, sanitize_history
from .definition import AgentDefinitionManager
from .feed import AgentFeedPage
from .models import AgentHandle
from .presentation import (
    format_definition_lines,
    format_sidechain_lines,
    format_status_lines,
)
from .sidechain import SidechainStore, read_sidechain


class MemoryEntryPort(Protocol):
    @property
    def id(self) -> str: ...


class MemoryStorePort(Protocol):
    def search(
        self,
        query: str,
        *,
        limit: int = 8,
    ) -> Sequence[MemoryEntryPort]: ...


class SubagentReadMixin:
    """Expose stable manager reads without coupling them to lifecycle code."""

    definitions: AgentDefinitionManager
    sidechains: SidechainStore
    memory_store: Optional[MemoryStorePort]
    get_parent_history: Optional[Callable[[], List[Message]]]
    _handles: Dict[str, AgentHandle]

    def _relevant_memory(self, task: str) -> Tuple[MemoryEntryPort, ...]:
        if self.memory_store is None:
            return ()
        parent_tail = ""
        get_parent_history = self.get_parent_history
        if get_parent_history is not None:
            for message in reversed(get_parent_history() or ()):
                if message.role != "user" or not message.content:
                    continue
                text = " ".join(str(message.content).split())
                if text.startswith("[上下文摘要]") or text.startswith("[用户插话]"):
                    continue
                parent_tail = text[:2_000]
                break
        query = "\n".join(item for item in (parent_tail, task) if item)
        return tuple(self.memory_store.search(query, limit=8)) if query else ()

    def _fork_parent_history(self) -> Tuple[List[Message], Optional[str]]:
        get_parent_history = self.get_parent_history
        source = get_parent_history() if get_parent_history is not None else []
        start = 1 if source and source[0].role == "system" else 0
        forked = [
            replace(m, tool_calls=list(m.tool_calls) if m.tool_calls else None)
            for m in source[start:]
        ]
        sanitize_history(forked)
        pending_user: Optional[str] = None
        if forked and forked[-1].role == "user":
            pending_user = forked.pop().content or ""
        return forked, pending_user

    def feed(
        self, agent_id: str, after_sequence: int = 0, limit: int = 200,
    ) -> AgentFeedPage:
        handle = self._require_handle(agent_id)
        if handle.feed is None:
            raise ValueError(f"子代理 {agent_id} 没有 feed")
        return handle.feed.page(after_sequence, limit)

    def definition_lines(self) -> List[str]:
        return format_definition_lines(self.definitions)

    def sidechain_lines(self, limit: int = 50) -> List[str]:
        return format_sidechain_lines(self.sidechains, limit=limit)

    def read_sidechain(
        self,
        value: str,
    ) -> Tuple[Path, Tuple[dict[str, object], ...]]:
        path = self.sidechains.resolve(value)
        if path is None:
            raise ValueError(f"未找到 sidechain: {value}")
        return path, read_sidechain(path)

    def _require_handle(self, agent_id: str) -> AgentHandle:
        handle = self._handles.get(agent_id)
        if handle is None:
            raise KeyError(f"未知子代理: {agent_id}")
        return handle

    def status_lines(self) -> List[str]:
        return format_status_lines(self._handles.values())


__all__ = [
    "MemoryEntryPort",
    "MemoryStorePort",
    "SubagentReadMixin",
]
