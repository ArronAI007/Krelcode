"""Single-workspace asyncio daemon for durable jobs and subagents."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import signal
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import load_config_layers
from ..core.events import (
    AgentEvent,
    EventBus,
    ShellJobFinished,
    ShellJobOutput,
    ShellJobStarted,
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
from .server_agents import DaemonAgentService
from .server_runtime import (
    DaemonAgentRuntime,
    DaemonAgentRuntimeFactory,
    DaemonAgentRuntimeRequest,
)
from .storage import AgentRecordStore, DurableJobRecord, JobRecordStore
from .transport import (
    DaemonEndpoint,
    choose_endpoint,
    prepare_unix_socket,
)

DAEMON_IDLE_CHECK_SECONDS = 15.0


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
        log: Callable[[str], None] = print,
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
        self._serve_task: Optional[asyncio.Task[None]] = None
        self._idle_task: Optional[asyncio.Task[None]] = None
        self._file_task: Optional[asyncio.Task[None]] = None
        self._file_workers: set[asyncio.Task[None]] = set()
        self._last_activity = time.monotonic()
        self._closed = False
        self._started = False
        self.agent_service = DaemonAgentService(
            workspace_root=self.workspace_root,
            config_path=self.config_path,
            config=self.config,
            no_sandbox=self.no_sandbox,
            sandbox_backend=self.sandbox_backend,
            sandbox_network=self.sandbox_network,
            auto_approve=self.auto_approve,
            permission_mode=self.permission_mode,
            runtime_factory=self.agent_runtime_factory,
            agent_store=self.agent_store,
            job_manager=self.jobs,
            redactor=self.redactor,
            instance_id=self.instance_id,
            log=self.log,
        )
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
        await self.agent_service.shutdown()
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
        return self.agent_service.has_active_work()

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
            return await self.agent_service.dispatch(op[6:], params)
        if op.startswith("task."):
            return await self.agent_service.dispatch(op, params)
        raise RPCError(f"未知 daemon operation: {op}", code="unknown_operation")

    async def _delayed_stop(self) -> None:
        await asyncio.sleep(0.1)
        await self.stop()

    # Compatibility wrappers for callers that used the historical private
    # methods before agent runtime management became a composed service.
    async def _ensure_agent_runtime(
        self,
    ) -> Tuple[DaemonAgentRuntime, Any]:
        return await self.agent_service._ensure_runtime()

    async def _dispatch_agent(self, operation: str, params: Dict[str, Any]) -> Any:
        return await self.agent_service.dispatch(operation, params)

    async def _on_agent_event(self, event: AgentEvent) -> None:
        await self.agent_service.on_event(event)

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
            "agent_runtime_loaded": self.agent_service.runtime_loaded,
            "issues": [*self.job_store.issues[-10:], *self.agent_store.issues[-10:]],
        }

    # -- jobs ------------------------------------------------------------------

    async def _on_job_event(self, event: AgentEvent) -> None:
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
            permission_profile = str(
                params.get("sandbox_permission") or "").strip() or None
            info = await self.jobs.start(
                command,
                cwd=cwd,
                sandbox=sandbox,
                permission_profile=permission_profile,
            )
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
                durable_page = self.job_store.output(
                    job_id,
                    after_sequence=int(params.get("after_sequence") or 0),
                    max_chars=int(params.get("max_chars") or 30_000),
                )
                if durable_page is None:
                    raise
                return {
                    "job": _job_info_dict(durable_page.job),
                    "text": durable_page.text,
                    "next_sequence": durable_page.next_sequence,
                    "truncated": durable_page.truncated,
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
        raw_install_paths = (
            section.get("user_install_paths")
            or section.get("install_paths")
            or ()
        )
        if isinstance(raw_install_paths, str):
            raw_install_paths = [raw_install_paths]
        return ExecutionSandbox(
            self.workspace_root,
            backend=str(backend),
            network=network,
            allow_user_install=bool(section.get("user_install", True)),
            install_network=bool(section.get("install_network", True)),
            user_install_roots=tuple(
                Path(path).expanduser() for path in raw_install_paths),
        )


def _required(params: Dict[str, Any], key: str) -> str:
    value = str(params.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} 不能为空")
    return value



def _job_info_dict(info: Any) -> Dict[str, Any]:
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
