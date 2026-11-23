"""Workspace-scoped daemon paths, durable state and single-instance locking.

The daemon never stores bearer tokens in ``daemon.json``.  The state file is
safe to print for diagnostics; the token lives in a separate 0600 file and is
only read by clients running as the same OS user.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .protocol import ProtocolError, RPCRequest, RPCResponse, encode_message

DAEMON_DIRNAME = Path(".krelcode") / "daemon"
DAEMON_SCHEMA_VERSION = 1


class DaemonStateError(RuntimeError):
    """Daemon state is corrupt, unsafe or belongs to another workspace."""


@dataclass(frozen=True)
class DaemonPaths:
    workspace_root: Path

    @property
    def root(self) -> Path:
        return self.workspace_root / DAEMON_DIRNAME

    @property
    def state(self) -> Path:
        return self.root / "daemon.json"

    @property
    def token(self) -> Path:
        return self.root / "auth.token"

    @property
    def lock(self) -> Path:
        return self.root / "daemon.lock"

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def agents_dir(self) -> Path:
        return self.root / "agents"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def requests_dir(self) -> Path:
        return self.root / "requests"

    @property
    def responses_dir(self) -> Path:
        return self.root / "responses"

    @property
    def daemon_log(self) -> Path:
        return self.logs_dir / "daemon.log"

    @property
    def socket(self) -> Path:
        return self.root / "daemon.sock"

    def ensure(self) -> "DaemonPaths":
        for path in (
            self.root,
            self.jobs_dir,
            self.agents_dir,
            self.logs_dir,
            self.requests_dir,
            self.responses_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass
        return self


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise DaemonStateError(f"无法读取 daemon 状态 {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise DaemonStateError(f"daemon 状态必须是 JSON object: {path}")
    return raw


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write JSON without exposing a partially-written state file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def secure_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_token(paths: DaemonPaths) -> str:
    try:
        token = paths.token.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise DaemonStateError(f"无法读取 daemon token: {exc}") from exc
    if not token:
        raise DaemonStateError("daemon token 为空")
    return token


def generate_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def pid_is_alive(pid: int) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    # Windows os.kill(pid, 0) is not a harmless existence probe.  A running
    # state is instead confirmed over the authenticated RPC endpoint.
    return True


def probe_daemon(paths: DaemonPaths, *, timeout: float = 0.5) -> bool:
    """Probe the exact instance advertised by the state file.

    A live PID is not ownership proof: the OS may have reused it after the
    original daemon died.  The authenticated ping both checks the endpoint and
    verifies that the endpoint still belongs to the recorded instance.
    """
    try:
        state = read_json(paths.state)
        endpoint = (state or {}).get("endpoint")
        if not isinstance(endpoint, dict):
            return False
        instance_id = str((state or {}).get("instance_id") or "")
        if not instance_id:
            return False
        request = RPCRequest.create("ping", {}, token=read_token(paths))
        payload = encode_message(request.to_dict())
        kind = str(endpoint.get("kind") or "")
        if kind == "unix":
            path = str(endpoint.get("path") or "")
            if not path:
                return False
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(max(0.1, float(timeout)))
            try:
                sock.connect(path)
                sock.sendall(payload)
                raw = _recv_response_line(sock, timeout=timeout)
            finally:
                sock.close()
        elif kind == "tcp":
            host = str(endpoint.get("host") or "127.0.0.1")
            port = int(endpoint.get("port") or 0)
            if not (0 < port < 65536):
                return False
            sock = socket.create_connection(
                (host, port), timeout=max(0.1, float(timeout)))
            sock.settimeout(max(0.1, float(timeout)))
            try:
                sock.sendall(payload)
                raw = _recv_response_line(sock, timeout=timeout)
            finally:
                sock.close()
        elif kind == "file":
            # The file transport has no socket-level liveness signal.  An
            # authenticated ping through the same atomic directory protocol
            # remains safe and deterministic for the startup lock probe.
            request_path = paths.requests_dir / f"{request.id}.json"
            response_path = paths.responses_dir / f"{request.id}.json"
            atomic_write_json(request_path, request.to_dict())
            deadline = time.monotonic() + max(0.1, float(timeout))
            raw = None
            while time.monotonic() < deadline:
                try:
                    raw = response_path.read_text(encoding="utf-8")
                    response_path.unlink(missing_ok=True)
                    break
                except FileNotFoundError:
                    time.sleep(0.03)
                except OSError:
                    return False
            if raw is None:
                request_path.unlink(missing_ok=True)
                return False
        else:
            return False
        response = RPCResponse.from_dict(json.loads(raw))
        result = response.result if isinstance(response.result, dict) else {}
        return bool(
            response.ok
            and result.get("ok")
            and str(result.get("instance_id") or "") == instance_id
        )
    except (
        OSError, ValueError, TypeError, json.JSONDecodeError,
        DaemonStateError, ProtocolError,
    ):
        return False


def _recv_response_line(sock: socket.socket, *, timeout: float) -> str:
    sock.settimeout(max(0.1, float(timeout)))
    data = bytearray()
    while b"\n" not in data:
        chunk = sock.recv(65_536)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("daemon ping response too large")
    line = bytes(data).split(b"\n", 1)[0]
    if not line:
        raise ValueError("empty daemon ping response")
    return line.decode("utf-8")


def new_instance_id() -> str:
    return f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"


class InstanceLock:
    """Atomic workspace lock with conservative stale-lock recovery."""

    def __init__(self, paths: DaemonPaths) -> None:
        self.paths = paths
        self._fd: Optional[int] = None
        self.instance: Dict[str, Any] = {}

    def acquire(self, instance: Mapping[str, Any]) -> None:
        self.paths.ensure()
        payload = {**dict(instance), "pid": os.getpid(), "created_at": time.time()}
        for attempt in range(2):
            try:
                fd = os.open(
                    self.paths.lock,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                if attempt or not self._stale():
                    raise DaemonStateError(
                        f"workspace daemon 已在运行或锁不可安全回收: {self.paths.lock}")
                try:
                    self.paths.lock.unlink()
                except OSError as exc:
                    raise DaemonStateError(f"无法回收 stale daemon lock: {exc}") from exc
                continue
            try:
                encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
                os.write(fd, encoded.encode("utf-8"))
                os.fsync(fd)
            except Exception:
                os.close(fd)
                try:
                    self.paths.lock.unlink()
                except OSError:
                    pass
                raise
            self._fd = fd
            self.instance = payload
            return
        raise DaemonStateError("无法获取 workspace daemon lock")

    def _stale(self) -> bool:
        try:
            payload = read_json(self.paths.lock)
        except DaemonStateError:
            return False
        if not payload:
            return False
        pid = int(payload.get("pid") or 0)
        if pid_is_alive(pid):
            try:
                state = read_json(self.paths.state)
            except DaemonStateError:
                state = None
            lock_instance = str(payload.get("instance_id") or "")
            state_instance = str((state or {}).get("instance_id") or "")
            if state_instance and lock_instance and state_instance == lock_instance:
                # A matching state/lock pair can outlive the daemon when the
                # recorded PID is reused.  Trust it only when the recorded
                # instance answers an authenticated ping.  During the short
                # publication window, however, the daemon may not have bound
                # its endpoint yet; preserve that startup grace window.
                if time.time() - float(payload.get("created_at") or 0.0) <= 10.0:
                    return False
                return not probe_daemon(self.paths, timeout=0.5)
            # A daemon briefly owns the lock before publishing daemon.json.
            # Keep a short startup grace window, then treat a live unrelated
            # PID as stale instead of trusting PID identity forever.
            created_at = float(payload.get("created_at") or 0.0)
            return time.time() - created_at > 10.0
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            self.paths.lock.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


__all__ = [
    "DAEMON_SCHEMA_VERSION",
    "DaemonPaths",
    "DaemonStateError",
    "InstanceLock",
    "atomic_write_json",
    "generate_token",
    "new_instance_id",
    "pid_is_alive",
    "probe_daemon",
    "read_json",
    "read_token",
    "secure_write_text",
]
