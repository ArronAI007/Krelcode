"""Single-workspace asyncio daemon for durable jobs and subagents."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..config import load_config_layers
from ..core.events import (
    EventBus,
    ShellJobFinished,
    ShellJobOutput,
    ShellJobStarted,
    SubagentFeedUpdated,
    SubagentFinished,
    SubagentSpawned,
    SubagentStateChanged,
)
from ..core.redact import SecretRedactor
from ..jobs import ShellJobError, ShellJobManager, ShellJobNotFound, ShellJobStatus
from ..safety.sandbox import ExecutionSandbox
from .paths import (
    DAEMON_SCHEMA_VERSION,
    DaemonPaths,
    DaemonStateError,
    InstanceLock,
    atomic_write_json,
    generate_token,
    new_instance_id,
    read_json,
    read_token,
    secure_write_text,
)
from .protocol import (
    DEFAULT_RPC_TIMEOUT,
    ProtocolError,
    RPCError,
    RPCRequest,
    RPCResponse,
    read_message,
    write_message,
)
from .storage import AgentRecordStore, DurableJobRecord, JobRecordStore
from .transport import (
    DaemonEndpoint,
    choose_endpoint,
    prepare_unix_socket,
)

DAEMON_IDLE_CHECK_SECONDS = 15.0


@dataclass(frozen=True)
class DaemonAgentRuntimeRequest:
    """Inputs needed by the composition root to build daemon-owned agents."""

    workspace_root: Path
    config_path: Optional[Path]
    config: Dict[str, Any]
    no_sandbox: bool = False
    sandbox_backend: Optional[str] = None
    sandbox_network: Optional[bool] = None
    auto_approve: bool = False
    permission_mode: Optional[str] = None
    log: Callable[[str], None] = print


@dataclass(frozen=True)
class DaemonAgentRuntime:
    """Concrete agent runtime returned by an injected composition factory."""

    loop: Any
    agents: Any
    mcp_supervisor: Any = None


DaemonAgentRuntimeFactory = Callable[
    [DaemonAgentRuntimeRequest], Awaitable[DaemonAgentRuntime]
]


class DaemonServer:
    def __init__(
        self,
        workspace_root: Path,
        *,
        config_path: Optional[Path] = None,
        config: Optional[Dict[str, Any]] = None,
        idle_timeout: float = 1_800.0,
        sandbox_backend: Optional[str] = None,
        sandbox_network: Optional[bool] = None,
        no_sandbox: bool = False,
        auto_approve: bool = False,
        permission_mode: Optional[str] = None,
        agent_runtime_factory: Optional[DaemonAgentRuntimeFactory] = None,
        log=print,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.paths = DaemonPaths(self.workspace_root).ensure()
        self.config_path = Path(config_path).expanduser().resolve() if config_path else None
        self.config = dict(config or {})
        self.idle_timeout = max(0.0, float(idle_timeout))
        self.sandbox_backend = sandbox_backend
        self.sandbox_network = sandbox_network
        self.no_sandbox = bool(no_sandbox)
        self.auto_approve = bool(auto_approve)
        self.permission_mode = permission_mode
        self.agent_runtime_factory = agent_runtime_factory
        self.log = log
        self.instance_id = new_instance_id()
        self.started_at = time.time()
        self.instance_token = generate_token()
        self.lock = InstanceLock(self.paths)
        self.bus = EventBus()
        self.redactor = SecretRedactor()
        self.job_store = JobRecordStore(self.paths, redactor=self.redactor)
        self.agent_store = AgentRecordStore(self.paths, redactor=self.redactor)
        self.jobs = ShellJobManager(
            self.bus, workspace_root=self.workspace_root, redactor=self.redactor)
        self._server: Optional[asyncio.AbstractServer] = None
        self._endpoint: Optional[DaemonEndpoint] = None
        self._stop_event = asyncio.Event()
        self._serve_task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._file_task: Optional[asyncio.Task] = None
        self._file_workers: set[asyncio.Task] = set()
        self._last_activity = time.monotonic()
        self._agent_loop = None
        self._agent_mcp_supervisor = None
        self._agents = None
        self._agent_lock = asyncio.Lock()
        self._closed = False
        self._started = False
        self.bus.subscribe(self._on_job_event)

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> DaemonEndpoint:
        if self._started and self._endpoint is not None:
            return self._endpoint
        state = {
            "schema_version": DAEMON_SCHEMA_VERSION,
            "instance_id": self.instance_id,
            "workspace_root": str(self.workspace_root),
            "started_at": self.started_at,
            "pid": os.getpid(),
            "protocol_version": 1,
        }
        self.lock.acquire(state)
        try:
            endpoint = choose_endpoint(self.paths)
            stream_started = False
            if endpoint.kind != "file":
                try:
                    if endpoint.kind == "unix":
                        prepare_unix_socket(endpoint)
                        self._server = await asyncio.start_unix_server(
                            self._handle_client,
                            path=endpoint.path,
                            limit=4 * 1024 * 1024,
                        )
                        if os.name == "posix":
                            os.chmod(endpoint.path, 0o600)
                    else:
                        self._server = await asyncio.start_server(
                            self._handle_client,
                            host=endpoint.host,
                            port=endpoint.port,
                            limit=4 * 1024 * 1024,
                        )
                    if endpoint.kind == "tcp":
                        socket = next(iter(self._server.sockets or ()), None)
                        if socket is None:
                            raise DaemonStateError("daemon TCP server 未绑定端口")
                        endpoint = DaemonEndpoint(
                            "tcp", host=endpoint.host,
                            port=int(socket.getsockname()[1]))
                    stream_started = True
                except (PermissionError, OSError) as exc:
                    self.log(f"[daemon] stream transport 不可用，回退 file RPC: {exc}")
                    if self._server is not None:
                        self._server.close()
                        await self._server.wait_closed()
                        self._server = None
            if not stream_started:
                endpoint = DaemonEndpoint("file")
                self._prepare_file_transport()
                self._file_task = asyncio.create_task(
                    self._file_rpc_loop(), name="krelcode-daemon-file-rpc")
            self._endpoint = endpoint
            secure_write_text(self.paths.token, self.instance_token + "\n")
            atomic_write_json(self.paths.state, {
                **state,
                "endpoint": endpoint.to_dict(),
                "token_file": str(self.paths.token),
                "idle_timeout": self.idle_timeout,
                "sandbox": {
                    "enabled": not self.no_sandbox,
                    "backend": self.sandbox_backend or "config/auto",
                },
            })
            lost_jobs = self.job_store.reconcile(self.instance_id)
            lost_agents = self.agent_store.reconcile(self.instance_id)
            if lost_jobs or lost_agents:
                self.log(
                    "[daemon] 恢复 durable metadata: "
                    f"lost jobs={len(lost_jobs)} lost agents={len(lost_agents)}")
            self._idle_task = asyncio.create_task(
                self._idle_loop(), name="krelcode-daemon-idle")
            self._started = True
            return endpoint
        except Exception:
            await self._cleanup_runtime_files()
            self.lock.release()
            raise

    async def serve_forever(self) -> None:
        if not self._started:
            await self.start()
        await self._stop_event.wait()
        await self.stop()

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        if self._idle_task is not None:
            self._idle_task.cancel()
            await asyncio.gather(self._idle_task, return_exceptions=True)
            self._idle_task = None
        if self._file_task is not None:
            self._file_task.cancel()
            await asyncio.gather(self._file_task, return_exceptions=True)
            self._file_task = None
        if self._file_workers:
            for task in list(self._file_workers):
                task.cancel()
            await asyncio.gather(*self._file_workers, return_exceptions=True)
            self._file_workers.clear()
        if self._agents is not None:
            try:
                await self._agents.shutdown()
            except Exception:
                pass
        if self._agent_mcp_supervisor is not None:
            try:
                await self._agent_mcp_supervisor.close_all()
            except Exception:
                pass
            self._agent_mcp_supervisor = None
        if self._agent_loop is not None:
            lsp_manager = getattr(self._agent_loop, "lsp_manager", None)
            if lsp_manager is not None:
                try:
                    await lsp_manager.close()
                except Exception:
                    pass
        try:
            await self.jobs.shutdown()
        finally:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
            await self._cleanup_runtime_files()
            self.lock.release()

    async def _cleanup_runtime_files(self) -> None:
        endpoint = self._endpoint
        if endpoint is not None and endpoint.kind == "unix":
            try:
                Path(endpoint.path).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        for path in (self.paths.state, self.paths.token):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        for directory in (self.paths.requests_dir, self.paths.responses_dir):
            try:
                for item in directory.glob("*.json"):
                    item.unlink(missing_ok=True)
            except OSError:
                pass

    def _prepare_file_transport(self) -> None:
        for directory in (self.paths.requests_dir, self.paths.responses_dir):
            directory.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                try:
                    os.chmod(directory, 0o700)
                except OSError:
                    pass
            for item in directory.glob("*.json"):
                try:
                    item.unlink()
                except OSError:
                    pass

    async def _file_rpc_loop(self) -> None:
        while not self._closed:
            try:
                paths = sorted(self.paths.requests_dir.glob("*.json"))
            except OSError:
                await asyncio.sleep(0.1)
                continue
            for path in paths:
                task = asyncio.create_task(
                    self._handle_file_request(path),
                    name=f"krelcode-daemon-file-{path.stem}",
                )
                self._file_workers.add(task)
                task.add_done_callback(self._file_workers.discard)
            await asyncio.sleep(0.05)

    async def _handle_file_request(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            path.unlink(missing_ok=True)
            if not isinstance(raw, dict):
                return
            request = RPCRequest.from_dict(raw)
            if not hmac.compare_digest(request.token, self.instance_token):
                response = RPCResponse.failure(
                    request.id, "daemon token 无效", code="unauthorized")
            else:
                self._touch()
                try:
                    response = RPCResponse.success(
                        request.id, await self.dispatch(request))
                except RPCError as exc:
                    response = RPCResponse.failure(request.id, str(exc), code=exc.code)
                except (KeyError, ShellJobNotFound) as exc:
                    response = RPCResponse.failure(request.id, str(exc), code="not_found")
                except (ValueError, ShellJobError, DaemonStateError) as exc:
                    response = RPCResponse.failure(
                        request.id, str(exc), code="invalid_request")
                except Exception as exc:
                    response = RPCResponse.failure(
                        request.id, f"{type(exc).__name__}: {exc}",
                        code="internal_error")
            atomic_write_json(self.paths.responses_dir / f"{request.id}.json", response.to_dict())
        except Exception as exc:
            self.log(
                f"[daemon] file RPC 处理失败 {path.name}: "
                f"{type(exc).__name__}: {exc}")
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _idle_loop(self) -> None:
        if self.idle_timeout <= 0:
            return
        interval = min(DAEMON_IDLE_CHECK_SECONDS, max(1.0, self.idle_timeout / 2))
        while not self._closed:
            await asyncio.sleep(interval)
            if self._has_active_work():
                self._touch()
                continue
            if time.monotonic() - self._last_activity >= self.idle_timeout:
                self.log("[daemon] idle timeout，正在退出")
                asyncio.create_task(self.stop(), name="krelcode-daemon-idle-stop")
                return

    def _has_active_work(self) -> bool:
        if any(job.running for job in self.jobs.list_jobs()):
            return True
        if self._agents is not None:
            try:
                if any(self._agents.is_running(handle)
                       for handle in self._agents._handles.values()):
                    return True
            except Exception:
                pass
        return False

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    # -- RPC -------------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        request_id = ""
        try:
            raw = await read_message(reader, timeout=30.0)
            request = RPCRequest.from_dict(raw)
            request_id = request.id
            if not hmac.compare_digest(request.token, self.instance_token):
                response = RPCResponse.failure(
                    request.id, "daemon token 无效", code="unauthorized")
            else:
                self._touch()
                try:
                    result = await self.dispatch(request)
                    response = RPCResponse.success(request.id, result)
                except RPCError as exc:
                    response = RPCResponse.failure(request.id, str(exc), code=exc.code)
                except (KeyError, ShellJobNotFound) as exc:
                    response = RPCResponse.failure(request.id, str(exc), code="not_found")
                except (ValueError, ShellJobError, DaemonStateError) as exc:
                    response = RPCResponse.failure(request.id, str(exc), code="invalid_request")
                except Exception as exc:
                    self.log(f"[daemon] {request.operation} 失败: {type(exc).__name__}: {exc}")
                    response = RPCResponse.failure(
                        request.id, f"{type(exc).__name__}: {exc}", code="internal_error")
            await write_message(writer, response.to_dict())
        except ProtocolError as exc:
            try:
                await write_message(
                    writer, RPCResponse.failure(request_id, str(exc), code=exc.code).to_dict())
            except Exception:
                pass
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def dispatch(self, request: RPCRequest) -> Any:
        op = request.operation
        params = request.params
        if op == "ping":
            return {"ok": True, "instance_id": self.instance_id}
        if op == "status":
            return self.status()
        if op == "shutdown":
            asyncio.create_task(self._delayed_stop())
            return {"stopping": True, "instance_id": self.instance_id}
        if op.startswith("job."):
            return await self._dispatch_job(op[4:], params)
        if op.startswith("agent."):
            return await self._dispatch_agent(op[6:], params)
        raise RPCError(f"未知 daemon operation: {op}", code="unknown_operation")

    async def _delayed_stop(self) -> None:
        await asyncio.sleep(0.1)
        await self.stop()

    def status(self) -> Dict[str, Any]:
        durable_jobs = self.job_store.list()
        return {
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "workspace_root": str(self.workspace_root),
            "started_at": self.started_at,
            "uptime_seconds": max(0.0, time.time() - self.started_at),
            "idle_timeout": self.idle_timeout,
            "endpoint": self._endpoint.to_dict() if self._endpoint else None,
            "active_jobs": sum(job.running for job in self.jobs.list_jobs()),
            "durable_jobs": len(durable_jobs),
            "agents": len(self.agent_store.list()),
            "agent_runtime_loaded": self._agents is not None,
            "issues": [*self.job_store.issues[-10:], *self.agent_store.issues[-10:]],
        }

    # -- jobs ------------------------------------------------------------------

    async def _on_job_event(self, event) -> None:
        try:
            if isinstance(event, ShellJobStarted):
                info = self.jobs.get(event.job_id)
                await asyncio.to_thread(
                    self.job_store.upsert_from_info,
                    info,
                    daemon_instance_id=self.instance_id,
                )
            elif isinstance(event, ShellJobOutput):
                await asyncio.to_thread(
                    self.job_store.append_output,
                    event.job_id,
                    event.sequence,
                    event.text,
                )
            elif isinstance(event, ShellJobFinished):
                await asyncio.to_thread(
                    self.job_store.mark_terminal,
                    event.job_id,
                    status=event.status,
                    exit_code=event.exit_code,
                )
        except Exception as exc:
            self.job_store.issues.append(f"job event 持久化失败: {exc}")

    async def _dispatch_job(self, operation: str, params: Dict[str, Any]) -> Any:
        if operation == "start":
            command = str(params.get("command") or "").strip()
            if not command:
                raise ValueError("command 不能为空")
            cwd_text = str(params.get("cwd") or self.workspace_root)
            cwd = Path(cwd_text)
            if not cwd.is_absolute():
                cwd = self.workspace_root / cwd
            sandbox = self._build_sandbox()
            info = await self.jobs.start(command, cwd=cwd, sandbox=sandbox)
            await asyncio.to_thread(
                self.job_store.upsert_from_info,
                info,
                daemon_instance_id=self.instance_id,
            )
            return _job_info_dict(info)
        if operation == "list":
            return self._merged_jobs()
        if operation == "output":
            job_id = _required(params, "job_id")
            try:
                page = await self.jobs.output(
                    job_id,
                    after_sequence=int(params.get("after_sequence") or 0),
                    max_chars=int(params.get("max_chars") or 30_000),
                    wait_seconds=float(params.get("wait_seconds") or 0.0),
                )
                return {
                    "job": _job_info_dict(page.job),
                    "text": page.text,
                    "next_sequence": page.next_sequence,
                    "truncated": page.truncated,
                }
            except ShellJobNotFound:
                page = self.job_store.output(
                    job_id,
                    after_sequence=int(params.get("after_sequence") or 0),
                    max_chars=int(params.get("max_chars") or 30_000),
                )
                if page is None:
                    raise
                return {
                    "job": _job_info_dict(page.job),
                    "text": page.text,
                    "next_sequence": page.next_sequence,
                    "truncated": page.truncated,
                }
        if operation == "kill":
            job_id = _required(params, "job_id")
            try:
                info = await self.jobs.kill(job_id)
            except ShellJobNotFound:
                record = self.job_store.load(job_id)
                if record is not None and record.status == ShellJobStatus.LOST.value:
                    raise RPCError(
                        "该 job 属于已退出的 daemon，无法安全按 PID 终止；"
                        "请人工确认并在工作区内处理残留进程",
                        code="orphaned_job",
                    )
                raise
            await asyncio.to_thread(
                self.job_store.upsert_from_info,
                info,
                daemon_instance_id=self.instance_id,
            )
            return _job_info_dict(info)
        raise RPCError(f"未知 job operation: {operation}", code="unknown_operation")

    def _merged_jobs(self) -> List[Dict[str, Any]]:
        records: Dict[str, DurableJobRecord] = {
            record.job_id: record for record in self.job_store.list()
        }
        for info in self.jobs.list_jobs():
            records[info.job_id] = DurableJobRecord(
                job_id=info.job_id,
                command=info.command,
                cwd=info.cwd,
                pid=info.pid,
                status=info.status.value,
                started_at=info.started_at,
                ended_at=info.ended_at,
                exit_code=info.exit_code,
                latest_sequence=info.latest_sequence,
                log_path=info.log_path,
                daemon_instance_id=self.instance_id,
            )
        ordered = sorted(records.values(), key=lambda item: item.started_at, reverse=True)
        return [_job_info_dict(record.info()) for record in ordered]

    def _build_sandbox(self) -> Optional[ExecutionSandbox]:
        if self.no_sandbox:
            return None
        section = self.config.get("sandbox") or {}
        if not isinstance(section, dict):
            section = {}
        backend = self.sandbox_backend or section.get("backend", "auto")
        network = (
            bool(self.sandbox_network)
            if self.sandbox_network is not None
            else bool(section.get("network", False))
        )
        return ExecutionSandbox(
            self.workspace_root, backend=str(backend), network=network)

    # -- subagents -------------------------------------------------------------

    async def _ensure_agent_runtime(self) -> Tuple[Any, Any]:
        async with self._agent_lock:
            if self._agents is not None:
                return self._agent_loop, self._agents
            if self.agent_runtime_factory is None:
                raise RuntimeError(
                    "daemon agent runtime 未配置；请由组合根注入 "
                    "agent_runtime_factory")
            runtime = await self.agent_runtime_factory(DaemonAgentRuntimeRequest(
                workspace_root=self.workspace_root,
                config_path=self.config_path,
                config=dict(self.config),
                no_sandbox=self.no_sandbox,
                sandbox_backend=self.sandbox_backend,
                sandbox_network=self.sandbox_network,
                auto_approve=self.auto_approve,
                permission_mode=self.permission_mode,
                log=lambda text: self.log(f"[daemon:agent] {text}"),
            ))
            loop = runtime.loop
            agents = runtime.agents
            self._agent_mcp_supervisor = runtime.mcp_supervisor
            redactor = loop.services.core.redactor or self.redactor
            agents.redactor = redactor
            agents.sidechains.redactor = redactor
            # Child tools use the daemon's durable job manager, so jobs started
            # by subagents remain controllable after the spawning client leaves.
            loop.services.core.tool_context.shell_jobs = self.jobs
            if getattr(agents, "tool_context", None) is not None:
                agents.tool_context.shell_jobs = self.jobs
            agents.bus.subscribe(self._on_agent_event)
            self._agent_loop = loop
            self._agents = agents
            return loop, agents

    async def _on_agent_event(self, event) -> None:
        try:
            if isinstance(event, SubagentSpawned):
                handle = self._agents.get(event.agent_id) if self._agents else None
                if handle is not None:
                    await asyncio.to_thread(
                        self.agent_store.upsert,
                        handle.id,
                        _agent_record(handle, self.instance_id),
                    )
                return
            agent_id = getattr(event, "agent_id", "")
            if not agent_id or self._agents is None:
                return
            handle = self._agents.get(agent_id)
            if handle is None:
                return
            values = _agent_record(handle, self.instance_id)
            if isinstance(event, SubagentStateChanged):
                values["reason"] = event.reason
            elif isinstance(event, SubagentFeedUpdated):
                values["latest_sequence"] = event.sequence
            elif isinstance(event, SubagentFinished):
                values["result"] = handle.result
                values["error"] = handle.error
            await asyncio.to_thread(self.agent_store.upsert, agent_id, values)
        except Exception as exc:
            self.agent_store.issues.append(f"agent event 持久化失败: {exc}")

    async def _dispatch_agent(self, operation: str, params: Dict[str, Any]) -> Any:
        if operation == "list":
            return self._merged_agents()
        if operation == "definitions":
            _, manager = await self._ensure_agent_runtime()
            return {"lines": manager.definition_lines()}
        if operation == "sidechains":
            _, manager = await self._ensure_agent_runtime()
            return {"lines": manager.sidechain_lines(limit=int(params.get("limit") or 50))}
        if operation == "sidechain":
            _, manager = await self._ensure_agent_runtime()
            path, records = manager.read_sidechain(_required(params, "agent_id"))
            return {"path": str(path), "records": list(records)}
        if operation == "spawn":
            _, manager = await self._ensure_agent_runtime()
            handle = await manager.spawn(
                _required(params, "task"),
                isolated=_optional_bool(params.get("isolated")),
                fork_context=_optional_bool(params.get("fork_context")),
                definition=(str(params.get("definition") or "").strip() or None),
            )
            await asyncio.to_thread(
                self.agent_store.upsert,
                handle.id,
                _agent_record(handle, self.instance_id),
            )
            return _handle_dict(handle)
        if operation in {"wait", "feed", "send_input", "pause", "resume", "kill"}:
            agent_id = _required(params, "agent_id")
            durable = self.agent_store.load(agent_id)
            if operation == "feed" and (
                    durable is not None
                    and durable.get("status") not in {"running", "paused", "queued"}):
                return self._durable_agent_feed(
                    agent_id,
                    after_sequence=int(params.get("after_sequence") or 0),
                    limit=int(params.get("limit") or 200),
                )
            if operation == "wait" and (
                    durable is not None
                    and durable.get("status") not in {"running", "paused", "queued"}):
                return durable
            _, manager = await self._ensure_agent_runtime()
            if operation == "wait":
                handle = await manager.wait(
                    agent_id, timeout=float(params.get("timeout") or 60.0))
                await asyncio.to_thread(
                    self.agent_store.upsert,
                    handle.id,
                    _agent_record(handle, self.instance_id),
                )
                return _handle_dict(handle)
            if operation == "feed":
                page = manager.feed(
                    agent_id,
                    after_sequence=int(params.get("after_sequence") or 0),
                    limit=int(params.get("limit") or 200),
                )
                return {
                    "agent_id": page.agent_id,
                    "after_sequence": page.after_sequence,
                    "latest_sequence": page.latest_sequence,
                    "next_sequence": page.next_sequence,
                    "dropped_through": page.dropped_through,
                    "entries": [
                        {
                            "agent_id": entry.agent_id,
                            "sequence": entry.sequence,
                            "kind": entry.kind,
                            "text": entry.text,
                            "ts": entry.ts,
                            "data": entry.data,
                        }
                        for entry in page.entries
                    ],
                }
            if operation == "send_input":
                handle, action = await manager.send_input(
                    agent_id,
                    _required(params, "message"),
                    interrupt=bool(params.get("interrupt")),
                )
            elif operation == "pause":
                handle = await manager.pause(agent_id)
                action = "paused"
            elif operation == "resume":
                handle = await manager.resume(agent_id)
                action = "resumed"
            else:
                handle = await manager.kill(agent_id)
                action = "killed"
            await asyncio.to_thread(
                self.agent_store.upsert,
                handle.id,
                _agent_record(handle, self.instance_id),
            )
            return {"action": action, "agent": _handle_dict(handle)}
        raise RPCError(f"未知 agent operation: {operation}", code="unknown_operation")

    def _durable_agent_feed(
        self, agent_id: str, *, after_sequence: int, limit: int,
    ) -> Dict[str, Any]:
        from ..subagent.sidechain import SidechainStore, read_sidechain

        store = SidechainStore(self.workspace_root, redactor=self.redactor)
        path = store.resolve(agent_id)
        if path is None:
            raise RPCError(f"未找到子代理 sidechain: {agent_id}", code="not_found")
        records = read_sidechain(path)
        entries = [
            {
                "agent_id": agent_id,
                "sequence": int(item.get("sequence") or 0),
                "kind": str(item.get("kind") or "event"),
                "text": str(item.get("text") or ""),
                "ts": float(item.get("ts") or 0.0),
                "data": dict(item.get("data") or {}),
            }
            for item in records
            if item.get("type") == "feed"
            and int(item.get("sequence") or 0) > max(0, int(after_sequence))
        ]
        entries = entries[:max(1, min(int(limit), 1_000))]
        latest = max(
            [int(item.get("sequence") or 0) for item in records
             if item.get("type") == "feed"] or [0])
        return {
            "agent_id": agent_id,
            "after_sequence": max(0, int(after_sequence)),
            "latest_sequence": latest,
            "next_sequence": entries[-1]["sequence"] if entries else max(0, int(after_sequence)),
            "dropped_through": 0,
            "entries": entries,
        }

    def _merged_agents(self) -> List[Dict[str, Any]]:
        records = {str(item.get("agent_id")): item for item in self.agent_store.list()}
        if self._agents is not None:
            for handle in self._agents._handles.values():
                records[handle.id] = _agent_record(handle, self.instance_id)
        ordered = sorted(
            records.values(),
            key=lambda item: float(item.get("updated_at") or item.get("started_at") or 0),
            reverse=True,
        )
        return ordered


def _required(params: Dict[str, Any], key: str) -> str:
    value = str(params.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} 不能为空")
    return value


def _optional_bool(value: Any) -> Optional[bool]:
    return None if value is None else bool(value)


def _job_info_dict(info) -> Dict[str, Any]:
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


def _agent_record(handle, instance_id: str) -> Dict[str, Any]:
    return {
        "agent_id": handle.id,
        "daemon_instance_id": instance_id,
        "task": handle.task,
        "definition": handle.definition,
        "status": handle.status,
        "turns": handle.turns,
        "result": handle.result,
        "error": handle.error,
        "worktree": str(handle.worktree) if handle.worktree else "",
        "branch": handle.branch or "",
        "forked": bool(handle.forked),
        "sidechain_path": str(handle.sidechain_path or ""),
        "model": handle.model,
        "provider": handle.provider,
        "permission_mode": handle.permission_mode,
        "mcp_servers": list(handle.mcp_servers),
        "latest_sequence": handle.feed.latest_sequence if handle.feed else 0,
        "queue": list(handle.queue),
        "started_at": getattr(handle, "started_at", None) or time.time(),
    }


def _handle_dict(handle) -> Dict[str, Any]:
    return {
        "agent_id": handle.id,
        "task": handle.task,
        "definition": handle.definition,
        "status": handle.status,
        "turns": handle.turns,
        "result": handle.result,
        "error": handle.error,
        "preview": handle.preview(),
        "worktree": str(handle.worktree) if handle.worktree else "",
        "branch": handle.branch or "",
        "forked": bool(handle.forked),
        "sidechain_path": str(handle.sidechain_path or ""),
        "model": handle.model,
        "provider": handle.provider,
        "permission_mode": handle.permission_mode,
        "mcp_servers": list(handle.mcp_servers),
        "queue": list(handle.queue),
        "latest_sequence": handle.feed.latest_sequence if handle.feed else 0,
    }


async def run_daemon(
    workspace_root: Path,
    *,
    config_path: Optional[Path] = None,
    idle_timeout: float = 1_800.0,
    sandbox_backend: Optional[str] = None,
    sandbox_network: Optional[bool] = None,
    no_sandbox: bool = False,
    auto_approve: bool = False,
    permission_mode: Optional[str] = None,
    agent_runtime_factory: Optional[DaemonAgentRuntimeFactory] = None,
) -> None:
    bundle = load_config_layers(
        workspace=workspace_root,
        explicit_path=config_path,
    )
    server = DaemonServer(
        workspace_root,
        config_path=config_path,
        config=bundle.data,
        idle_timeout=idle_timeout,
        sandbox_backend=sandbox_backend,
        sandbox_network=sandbox_network,
        no_sandbox=no_sandbox,
        auto_approve=auto_approve,
        permission_mode=permission_mode,
        agent_runtime_factory=agent_runtime_factory,
        log=lambda text: print(text, flush=True),
    )
    await server.start()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(server.stop()))
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    state = read_json(server.paths.state) or {}
    print(
        f"[daemon] started instance={server.instance_id} pid={os.getpid()} "
        f"workspace={server.workspace_root} endpoint={state.get('endpoint')}",
        flush=True,
    )
    await server.serve_forever()


__all__ = [
    "DaemonAgentRuntime",
    "DaemonAgentRuntimeFactory",
    "DaemonAgentRuntimeRequest",
    "DaemonServer",
    "run_daemon",
]
