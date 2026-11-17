"""Lazy language-server registry and document synchronization."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .client import LSPClient, LSPError, path_to_uri


@dataclass(frozen=True)
class LSPServerConfig:
    name: str
    command: tuple[str, ...]
    extensions: tuple[str, ...]
    language_id: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.command:
            raise ValueError(f"LSP server {self.name!r} 缺少 command")
        if not self.extensions:
            raise ValueError(f"LSP server {self.name!r} 缺少 extensions")
        normalized = tuple(
            ext.lower() if ext.startswith(".") else f".{ext.lower()}"
            for ext in self.extensions
        )
        object.__setattr__(self, "extensions", normalized)

    @property
    def argv(self) -> tuple[str, ...]:
        return self.command + self.args

    @classmethod
    def from_mapping(cls, name: str, value: Mapping[str, Any]) -> "LSPServerConfig":
        if not isinstance(value, Mapping):
            raise ValueError(f"LSP server {name!r} 必须是 table")
        raw_command = value.get("command")
        if isinstance(raw_command, str):
            command = (raw_command,)
        elif isinstance(raw_command, Sequence):
            command = tuple(str(item) for item in raw_command if str(item))
        else:
            command = ()
        raw_args = value.get("args") or ()
        if isinstance(raw_args, str):
            args = (raw_args,)
        elif isinstance(raw_args, Sequence):
            args = tuple(str(item) for item in raw_args)
        else:
            args = ()
        raw_ext = value.get("extensions") or ()
        if isinstance(raw_ext, str):
            extensions = (raw_ext,)
        elif isinstance(raw_ext, Sequence):
            extensions = tuple(str(item) for item in raw_ext)
        else:
            extensions = ()
        language_id = str(value.get("language_id") or name).strip() or name
        raw_env = value.get("env") or {}
        env = ({
            str(key): str(item) for key, item in raw_env.items()
        } if isinstance(raw_env, Mapping) else {})
        return cls(
            name=name,
            command=command,
            args=args,
            extensions=extensions,
            language_id=language_id,
            env=env,
            enabled=bool(value.get("enabled", True)),
        )


@dataclass(frozen=True)
class LSPOptions:
    enabled: bool = False
    startup_timeout: float = 10.0
    request_timeout: float = 5.0
    max_message_bytes: int = 16 * 1024 * 1024
    max_file_bytes: int = 2 * 1024 * 1024
    servers: tuple[LSPServerConfig, ...] = ()

    @classmethod
    def from_config(cls, section: Any) -> "LSPOptions":
        if not isinstance(section, Mapping):
            section = {}
        enabled = bool(section.get("enabled", False))
        startup = _positive_float(section.get("startup_timeout_ms"), 10000.0) / 1000
        request = _positive_float(section.get("request_timeout_ms"), 5000.0) / 1000
        max_message = int(_positive_float(
            section.get("max_message_bytes"), 16 * 1024 * 1024))
        max_file = int(_positive_float(
            section.get("max_file_bytes"), 2 * 1024 * 1024))
        raw_servers = section.get("servers") or {}
        servers: list[LSPServerConfig] = []
        if isinstance(raw_servers, Mapping):
            for name in sorted(raw_servers):
                try:
                    servers.append(LSPServerConfig.from_mapping(
                        str(name), raw_servers[name]))
                except (TypeError, ValueError):
                    # Broken optional server definitions do not disable other
                    # language servers. Manager construction re-validates and
                    # reports malformed entries through ``issues``.
                    continue
        return cls(
            enabled=enabled,
            startup_timeout=startup,
            request_timeout=request,
            max_message_bytes=max_message,
            max_file_bytes=max_file,
            servers=tuple(servers),
        )


@dataclass
class _OpenDocument:
    version: int
    text_hash: int
    language_id: str


class LSPManager:
    """Start language servers on demand and isolate per-server failures."""

    def __init__(
        self,
        workspace_root: str | Path,
        options: Optional[LSPOptions] = None,
        *,
        client_factory=None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.options = options or LSPOptions()
        self.environ = environ or os.environ
        self.issues: list[str] = []
        self._client_factory = client_factory or self._make_client
        self._clients: dict[str, LSPClient] = {}
        self._start_locks: dict[str, asyncio.Lock] = {}
        self._documents: dict[str, dict[str, _OpenDocument]] = {}
        self._require_workspace_file = True
        self._extension_map: dict[str, LSPServerConfig] = {}
        for server in self.options.servers:
            if not server.enabled:
                continue
            for ext in server.extensions:
                existing = self._extension_map.get(ext)
                if existing is not None:
                    self.issues.append(
                        f"LSP 扩展 {ext} 同时映射到 {existing.name} 和 {server.name}；"
                        f"保留 {existing.name}")
                    continue
                self._extension_map[ext] = server

    @property
    def enabled(self) -> bool:
        return self.options.enabled

    @property
    def servers(self) -> tuple[LSPServerConfig, ...]:
        return self.options.servers

    def status_lines(self) -> tuple[str, ...]:
        if not self.enabled:
            return ("  LSP 未启用（[lsp].enabled = true 后生效）",)
        lines = ["  LSP 服务:"]
        for server in self.options.servers:
            state = "运行中" if server.name in self._clients else "按需启动"
            lines.append(
                f"  - {server.name:<12} {state:<5} {shlex_display(server.argv)}"
                f"  ({', '.join(server.extensions)})")
        if not self.options.servers:
            lines.append("  （未配置 [lsp.servers.*]）")
        lines.extend(f"  ! {issue}" for issue in self.issues)
        return tuple(lines)

    def server_for_path(self, path: str | Path) -> Optional[LSPServerConfig]:
        suffix = Path(path).suffix.lower()
        return self._extension_map.get(suffix)

    def resolve_path(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        resolved = candidate.expanduser().resolve()
        if self._require_workspace_file:
            try:
                resolved.relative_to(self.workspace_root)
            except ValueError as exc:
                raise ValueError(f"LSP 只允许访问工作区内文件: {resolved}") from exc
        return resolved

    async def client_for(
        self, path: str | Path, *, start: bool = True,
    ) -> Optional[LSPClient]:
        if not self.enabled:
            return None
        resolved = self.resolve_path(path)
        server = self.server_for_path(resolved)
        if server is None:
            return None
        client = self._clients.get(server.name)
        if client is not None:
            return client
        if not start:
            return None
        lock = self._start_locks.setdefault(server.name, asyncio.Lock())
        async with lock:
            client = self._clients.get(server.name)
            if client is not None:
                return client
            try:
                created = self._client_factory(server)
                if hasattr(created, "__await__"):
                    created = await created
                client = await created.start()
            except Exception as exc:
                self.issues.append(
                    f"{server.name} 启动失败: {type(exc).__name__}: {exc}")
                return None
            self._clients[server.name] = client
            self._documents[server.name] = {}
            return client

    def _make_client(self, server: LSPServerConfig) -> LSPClient:
        env = dict(self.environ)
        env.update(server.env)
        return LSPClient(
            server.argv,
            cwd=self.workspace_root,
            env=env,
            server_name=server.name,
            startup_timeout=self.options.startup_timeout,
            request_timeout=self.options.request_timeout,
            max_message_bytes=self.options.max_message_bytes,
        )

    async def sync_document(
        self, path: str | Path, *, language_id: Optional[str] = None,
    ) -> tuple[LSPClient, str, LSPServerConfig]:
        resolved = self.resolve_path(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"文件不存在: {resolved}")
        if resolved.stat().st_size > self.options.max_file_bytes:
            raise ValueError(
                f"文件超过 LSP 同步上限 {self.options.max_file_bytes} bytes: {resolved}")
        server = self.server_for_path(resolved)
        if server is None:
            raise ValueError(f"没有为 {resolved.suffix} 配置 LSP server")
        client = await self.client_for(resolved)
        if client is None:
            raise LSPError(f"LSP server {server.name} 不可用")
        text = await asyncio.to_thread(
            resolved.read_text, encoding="utf-8", errors="replace")
        text_hash = hash(text)
        uri = path_to_uri(resolved)
        docs = self._documents.setdefault(server.name, {})
        current = docs.get(uri)
        if current is None:
            await client.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": uri,
                    "languageId": language_id or server.language_id,
                    "version": 1,
                    "text": text,
                }
            })
            docs[uri] = _OpenDocument(1, text_hash, language_id or server.language_id)
        elif current.text_hash != text_hash:
            version = current.version + 1
            await client.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            })
            docs[uri] = _OpenDocument(version, text_hash, current.language_id)
        return client, uri, server

    async def request_for_file(
        self,
        path: str | Path,
        method: str,
        params: Mapping[str, Any],
    ) -> tuple[Any, LSPClient, str, LSPServerConfig]:
        client, uri, server = await self.sync_document(path)
        payload = {"textDocument": {"uri": uri}, **dict(params)}
        result = await client.request(method, payload)
        return result, client, uri, server

    async def diagnostics_for(
        self, path: str | Path, *, pull: bool = True,
    ) -> tuple[Any, LSPClient, str, LSPServerConfig]:
        client, uri, server = await self.sync_document(path)
        if pull:
            try:
                result = await client.request(
                    "textDocument/diagnostic", {"textDocument": {"uri": uri}})
                return result, client, uri, server
            except LSPError:
                pass
        # Push diagnostics are populated by
        # ``textDocument/publishDiagnostics`` notifications.
        await asyncio.sleep(0)
        return client.diagnostics.get(uri, {"diagnostics": []}), client, uri, server

    async def close(self) -> None:
        clients = tuple(self._clients.values())
        self._clients.clear()
        self._documents.clear()
        await asyncio.gather(
            *(client.close() for client in clients), return_exceptions=True)


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def shlex_display(argv: Sequence[str]) -> str:
    """Small deterministic display helper (display only, never execution)."""
    parts = []
    for item in argv:
        text = str(item)
        if not text or any(ch.isspace() for ch in text):
            text = repr(text)
        parts.append(text)
    return " ".join(parts)


__all__ = ["LSPManager", "LSPOptions", "LSPServerConfig"]
