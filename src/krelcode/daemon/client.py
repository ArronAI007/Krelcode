"""Daemon RPC client and REPL-compatible manager adapters."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..core.redact import SecretRedactor
from ..core.events import (
    EventBus,
    ShellJobFinished,
    ShellJobOutput,
    ShellJobStarted,
)
from ..jobs.shell import (
    ShellJobError,
    ShellJobInfo,
    ShellJobNotFound,
    ShellJobOutputPage,
    ShellJobStatus,
)
from ..subagent.feed import AgentFeedEntry, AgentFeedPage
from ..subagent.sidechain import SidechainStore, read_sidechain
from .paths import (
    DaemonPaths,
    DaemonStateError,
    atomic_write_json,
    pid_is_alive,
    read_json,
    read_token,
)
from .protocol import (
    DEFAULT_RPC_TIMEOUT,
    RPCError,
    RPCRequest,
    RPCResponse,
    encode_message,
    read_message,
    write_message,
)
from .storage import JobRecordStore
from .transport import DaemonEndpoint, open_connection

DEFAULT_START_TIMEOUT = 12.0


class DaemonUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class DaemonStatus:
    running: bool
    instance_id: str = ""
    pid: int = 0
    workspace_root: str = ""
    endpoint: Dict[str, Any] = field(default_factory=dict)
    data: Dict[str, Any] = field(default_factory=dict)


class DaemonClient:
    def __init__(
        self,
        workspace_root: Path,
        *,
        config_path: Optional[Path] = None,
        redactor: Optional[SecretRedactor] = None,
        sandbox_backend: Optional[str] = None,
        sandbox_network: Optional[bool] = None,
        no_sandbox: bool = False,
        auto_approve: bool = False,
        permission_mode: Optional[str] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.paths = DaemonPaths(self.workspace_root)
        self.config_path = Path(config_path).expanduser().resolve() if config_path else None
        self.redactor = redactor
        self.sandbox_backend = sandbox_backend
        self.sandbox_network = sandbox_network
        self.no_sandbox = bool(no_sandbox)
        self.auto_approve = bool(auto_approve)
        self.permission_mode = permission_mode

    def state(self) -> Optional[Dict[str, Any]]:
        try:
            raw = read_json(self.paths.state)
        except DaemonStateError:
            return None
        if raw is None:
            return None
        workspace = str(raw.get("workspace_root") or "")
        if workspace and Path(workspace).resolve() != self.workspace_root:
            raise DaemonStateError("daemon state 属于其他 workspace")
        return raw

    def endpoint(self) -> Optional[DaemonEndpoint]:
        raw = self.state()
        if raw is None or not isinstance(raw.get("endpoint"), dict):
            return None
        return DaemonEndpoint.from_dict(raw["endpoint"])

    async def request(
        self,
        operation: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = DEFAULT_RPC_TIMEOUT,
        auto_start: bool = False,
    ) -> Any:
        if auto_start:
            await self.ensure_started()
        return await self._request_async(operation, params or {}, timeout=timeout)

    async def _request_async(
        self,
        operation: str,
        params: Dict[str, Any],
        *,
        timeout: float,
    ) -> Any:
        endpoint = self.endpoint()
        if endpoint is None:
            raise DaemonUnavailable("daemon 未运行")
        try:
            token = read_token(self.paths)
        except DaemonStateError as exc:
            raise DaemonUnavailable(str(exc)) from exc
        request = RPCRequest.create(operation, params, token=token)
        if endpoint.kind == "file":
            return await self._request_file_async(request, timeout=timeout)
        try:
            reader, writer = await asyncio.wait_for(
                open_connection(endpoint), timeout=min(5.0, timeout))
        except (OSError, asyncio.TimeoutError) as exc:
            raise DaemonUnavailable(f"无法连接 daemon: {exc}") from exc
        try:
            await write_message(writer, request.to_dict())
            raw = await read_message(reader, timeout=timeout)
            response = RPCResponse.from_dict(raw)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        if response.id != request.id:
            raise DaemonUnavailable("daemon 响应 request id 不匹配")
        if not response.ok:
            error = response.error or {}
            raise RPCError(
                str(error.get("message") or "daemon 请求失败"),
                code=str(error.get("code") or "rpc_error"),
            )
        return response.result

    def _request_sync(
        self,
        operation: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = 5.0,
    ) -> Any:
        endpoint = self.endpoint()
        if endpoint is None:
            raise DaemonUnavailable("daemon 未运行")
        token = read_token(self.paths)
        request = RPCRequest.create(operation, params or {}, token=token)
        if endpoint.kind == "file":
            return self._request_file_sync(request, timeout=timeout)
        payload = encode_message(request.to_dict())
        if endpoint.kind == "unix":
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                sock.connect(endpoint.path)
                return self._sync_roundtrip(sock, payload, request.id)
            finally:
                sock.close()
        sock = socket.create_connection(
            (endpoint.host, endpoint.port), timeout=timeout)
        sock.settimeout(timeout)
        try:
            return self._sync_roundtrip(sock, payload, request.id)
        finally:
            sock.close()

    @staticmethod
    def _sync_roundtrip(sock: socket.socket, payload: bytes, request_id: str) -> Any:
        sock.sendall(payload)
        chunks = bytearray()
        while b"\n" not in chunks:
            block = sock.recv(65_536)
            if not block:
                break
            chunks.extend(block)
            if len(chunks) > 4 * 1024 * 1024:
                raise DaemonUnavailable("daemon 同步响应过大")
        try:
            raw = json.loads(bytes(chunks).split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DaemonUnavailable(f"daemon 同步响应无效: {exc}") from exc
        response = RPCResponse.from_dict(raw)
        if response.id != request_id:
            raise DaemonUnavailable("daemon 同步响应 request id 不匹配")
        if not response.ok:
            error = response.error or {}
            raise RPCError(
                str(error.get("message") or "daemon 请求失败"),
                code=str(error.get("code") or "rpc_error"),
            )
        return response.result

    async def _request_file_async(self, request: RPCRequest, *, timeout: float) -> Any:
        request_path = self.paths.requests_dir / f"{request.id}.json"
        response_path = self.paths.responses_dir / f"{request.id}.json"
        await asyncio.to_thread(atomic_write_json, request_path, request.to_dict())
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            if response_path.is_file():
                try:
                    raw = json.loads(await asyncio.to_thread(
                        response_path.read_text, "utf-8"))
                except FileNotFoundError:
                    continue
                finally:
                    try:
                        response_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                return self._checked_response(raw, request.id)
            await asyncio.sleep(0.03)
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise DaemonUnavailable("daemon file RPC 响应超时")

    def _request_file_sync(self, request: RPCRequest, *, timeout: float) -> Any:
        request_path = self.paths.requests_dir / f"{request.id}.json"
        response_path = self.paths.responses_dir / f"{request.id}.json"
        atomic_write_json(request_path, request.to_dict())
        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            if response_path.is_file():
                try:
                    raw = json.loads(response_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    continue
                finally:
                    try:
                        response_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                return self._checked_response(raw, request.id)
            time.sleep(0.03)
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise DaemonUnavailable("daemon file RPC 响应超时")

    @staticmethod
    def _checked_response(raw: Dict[str, Any], request_id: str) -> Any:
        response = RPCResponse.from_dict(raw)
        if response.id != request_id:
            raise DaemonUnavailable("daemon file 响应 request id 不匹配")
        if not response.ok:
            error = response.error or {}
            raise RPCError(
                str(error.get("message") or "daemon 请求失败"),
                code=str(error.get("code") or "rpc_error"),
            )
        return response.result

    async def ping(self, *, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
        try:
            result = await self._request_async("ping", {}, timeout=timeout)
        except (DaemonUnavailable, RPCError, OSError):
            return None
        return result if isinstance(result, dict) else None

    async def ensure_started(
        self,
        *,
        timeout: float = DEFAULT_START_TIMEOUT,
        idle_timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        current = await self.ping()
        if current is not None:
            return current
        state = self.state()
        if state is not None:
            pid = int(state.get("pid") or 0)
            if pid_is_alive(pid):
                # Give a just-starting daemon a short readiness window before
                # attempting a competing spawn that the lock will reject.
                deadline = time.monotonic() + min(3.0, timeout)
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                    current = await self.ping()
                    if current is not None:
                        return current
        self._spawn(idle_timeout=idle_timeout)
        deadline = time.monotonic() + max(1.0, timeout)
        last_error = ""
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            current = await self.ping()
            if current is not None:
                return current
            try:
                if self.paths.daemon_log.is_file():
                    last_error = self.paths.daemon_log.read_text(
                        encoding="utf-8", errors="replace")[-2_000:]
            except OSError:
                pass
        raise DaemonUnavailable(
            "daemon 启动超时" + (f"\n{last_error}" if last_error else ""))

    def _spawn(self, *, idle_timeout: Optional[float]) -> None:
        self.paths.ensure()
        argv = [
            sys.executable,
            "-m", "krelcode.daemon",
            "--workspace", str(self.workspace_root),
        ]
        if self.config_path is not None:
            argv += ["--config", str(self.config_path)]
        if idle_timeout is not None:
            argv += ["--idle-timeout", str(float(idle_timeout))]
        if self.sandbox_backend:
            argv += ["--sandbox-backend", str(self.sandbox_backend)]
        if self.sandbox_network is True:
            argv.append("--sandbox-network")
        if self.no_sandbox:
            argv.append("--no-sandbox")
        if self.auto_approve:
            argv.append("--auto-approve")
        if self.permission_mode:
            argv += ["--permission-mode", str(self.permission_mode)]
        kwargs: Dict[str, Any] = {
            "cwd": str(self.workspace_root),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
            "env": os.environ.copy(),
        }
        if os.name == "posix":
            kwargs["start_new_session"] = True
        elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        log = self.paths.daemon_log.open("ab", buffering=0)
        try:
            kwargs["stdout"] = log
            kwargs["stderr"] = log
            subprocess.Popen(argv, **kwargs)
        finally:
            log.close()

    async def start(self, *, idle_timeout: Optional[float] = None) -> DaemonStatus:
        await self.ensure_started(idle_timeout=idle_timeout)
        return await self.status()

    async def stop(self, *, timeout: float = 10.0) -> bool:
        try:
            await self._request_async("shutdown", {}, timeout=5.0)
        except (DaemonUnavailable, RPCError):
            return False
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            if await self.ping(timeout=0.5) is None:
                return True
        return False

    async def status(self) -> DaemonStatus:
        try:
            data = await self._request_async("status", {}, timeout=3.0)
        except (DaemonUnavailable, RPCError):
            return DaemonStatus(False)
        if not isinstance(data, dict):
            return DaemonStatus(False)
        return DaemonStatus(
            running=True,
            instance_id=str(data.get("instance_id") or ""),
            pid=int(data.get("pid") or 0),
            workspace_root=str(data.get("workspace_root") or ""),
            endpoint=dict(data.get("endpoint") or {}),
            data=data,
        )


class DaemonJobManager:
    """Drop-in ShellJobManager adapter whose lifecycle is daemon-owned."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        config_path: Optional[Path] = None,
        client: Optional[DaemonClient] = None,
        redactor: Optional[SecretRedactor] = None,
        idle_timeout: Optional[float] = None,
        bus: Optional[EventBus] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.client = client or DaemonClient(
            self.workspace_root, config_path=config_path, redactor=redactor)
        self._redactor = redactor
        self.client.redactor = redactor
        self.idle_timeout = idle_timeout
        self.bus = bus
        self._cache: Dict[str, ShellJobInfo] = {}
        self._poll_task: Optional[asyncio.Task] = None
        self._seen_status: Dict[str, str] = {}
        self._seen_sequence: Dict[str, int] = {}
        self._closed = False

    @property
    def redactor(self) -> Optional[SecretRedactor]:
        return self._redactor

    @redactor.setter
    def redactor(self, value: Optional[SecretRedactor]) -> None:
        self._redactor = value
        self.client.redactor = value

    def list_jobs(self) -> List[ShellJobInfo]:
        try:
            rows = self.client._request_sync("job.list", {})
        except (DaemonUnavailable, RPCError):
            store = JobRecordStore(DaemonPaths(self.workspace_root), redactor=self.redactor)
            rows = [_job_info_dict(item.info()) for item in store.list()]
        jobs = [_job_from_dict(item) for item in rows if isinstance(item, dict)]
        self._cache = {job.job_id: job for job in jobs}
        return jobs

    def get(self, job_id: str) -> ShellJobInfo:
        target = str(job_id or "").strip()
        if target in self._cache:
            return self._cache[target]
        for job in self.list_jobs():
            if job.job_id == target:
                return job
        raise ShellJobNotFound(f"找不到后台任务: {job_id}")

    async def start(
        self,
        command: str,
        *,
        cwd: Path,
        sandbox=None,
    ) -> ShellJobInfo:
        await self.client.ensure_started(idle_timeout=self.idle_timeout)
        params: Dict[str, Any] = {
            "command": str(command or ""),
            "cwd": str(Path(cwd).resolve()),
        }
        if sandbox is not None:
            params["sandbox_backend"] = getattr(sandbox, "backend", "")
            params["sandbox_network"] = bool(getattr(sandbox, "network", False))
        data = await self.client.request("job.start", params, timeout=30.0)
        job = _job_from_dict(data)
        self._cache[job.job_id] = job
        if self.bus is not None:
            self._seen_status[job.job_id] = job.status.value
            self._seen_sequence[job.job_id] = 0
            await self._safe_emit(ShellJobStarted(
                job_id=job.job_id,
                command=job.command,
                pid=job.pid,
                cwd=job.cwd,
            ))
            self._ensure_polling()
        return job

    async def output(
        self,
        job_id: str,
        *,
        after_sequence: int = 0,
        max_chars: int = 30_000,
        wait_seconds: float = 0.0,
    ) -> ShellJobOutputPage:
        try:
            data = await self.client.request(
                "job.output",
                {
                    "job_id": job_id,
                    "after_sequence": int(after_sequence),
                    "max_chars": int(max_chars),
                    "wait_seconds": float(wait_seconds),
                },
                timeout=max(5.0, min(float(wait_seconds) + 5.0, 40.0)),
            )
        except (DaemonUnavailable, RPCError) as exc:
            store = JobRecordStore(DaemonPaths(self.workspace_root), redactor=self.redactor)
            page = store.output(
                job_id,
                after_sequence=after_sequence,
                max_chars=max_chars,
            )
            if page is not None:
                return ShellJobOutputPage(
                    job=page.job,
                    text=page.text,
                    next_sequence=page.next_sequence,
                    truncated=page.truncated,
                )
            raise ShellJobError(str(exc)) from exc
        job = _job_from_dict(data.get("job") or {})
        self._cache[job.job_id] = job
        return ShellJobOutputPage(
            job=job,
            text=str(data.get("text") or ""),
            next_sequence=int(data.get("next_sequence") or 0),
            truncated=bool(data.get("truncated")),
        )

    async def kill(self, job_id: str) -> ShellJobInfo:
        try:
            data = await self.client.request(
                "job.kill", {"job_id": job_id}, timeout=15.0)
        except (DaemonUnavailable, RPCError) as exc:
            raise ShellJobError(str(exc)) from exc
        job = _job_from_dict(data)
        self._cache[job.job_id] = job
        return job

    async def shutdown(self) -> None:
        # Deliberately does not stop the daemon: AgentRuntime.close() only ends
        # this client session.  Background jobs survive until explicit kill,
        # daemon shutdown, or configured idle timeout.
        self._closed = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
            self._poll_task = None

    def _ensure_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(
                self._poll_events(), name="krelcode-daemon-job-events")

    async def _poll_events(self) -> None:
        while not self._closed:
            try:
                rows = await self.client.request("job.list", {}, timeout=5.0)
                for item in rows:
                    job_id = str(item.get("job_id") or "")
                    if not job_id:
                        continue
                    job = _job_from_dict(item)
                    previous_sequence = self._seen_sequence.get(job_id, 0)
                    previous_status = self._seen_status.get(job_id, "")
                    if not previous_status:
                        await self._safe_emit(ShellJobStarted(
                            job_id=job.job_id,
                            command=job.command,
                            pid=job.pid,
                            cwd=job.cwd,
                        ))
                    if job.latest_sequence > previous_sequence:
                        page = await self.output(
                            job_id, after_sequence=previous_sequence)
                        self._seen_sequence[job_id] = page.next_sequence
                        if page.text:
                            await self._safe_emit(ShellJobOutput(
                                job_id=job_id,
                                sequence=page.next_sequence,
                                text=page.text,
                            ))
                    if (job.status.value != previous_status
                            and job.status is not ShellJobStatus.RUNNING
                            and previous_status):
                        try:
                            tail = await self.output(
                                job_id, after_sequence=0, max_chars=600)
                            preview = tail.text.strip()
                        except Exception:
                            preview = ""
                        await self._safe_emit(ShellJobFinished(
                            job_id=job.job_id,
                            command=job.command,
                            status=job.status.value,
                            exit_code=job.exit_code,
                            preview=preview,
                        ))
                    self._seen_status[job_id] = job.status.value
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.5)

    async def _safe_emit(self, event) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.emit(event)
        except Exception:
            pass


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

    def read_sidechain(self, value: str) -> Tuple[Path, Tuple[dict, ...]]:
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


