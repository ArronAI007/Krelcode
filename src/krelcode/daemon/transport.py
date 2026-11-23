"""Local transport selection for the workspace daemon."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

from .paths import DaemonPaths, DaemonStateError

UNIX_SOCKET_PATH_LIMIT = 96


@dataclass(frozen=True)
class DaemonEndpoint:
    kind: str
    path: str = ""
    host: str = ""
    port: int = 0

    def to_dict(self) -> Dict[str, Any]:
        if self.kind == "unix":
            return {"kind": "unix", "path": self.path}
        if self.kind == "tcp":
            return {"kind": "tcp", "host": self.host, "port": self.port}
        return {"kind": "file"}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "DaemonEndpoint":
        kind = str(raw.get("kind") or "")
        if kind == "unix":
            path = str(raw.get("path") or "")
            if not path:
                raise DaemonStateError("daemon endpoint 缺少 socket path")
            return cls("unix", path=path)
        if kind == "tcp":
            host = str(raw.get("host") or "127.0.0.1")
            port = int(raw.get("port") or 0)
            if host not in {"127.0.0.1", "::1", "localhost"} or not (0 < port < 65536):
                raise DaemonStateError("daemon TCP endpoint 必须绑定 loopback")
            return cls("tcp", host=host, port=port)
        if kind == "file":
            return cls("file")
        raise DaemonStateError(f"未知 daemon transport: {kind}")


def choose_endpoint(paths: DaemonPaths, *, platform: str | None = None) -> DaemonEndpoint:
    family = platform or os.name
    if family != "nt":
        configured = paths.socket
        if len(str(configured)) <= UNIX_SOCKET_PATH_LIMIT:
            return DaemonEndpoint("unix", path=str(configured))
        # macOS/Linux AF_UNIX path limits are small.  Keep the socket under the
        # per-user temp root, keyed by the canonical workspace path.
        digest = hashlib.sha256(str(paths.workspace_root).encode()).hexdigest()[:20]
        fallback = Path(tempfile.gettempdir()) / f"krelcode-{digest}.sock"
        return DaemonEndpoint("unix", path=str(fallback))
    return DaemonEndpoint("tcp", host="127.0.0.1", port=0)


def prepare_unix_socket(endpoint: DaemonEndpoint) -> None:
    if endpoint.kind != "unix":
        return
    path = Path(endpoint.path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise DaemonStateError(f"无法清理旧 Unix socket: {exc}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass


async def open_connection(endpoint: DaemonEndpoint) -> Tuple[Any, Any]:
    import asyncio

    if endpoint.kind == "unix":
        return await asyncio.open_unix_connection(endpoint.path, limit=4 * 1024 * 1024)
    if endpoint.kind == "tcp":
        return await asyncio.open_connection(
            endpoint.host, endpoint.port, limit=4 * 1024 * 1024)
    raise DaemonStateError("file transport 不使用 stream connection")


__all__ = [
    "DaemonEndpoint",
    "UNIX_SOCKET_PATH_LIMIT",
    "choose_endpoint",
    "open_connection",
    "prepare_unix_socket",
]
