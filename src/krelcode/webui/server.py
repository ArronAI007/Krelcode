"""Independent localhost HTTP/SSE frontend for Krelcode.

This process is a peer of the CLI/TUI.  It consumes the shared observability
journal and experiment profile format; it never imports AgentLoop, mutates a
running turn, or affects permission/sandbox policy.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import secrets
import sys
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, cast
from urllib.parse import parse_qs, urlsplit

from krelcode.observability.experiments import (
    ExperimentError,
    KNOBS,
    empty_profile,
    load_profile,
    profile_from_mapping,
    profile_warnings,
    save_profile,
)
from krelcode.observability.models import SCHEMA_VERSION

from .store import DashboardStore, safe_run_id

COOKIE_NAME = "krelcode_observe"
MAX_BODY_BYTES = 1024 * 1024
STATIC_ROOT = Path(__file__).with_name("static")


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        store: DashboardStore,
        token: str,
    ) -> None:
        self.store = store
        self.token = token
        super().__init__(server_address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "KrelcodeWeb/1"
    sys_version = ""

    @property
    def dashboard(self) -> DashboardHTTPServer:
        return cast(DashboardHTTPServer, self.server)

    def do_GET(self) -> None:
        self._dispatch_get()

    def do_PUT(self) -> None:
        self._dispatch_put()

    def do_OPTIONS(self) -> None:
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    def log_message(self, format: str, *args: object) -> None:
        # Keep normal operation quiet; startup and explicit errors are enough.
        return

    def _dispatch_get(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        if path == "/" and self._query_token_valid(query):
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={self.dashboard.token}; "
                "Path=/; HttpOnly; SameSite=Strict",
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if not self._authorized(query):
            self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        if path == "/":
            self._serve_static("index.html")
            return
        if path in {"/app.js", "/style.css"}:
            self._serve_static(path.lstrip("/"))
            return
        try:
            if path == "/api/v1/status":
                self._send_json({
                    "ok": True,
                    "schema_version": SCHEMA_VERSION,
                    "workspace": str(self.dashboard.store.workspace_root),
                    "server_time": time.time(),
                })
                return
            if path == "/api/v1/runs":
                limit = _query_int(query, "limit", 200, 1, 1_000)
                self._send_json({
                    "runs": self.dashboard.store.list_runs(limit=limit)})
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) == 4 and parts[:3] == ["api", "v1", "runs"]:
                self._send_json(self.dashboard.store.get_run(parts[3]))
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "runs"]
                and parts[4] == "events"
            ):
                after = _query_int(query, "after", 0, 0, 2_000_000_000)
                limit = _query_int(query, "limit", 10_000, 1, 50_000)
                self._send_json({
                    "events": self.dashboard.store.read_events(
                        parts[3], after=after, limit=limit)})
                return
            if (
                len(parts) == 5
                and parts[:3] == ["api", "v1", "runs"]
                and parts[4] == "stream"
            ):
                after = _query_int(query, "after", 0, 0, 2_000_000_000)
                self._stream_events(parts[3], after)
                return
            if path == "/api/v1/experiments/knobs":
                self._send_json({
                    "knobs": [knob.to_dict() for knob in KNOBS],
                    "applies_next_run": True,
                })
                return
            if path == "/api/v1/experiments/active":
                profile_path = self.dashboard.store.profile_path
                profile = (
                    load_profile(profile_path)
                    if profile_path.is_file() else empty_profile()
                )
                self._send_json({
                    "profile": profile.to_dict(),
                    "warnings": list(profile_warnings(profile)),
                    "path": str(profile_path),
                    "applies_next_run": True,
                })
                return
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
        except KeyError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except (ValueError, ExperimentError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            self._send_json(
                {"error": f"internal_error: {type(exc).__name__}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _dispatch_put(self) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if not self._authorized(query):
            self._send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        if not self._same_origin():
            self._send_json({"error": "origin_rejected"}, HTTPStatus.FORBIDDEN)
            return
        if parsed.path != "/api/v1/experiments/active":
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(
                {"error": "invalid_body_size"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            raw = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(
                {"error": f"invalid_json: {exc}"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(raw, Mapping):
            self._send_json(
                {"error": "profile must be an object"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            profile = profile_from_mapping(
                {str(key): value for key, value in raw.items()})
            save_profile(self.dashboard.store.profile_path, profile)
        except ExperimentError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except OSError as exc:
            self._send_json(
                {"error": f"profile_write_failed: {exc}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._send_json({
            "profile": profile.to_dict(),
            "warnings": list(profile_warnings(profile)),
            "applies_next_run": True,
        })

    def _stream_events(self, run_id: str, after: int) -> None:
        run_id = safe_run_id(run_id)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        cursor = after
        last_event_id = self.headers.get("Last-Event-ID", "").strip()
        if last_event_id:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError:
                pass
        heartbeat = time.monotonic()
        try:
            self.wfile.write(
                f"event: ready\ndata: "
                f"{json.dumps({'run_id': run_id, 'after': cursor})}\n\n"
                .encode("utf-8"))
            self.wfile.flush()
            while True:
                events = self.dashboard.store.events_after(
                    run_id, cursor, limit=1_000)
                for envelope in events:
                    cursor = envelope.sequence
                    data = json.dumps(
                        envelope.to_dict(), ensure_ascii=False,
                        separators=(",", ":"))
                    self.wfile.write(
                        f"id: {cursor}\nevent: agent-event\ndata: {data}\n\n"
                        .encode("utf-8"))
                if events:
                    self.wfile.flush()
                    heartbeat = time.monotonic()
                elif time.monotonic() - heartbeat >= 15.0:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    heartbeat = time.monotonic()
                time.sleep(0.35)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _authorized(self, query: Mapping[str, Sequence[str]]) -> bool:
        token = self.dashboard.token
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            supplied = authorization.removeprefix("Bearer ").strip()
            if hmac.compare_digest(supplied, token):
                return True
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            cookie = SimpleCookie()
        morsel = cookie.get(COOKIE_NAME)
        if morsel is not None and hmac.compare_digest(morsel.value, token):
            return True
        values = query.get("token") or ()
        return bool(values and hmac.compare_digest(str(values[0]), token))

    def _query_token_valid(self, query: Mapping[str, Sequence[str]]) -> bool:
        values = query.get("token") or ()
        return bool(
            values
            and hmac.compare_digest(str(values[0]), self.dashboard.token)
        )

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return bool(host and origin in {f"http://{host}", f"https://{host}"})

    def _serve_static(self, name: str) -> None:
        if name not in {"index.html", "app.js", "style.css"}:
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        path = STATIC_ROOT / name
        try:
            content = path.read_bytes()
        except OSError:
            self._send_json({"error": "asset_missing"}, HTTPStatus.NOT_FOUND)
            return
        content_type = {
            "index.html": "text/html; charset=utf-8",
            "app.js": "text/javascript; charset=utf-8",
            "style.css": "text/css; charset=utf-8",
        }[name]
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(content)

    def _send_json(
        self,
        value: Mapping[str, object],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def _query_int(
    query: Mapping[str, Sequence[str]],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    values = query.get(key) or ()
    try:
        parsed = int(values[0]) if values else default
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


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


def run_server(
    workspace: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    token: Optional[str] = None,
) -> int:
    bound_host = _loopback_host(host)
    store = DashboardStore(workspace)
    server_token = token or secrets.token_urlsafe(32)
    server = DashboardHTTPServer((bound_host, int(port)), store, server_token)
    actual_host = str(server.server_address[0])
    actual_port = int(server.server_address[1])
    url = f"http://{actual_host}:{actual_port}/?token={server_token}"
    print(f"Krelcode Web UI: http://{actual_host}:{actual_port}/")
    print("仅监听本机 loopback；首次打开需要一次性 token。按 Ctrl-C 停止。")
    if open_browser:
        webbrowser.open(url)
    else:
        print(f"一次性打开链接: {url}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nKrelcode Web UI 已停止。")
    finally:
        server.server_close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="krelcode-web",
        description="Krelcode 独立 Web 可观测与消融实验 UI",
    )
    parser.add_argument(
        "--workspace", default=".", help="要观测的工作区（默认当前目录）")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="监听地址，只允许 loopback（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口")
    parser.add_argument(
        "--no-open-browser", action="store_true",
        help="只打印带 token 的本地 URL，不自动打开浏览器")
    args = parser.parse_args(argv)
    try:
        return run_server(
            Path(args.workspace).expanduser().resolve(),
            host=args.host,
            port=args.port,
            open_browser=not args.no_open_browser,
        )
    except (OSError, ValueError) as exc:
        print(f"krelcode-web 错误: {exc}", file=sys.stderr)
        return 1


__all__ = ["DashboardHTTPServer", "main", "run_server"]


if __name__ == "__main__":
    raise SystemExit(main())
