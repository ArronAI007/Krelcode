"""Minimal async Language Server Protocol client.

The client owns only the LSP wire protocol and process lifecycle.  It does not
know which language server is running, how files are selected, or how results
are rendered.  Messages use LSP's ``Content-Length`` framing over stdio:

    Content-Length: <bytes>\r\n
    Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n
    \r\n
    <UTF-8 JSON body>

All operations are asynchronous.  A malformed message or a server crash fails
pending requests (without terminating the main agent process), and stderr is
kept as a bounded diagnostic tail.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from types import TracebackType
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import unquote, urlparse

from ..core.process import process_isolation_kwargs, stop_process_tree

MAX_HEADER_BYTES = 64 * 1024
DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
_SENSITIVE_ENV = re.compile(
    r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH)", re.IGNORECASE
)


class LSPError(RuntimeError):
    """Base class for LSP client failures."""


class LSPProcessError(LSPError):
    """The language server could not be started or exited unexpectedly."""


class LSPTimeoutError(LSPError):
    """An LSP request exceeded its deadline."""


class LSPProtocolError(LSPError):
    """The language server emitted malformed protocol data or an error reply."""


@dataclass(frozen=True)
class LSPResponseError(LSPProtocolError):
    """A JSON-RPC error response returned by the language server."""

    code: int
    message: str
    data: Any = None

    def __str__(self) -> str:
        return f"LSP error {self.code}: {self.message}"


def sanitized_environment(environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Return a child environment without common credential variables.

    Language servers normally need ``PATH``/``HOME`` and language-specific
    runtime roots, not the coding agent's model API keys.  This is defense in
    depth: configured server executables are trusted by the user, but they do
    not need secret material from Krelcode's process.
    """

    source = os.environ if environ is None else environ
    safe: dict[str, str] = {}
    for key, value in source.items():
        if _SENSITIVE_ENV.search(str(key)):
            continue
        safe[str(key)] = str(value)
    return safe


def path_to_uri(path: str | Path) -> str:
    return Path(path).expanduser().resolve().as_uri()


def uri_to_path(uri: str) -> Path:
    parsed = urlparse(str(uri))
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        raise ValueError(f"只支持本地 file URI: {uri}")
    value = unquote(parsed.path or "")
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", value):
        value = value[1:]
    return Path(value)


