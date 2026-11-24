"""Daemon-backed subagent manager and remote handle DTO."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast

from ..core.redact import SecretRedactor
from ..subagent.feed import AgentFeedEntry, AgentFeedPage
from ..subagent.sidechain import SidechainStore, read_sidechain
from .client_transport import DaemonClient, DaemonUnavailable
from .protocol import DEFAULT_RPC_TIMEOUT, RPCError


@dataclass
class RemoteAgentHandle:
    id: str
    task: str
    status: str = "running"
    result: Optional[str] = None
    error: Optional[str] = None
    turns: int = 0
    queue: List[str] = field(default_factory=list)
    worktree: Optional[Path] = None
    branch: Optional[str] = None
    forked: bool = False
    definition: str = ""
    sidechain_path: Optional[Path] = None
    model: str = ""
    provider: str = ""
    permission_mode: str = ""
    mcp_servers: Tuple[str, ...] = field(default_factory=tuple)
    latest_sequence: int = 0

    def preview(self) -> str:
        text = self.result or self.error or ""
        if len(text) > 160:
            text = text[:160] + "…"
        return text.replace("\n", " ")


class DaemonSubagentManager:
    """SubagentManager-compatible facade backed by the workspace daemon."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        config_path: Optional[Path] = None,
        client: Optional[DaemonClient] = None,
        redactor: Optional[SecretRedactor] = None,
        idle_timeout: Optional[float] = None,
        feed_max_entries: int = 2_000,
        feed_max_entry_chars: int = 24_000,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.client = client or DaemonClient(
            self.workspace_root, config_path=config_path, redactor=redactor)
        self._redactor = redactor
        self.client.redactor = redactor
        self.idle_timeout = idle_timeout
        self.feed_max_entries = feed_max_entries
        self.feed_max_entry_chars = feed_max_entry_chars
        self.sidechains = SidechainStore(self.workspace_root, redactor=redactor)
        # Compatibility attributes assigned by the composition root.  They do
        # not control the remote manager; model changes require a daemon restart
        # until an explicit remote reconfigure RPC is introduced.
        self.llm: Optional[object] = None
        self.prompt_profile = ""
        self.prompt_release_id = ""
        self.prompt_calibrations: Sequence[object] = ()
        self.hook_port = None
        self.get_parent_history = None
        self.cache_ledger_provider = None
        self._cache: Dict[str, RemoteAgentHandle] = {}

    @property
    def redactor(self) -> Optional[SecretRedactor]:
        return self._redactor

    @redactor.setter
    def redactor(self, value: Optional[SecretRedactor]) -> None:
        self._redactor = value
        self.client.redactor = value
        self.sidechains.redactor = value

    async def _request(
        self,
        operation: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = DEFAULT_RPC_TIMEOUT,
    ) -> Any:
        await self.client.ensure_started(idle_timeout=self.idle_timeout)
        return await self.client.request(
            f"agent.{operation}", params or {}, timeout=timeout)

    def _sync_list(self) -> List[Dict[str, Any]]:
        try:
            rows = self.client._request_sync("agent.list", {})
        except (DaemonUnavailable, RPCError):
            return []
        return [item for item in rows if isinstance(item, dict)]

    async def spawn(
        self,
        task: str,
        isolated: Optional[bool] = None,
        fork_context: Optional[bool] = None,
        *,
        definition: Optional[str] = None,
    ) -> RemoteAgentHandle:
        data = await self._request("spawn", {
            "task": task,
            "isolated": isolated,
            "fork_context": fork_context,
            "definition": definition,
        }, timeout=60.0)
        handle = _remote_handle(data)
        self._cache[handle.id] = handle
        return handle

    async def send_input(
        self, agent_id: str, message: str, interrupt: bool = False,
    ) -> Tuple[RemoteAgentHandle, str]:
        data = await self._request("send_input", {
            "agent_id": agent_id,
            "message": message,
            "interrupt": bool(interrupt),
        })
        handle = _remote_handle(data.get("agent") or {})
        self._cache[handle.id] = handle
        return handle, str(data.get("action") or "")

    async def pause(self, agent_id: str) -> RemoteAgentHandle:
        return await self._control("pause", agent_id)

    async def resume(self, agent_id: str) -> RemoteAgentHandle:
        return await self._control("resume", agent_id)

    async def kill(self, agent_id: str) -> RemoteAgentHandle:
        return await self._control("kill", agent_id)

    async def _control(self, operation: str, agent_id: str) -> RemoteAgentHandle:
        data = await self._request(operation, {"agent_id": agent_id}, timeout=20.0)
        handle = _remote_handle(data.get("agent") or {})
        self._cache[handle.id] = handle
        return handle

    async def wait(
        self, agent_id: str, timeout: Optional[float] = None,
    ) -> RemoteAgentHandle:
        data = await self._request("wait", {
            "agent_id": agent_id,
            "timeout": 60.0 if timeout is None else float(timeout),
        }, timeout=(60.0 if timeout is None else float(timeout)) + 5.0)
        handle = _remote_handle(data)
        self._cache[handle.id] = handle
        return handle

    async def wait_many(
        self, agent_ids: List[str], timeout: Optional[float] = None,
    ) -> List[RemoteAgentHandle]:
        return [await self.wait(agent_id, timeout=timeout) for agent_id in agent_ids]

    def get(self, agent_id: str) -> Optional[RemoteAgentHandle]:
        if agent_id in self._cache:
            return self._cache[agent_id]
        for item in self._sync_list():
            if str(item.get("agent_id") or item.get("id")) == agent_id:
                return _remote_handle(item)
        return None

    @staticmethod
    def is_running(handle: RemoteAgentHandle) -> bool:
        return handle.status in {"running", "paused", "queued"}

    def feed(
        self, agent_id: str, after_sequence: int = 0, limit: int = 200,
    ) -> AgentFeedPage:
        try:
            data = self.client._request_sync("agent.feed", {
                "agent_id": agent_id,
                "after_sequence": max(0, int(after_sequence)),
                "limit": max(1, min(int(limit), 1_000)),
            }, timeout=10.0)
        except (DaemonUnavailable, RPCError) as exc:
            raise ValueError(str(exc)) from exc
        entries = tuple(
            AgentFeedEntry(
                agent_id=str(item.get("agent_id") or agent_id),
                sequence=int(item.get("sequence") or 0),
                kind=str(item.get("kind") or "event"),
                text=str(item.get("text") or ""),
                ts=float(item.get("ts") or 0.0),
                data=dict(item.get("data") or {}),
            )
            for item in data.get("entries", [])
            if isinstance(item, dict)
        )
        return AgentFeedPage(
            agent_id=str(data.get("agent_id") or agent_id),
            entries=entries,
            after_sequence=int(data.get("after_sequence") or after_sequence),
            latest_sequence=int(data.get("latest_sequence") or 0),
            dropped_through=int(data.get("dropped_through") or 0),
        )

    def status_lines(self) -> List[str]:
        lines: List[str] = []
        for record in self._sync_list():
            agent_id = str(record.get("agent_id") or record.get("id") or "")
            status = str(record.get("status") or "-")
            turns = int(record.get("turns") or 0)
            task = str(record.get("task") or "").replace("\n", " ")
            if len(task) > 60:
                task = task[:60] + "…"
            definition = (
                f" · 定义 {record.get('definition')}" if record.get("definition") else "")
            sequence = (
                f" · feed {record.get('latest_sequence')}"
                if record.get("latest_sequence") else "")
            line = (f"{agent_id} [{status}] {turns} 轮{definition}{sequence} · 任务: {task}")
            preview = record.get("result") or record.get("error")
            if preview and status in {"completed", "failed", "cancelled", "killed", "lost"}:
                line += f" · 结果: {str(preview)[:160].replace(chr(10), ' ')}"
            lines.append(line)
        return lines

    def definition_lines(self) -> List[str]:
        try:
            data = self.client._request_sync("agent.definitions", {}, timeout=10.0)
            return [str(item) for item in data.get("lines", [])]
        except (DaemonUnavailable, RPCError):
            return []

    def sidechain_lines(self, limit: int = 50) -> List[str]:
        return [
            f"{info.agent_id} [{info.status or '-'}] run={info.run_id} "
            f"records={info.records} · {info.task[:60]} · {info.path}"
            for info in self.sidechains.list(limit=limit)
        ]

    def read_sidechain(self, value: str) -> Tuple[Path, Tuple[Dict[str, object], ...]]:
        path = self.sidechains.resolve(value)
        if path is None:
            raise ValueError(f"未找到 sidechain: {value}")
        return path, read_sidechain(path)

    def build_tools(self) -> List[object]:
        from ..subagent.tools import build_subagent_tools

        return cast(List[object], build_subagent_tools(self))

    async def shutdown(self) -> None:
        # Keep remote subagents alive when the REPL client exits.  They are
        # reclaimed by explicit kill, daemon stop, or daemon crash reconciliation.
        return None


