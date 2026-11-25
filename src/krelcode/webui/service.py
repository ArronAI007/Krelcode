"""CLI lifecycle-managed localhost Web UI."""

from __future__ import annotations

import argparse
import ipaddress
import secrets
import sys
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from .server import DashboardHTTPServer
from .store import DashboardStore


@dataclass(frozen=True)
class WebUIOptions:
    """Resolve whether the current CLI process hosts the Web UI.

    ``auto`` follows the interactive CLI: if stdin is not a TTY (headless,
    tests, pipes, IDE stream-json), no listener is opened.  ``--webui`` and
    ``[webui].enabled = true`` can force it; ``--no-webui`` disables it.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = False
    port_scan: int = 10

    @classmethod
    def from_args_and_config(
        cls,
        args: argparse.Namespace,
        config: Mapping[str, object],
        *,
        interactive: bool,
    ) -> "WebUIOptions":
        section = config.get("webui")
        raw = section if isinstance(section, Mapping) else {}
        explicit = getattr(args, "webui", None)
        configured = raw.get("enabled", "auto")
        if explicit is not None:
            enabled = bool(explicit)
        elif isinstance(configured, str):
            mode = configured.strip().lower()
            if mode not in {"auto", "on", "off", "true", "false"}:
                mode = "auto"
            enabled = interactive if mode == "auto" else mode in {"on", "true"}
        else:
            # Explicit true is a force-enable, even for headless runs.
            enabled = bool(configured)
        return cls(
            enabled=enabled,
            host=str(raw.get("host") or "127.0.0.1"),
            port=_bounded_int(raw.get("port"), 8765, 0, 65_535),
            open_browser=bool(raw.get("open_browser", False)),
            port_scan=_bounded_int(raw.get("port_scan"), 10, 1, 100),
        )


class WebUIService:
    """Host the Web frontend inside one CLI process.

    The HTTP server runs on a daemon thread and reads the shared observability
    journal.  A bind failure is reported and ignored; the terminal Agent must
    remain usable even when the Web UI cannot start.
    """

    def __init__(
        self,
        workspace_root: Path,
        options: WebUIOptions,
        *,
        note: Optional[Callable[[str], None]] = None,
        token: Optional[str] = None,
        store: Optional[DashboardStore] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.options = options
        self.note = note or (lambda message: print(message, file=sys.stderr))
        self.token = token or secrets.token_urlsafe(32)
        self.store = store or DashboardStore(self.workspace_root)
        self._server: Optional[DashboardHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._url = ""

    @property
    def url(self) -> str:
        return self._url

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return True
        try:
            host = _loopback_host(self.options.host)
        except ValueError as exc:
            self.note(f"[webui] {exc}")
            return False
        ports = self._candidate_ports()
        last_error = ""
        for port in ports:
            try:
                server = DashboardHTTPServer(
                    (host, port), self.store, self.token)
            except OSError as exc:
                last_error = str(exc)
                continue
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.5},
                name="krelcode-webui",
                daemon=True,
            )
            thread.start()
            self._server = server
            self._thread = thread
            actual_host = str(server.server_address[0])
            actual_port = int(server.server_address[1])
            display_host = f"[{actual_host}]" if ":" in actual_host else actual_host
            self._url = f"http://{display_host}:{actual_port}/?token={self.token}"
            self.note(f"[webui] 已同步启动: http://{display_host}:{actual_port}/")
            self.note("[webui] 使用命令行输出中的一次性 token URL 打开；不会开放公网")
            if self.options.open_browser:
                webbrowser.open(self._url)
            else:
                self.note(f"[webui] 一次性打开链接: {self._url}")
            return True
        suffix = f": {last_error}" if last_error else ""
        self.note(f"[webui] 无法启动本地服务，CLI 继续运行{suffix}")
        return False

    def close(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _candidate_ports(self) -> tuple[int, ...]:
        if self.options.port == 0:
            return (0,)
        last = min(65_535, self.options.port + self.options.port_scan - 1)
        return tuple(range(self.options.port, last + 1))


def _loopback_host(host: str) -> str:
    normalized = str(host or "").strip().lower()
    if normalized == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError("Web UI 只允许绑定 loopback 地址") from exc
    if not address.is_loopback:
        raise ValueError("Web UI 只允许绑定 loopback 地址")
    return str(address)


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(parsed, maximum))


__all__ = ["WebUIOptions", "WebUIService"]