class LSPClient:
    """A single language-server process using JSON-RPC over stdio."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | Path,
        env: Optional[Mapping[str, str]] = None,
        server_name: str = "lsp",
        startup_timeout: float = 10.0,
        request_timeout: float = 5.0,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        notification_handler: Optional[
            Callable[[str, Any], Optional[Awaitable[None]]]
        ] = None,
    ) -> None:
        if not command:
            raise ValueError("LSP command 不能为空")
        if startup_timeout <= 0 or request_timeout <= 0:
            raise ValueError("LSP 超时必须大于 0")
        if max_message_bytes < 1024:
            raise ValueError("max_message_bytes 不能小于 1024")
        self.command = tuple(str(item) for item in command)
        self.cwd = Path(cwd).expanduser().resolve()
        self.env = sanitized_environment(env)
        self.server_name = server_name
        self.startup_timeout = float(startup_timeout)
        self.request_timeout = float(request_timeout)
        self.max_message_bytes = int(max_message_bytes)
        self.notification_handler = notification_handler
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stdout_task: Optional[asyncio.Task[Any]] = None
        self._stderr_task: Optional[asyncio.Task[Any]] = None
        self._write_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: MutableMapping[int, asyncio.Future[Any]] = {}
        self._closed = False
        self._started = False
        self._stderr_lines: Deque[str] = deque(maxlen=100)
        self._stderr_bytes = 0
        self.diagnostics: dict[str, dict[str, Any]] = {}
        self.server_capabilities: dict[str, Any] = {}
        self.workspace_folders: list[dict[str, Any]] = [
            {"uri": path_to_uri(self.cwd), "name": self.cwd.name}
        ]

    @property
    def process(self) -> Optional[asyncio.subprocess.Process]:
        return self._process

    @property
    def started(self) -> bool:
        return self._started and not self._closed

    def stderr_tail(self) -> str:
        return "".join(self._stderr_lines).strip()

    async def start(self) -> "LSPClient":
        if self._started:
            return self
        if self._closed:
            raise LSPProcessError(f"{self.server_name}: 客户端已关闭")
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(self.cwd),
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **process_isolation_kwargs(),
            )
        except (OSError, ValueError) as exc:
            raise LSPProcessError(
                f"{self.server_name}: 无法启动 {self.command[0]!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self._stdout_task = asyncio.create_task(
            self._read_loop(), name=f"lsp-{self.server_name}-stdout")
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name=f"lsp-{self.server_name}-stderr")
        try:
            response = await self._request_impl(
                "initialize",
                {
                    "processId": os.getpid(),
                    "clientInfo": {"name": "krelcode", "version": "1"},
                    "rootUri": path_to_uri(self.cwd),
                    "rootPath": str(self.cwd),
                    "workspaceFolders": self.workspace_folders,
                    "capabilities": {
                        "workspace": {
                            "workspaceFolders": True,
                            "configuration": True,
                        },
                        "textDocument": {
                            "synchronization": {"dynamicRegistration": False},
                            "publishDiagnostics": {
                                "relatedInformation": True,
                                "versionSupport": True,
                            },
                            "diagnostic": {
                                "dynamicRegistration": False,
                                "relatedDocumentSupport": True,
                            },
                            "hover": {"contentFormat": ["markdown", "plaintext"]},
                            "definition": {"linkSupport": True},
                            "references": {},
                        },
                    },
                    "positionEncodings": ["utf-16"],
                },
                timeout=self.startup_timeout,
            )
        except Exception:
            await self.close()
            raise
        if isinstance(response, dict):
            capabilities = response.get("capabilities")
            if isinstance(capabilities, dict):
                self.server_capabilities = dict(capabilities)
            server_info = response.get("serverInfo")
            if isinstance(server_info, dict) and server_info.get("name"):
                self.server_name = str(server_info["name"])
        await self.notify("initialized", {})
        self._started = True
        return self

    async def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        if not self._started or self._closed:
            raise LSPProcessError(f"{self.server_name}: 尚未启动")
        return await self._request_impl(
            method, params, timeout=timeout or self.request_timeout)

    async def _request_impl(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float,
    ) -> Any:
        process = self._process
        if process is None or process.stdin is None:
            raise LSPProcessError(f"{self.server_name}: stdio 不可用")
        async with self._request_lock:
            request_id = self._next_id
            self._next_id += 1
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            try:
                await self._send({"jsonrpc": "2.0", "id": request_id,
                                  "method": method, "params": params})
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                if not future.done():
                    future.cancel()
                self._pending.pop(request_id, None)
                await self._send({
                    "jsonrpc": "2.0", "method": "$/cancelRequest",
                    "params": {"id": request_id},
                }, ignore_errors=True)
                raise LSPTimeoutError(
                    f"{self.server_name}: {method} 超时 ({timeout:g}s)")
            except asyncio.CancelledError:
                if not future.done():
                    future.cancel()
                self._pending.pop(request_id, None)
                raise
            finally:
                self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Any = None) -> None:
        if self._closed:
            return
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(
        self,
        payload: Mapping[str, Any],
        *,
        ignore_errors: bool = False,
    ) -> None:
        process = self._process
        if process is None or process.stdin is None:
            if ignore_errors:
                return
            raise LSPProcessError(f"{self.server_name}: stdin 不可用")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(body) > self.max_message_bytes:
            raise LSPProtocolError(
                f"{self.server_name}: 消息过大 ({len(body)} bytes)")
        frame = (
            f"Content-Length: {len(body)}\r\n"
            "Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n"
        ).encode("ascii") + body
        try:
            async with self._write_lock:
                process.stdin.write(frame)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            if ignore_errors:
                return
            raise LSPProcessError(
                f"{self.server_name}: 写入失败: {type(exc).__name__}: {exc}") from exc

    async def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        failure: Optional[BaseException] = None
        try:
            while True:
                message = await self._read_message(process.stdout)
                if message is None:
                    break
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # propagate protocol failure to every waiter
            failure = exc
        finally:
            if not self._closed and failure is None:
                failure = LSPProcessError(
                    f"{self.server_name}: 服务端已关闭 stdout")
            if failure is not None:
                self._fail_pending(failure)

    async def _read_message(
        self, stream: asyncio.StreamReader,
    ) -> Optional[dict[str, Any]]:
        headers: dict[str, str] = {}
        consumed = 0
        while True:
            line = await stream.readline()
            if line == b"":
                return None
            consumed += len(line)
            if consumed > MAX_HEADER_BYTES:
                raise LSPProtocolError(f"{self.server_name}: LSP header 过大")
            if line in (b"\r\n", b"\n"):
                break
            try:
                decoded = line.decode("ascii").strip()
                name, value = decoded.split(":", 1)
            except (UnicodeDecodeError, ValueError) as exc:
                raise LSPProtocolError(
                    f"{self.server_name}: 非法 LSP header") from exc
            headers[name.strip().lower()] = value.strip()
        try:
            length = int(headers.get("content-length", ""))
        except ValueError as exc:
            raise LSPProtocolError(
                f"{self.server_name}: 缺少或非法 Content-Length") from exc
        if length < 0 or length > self.max_message_bytes:
            raise LSPProtocolError(
                f"{self.server_name}: Content-Length 越界: {length}")
        raw = await stream.readexactly(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LSPProtocolError(
                f"{self.server_name}: LSP JSON 解析失败: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise LSPProtocolError(f"{self.server_name}: LSP body 必须是对象")
        return payload

    async def _dispatch(self, payload: dict[str, Any]) -> None:
        if "method" in payload and "id" in payload:
            await self._handle_server_request(payload)
            return
        if "method" in payload:
            method = str(payload.get("method") or "")
            params = payload.get("params")
            if method == "textDocument/publishDiagnostics" and isinstance(params, dict):
                uri = str(params.get("uri") or "")
                if uri:
                    self.diagnostics[uri] = dict(params)
            handler = self.notification_handler
            if handler is not None:
                pending = handler(method, params)
                if pending is not None:
                    await pending
            return
        if "id" not in payload:
            return
        request_id = payload.get("id")
        if not isinstance(request_id, int):
            return
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        if "error" in payload:
            error = payload.get("error")
            if isinstance(error, dict):
                detail = LSPResponseError(
                    int(error.get("code", -32000)),
                    str(error.get("message", "unknown error")),
                    error.get("data"),
                )
            else:
                detail = LSPResponseError(-32000, str(error))
            future.set_exception(detail)
        else:
            future.set_result(payload.get("result"))

    async def _handle_server_request(self, payload: dict[str, Any]) -> None:
        request_id = payload.get("id")
        method = str(payload.get("method") or "")
        params = payload.get("params")
        result: Any = None
        error: Optional[dict[str, Any]] = None
        if method == "workspace/configuration":
            items = params.get("items", []) if isinstance(params, dict) else []
            result = [None for _ in items] if isinstance(items, list) else []
        elif method == "workspace/workspaceFolders":
            result = self.workspace_folders
        elif method in {
            "client/registerCapability",
            "client/unregisterCapability",
            "window/workDoneProgress/create",
            "window/showMessageRequest",
        }:
            result = None
        else:
            error = {"code": -32601, "message": f"Method not found: {method}"}
        response: dict[str, Any] = {
            "jsonrpc": "2.0", "id": request_id,
        }
        if error is not None:
            response["error"] = error
        else:
            response["result"] = result
        await self._send(response, ignore_errors=True)

    def _fail_pending(self, error: BaseException) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace")
                if len(line) > 4000:
                    line = line[-4000:]
                self._stderr_lines.append(line)
                self._stderr_bytes += len(line.encode("utf-8", errors="replace"))
                while self._stderr_bytes > 32 * 1024 and self._stderr_lines:
                    removed = self._stderr_lines.popleft()
                    self._stderr_bytes -= len(removed.encode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None and process.returncode is None:
            if self._started:
                try:
                    await self._send({
                        "jsonrpc": "2.0", "id": self._next_id,
                        "method": "shutdown", "params": None,
                    }, ignore_errors=True)
                    self._next_id += 1
                except Exception:
                    pass
                try:
                    await self._send({
                        "jsonrpc": "2.0", "method": "exit", "params": None,
                    }, ignore_errors=True)
                except Exception:
                    pass
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                stopped = await stop_process_tree(
                    process, force=False, timeout=1.0)
                if not stopped:
                    await stop_process_tree(process, force=True, timeout=2.0)
        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._stdout_task, self._stderr_task)
              if task is not None),
            return_exceptions=True,
        )
        self._fail_pending(LSPProcessError(f"{self.server_name}: 客户端已关闭"))

    async def __aenter__(self) -> "LSPClient":
        return await self.start()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


__all__ = [
    "LSPClient",
    "LSPError",
    "LSPProcessError",
    "LSPProtocolError",
    "LSPResponseError",
    "LSPTimeoutError",
    "path_to_uri",
    "sanitized_environment",
    "uri_to_path",
]