def _job_from_dict(raw: Dict[str, Any]) -> ShellJobInfo:
    if not isinstance(raw, dict) or not raw.get("job_id"):
        raise ShellJobError("daemon 返回了无效 job 信息")
    return ShellJobInfo(
        job_id=str(raw.get("job_id") or ""),
        command=str(raw.get("command") or ""),
        cwd=str(raw.get("cwd") or ""),
        pid=int(raw.get("pid") or 0),
        status=ShellJobStatus(str(raw.get("status") or "lost")),
        started_at=float(raw.get("started_at") or 0.0),
        ended_at=(float(raw["ended_at"]) if raw.get("ended_at") is not None else None),
        exit_code=(int(raw["exit_code"]) if raw.get("exit_code") is not None else None),
        latest_sequence=int(raw.get("latest_sequence") or 0),
        log_path=str(raw.get("log_path") or ""),
    )


def _job_info_dict(info: ShellJobInfo) -> Dict[str, Any]:
    return {
        "job_id": info.job_id,
        "command": info.command,
        "cwd": info.cwd,
        "pid": info.pid,
        "status": info.status.value,
        "started_at": info.started_at,
        "ended_at": info.ended_at,
        "exit_code": info.exit_code,
        "latest_sequence": info.latest_sequence,
        "log_path": info.log_path,
    }


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


__all__ = [
    "DaemonClient",
    "DaemonJobManager",
    "DaemonStatus",
    "DaemonSubagentManager",
    "DaemonUnavailable",
    "RemoteAgentHandle",
]