def _remote_handle(raw: Dict[str, Any]) -> RemoteAgentHandle:
    agent_id = str(raw.get("agent_id") or raw.get("id") or "").strip()
    if not agent_id:
        raise ValueError("daemon 返回了无效 agent 信息")
    return RemoteAgentHandle(
        id=agent_id,
        task=str(raw.get("task") or ""),
        status=str(raw.get("status") or "running"),
        result=(str(raw["result"]) if raw.get("result") is not None else None),
        error=(str(raw["error"]) if raw.get("error") is not None else None),
        turns=int(raw.get("turns") or 0),
        queue=[str(item) for item in raw.get("queue") or ()],
        worktree=(Path(str(raw["worktree"])) if raw.get("worktree") else None),
        branch=(str(raw.get("branch") or "") or None),
        forked=bool(raw.get("forked")),
        definition=str(raw.get("definition") or ""),
        sidechain_path=(
            Path(str(raw["sidechain_path"])) if raw.get("sidechain_path") else None),
        model=str(raw.get("model") or ""),
        provider=str(raw.get("provider") or ""),
        permission_mode=str(raw.get("permission_mode") or ""),
        mcp_servers=tuple(str(item) for item in raw.get("mcp_servers") or ()),
        latest_sequence=int(raw.get("latest_sequence") or 0),
    )



__all__ = ["DaemonSubagentManager", "RemoteAgentHandle"]
