"""Daemon RPC transport, lifecycle and status client."""

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
from typing import Any, Dict, Optional

from ..core.redact import SecretRedactor
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



__all__ = ["DaemonClient", "DaemonStatus", "DaemonUnavailable"]
