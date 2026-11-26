"""Small cross-process lock built on atomic file creation."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional


class FileLockTimeout(TimeoutError):
    """Raised when another process holds a file lock for too long."""


class InterProcessFileLock:
    """Exclusive lock based on ``O_CREAT|O_EXCL`` with stale-owner recovery."""

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.05,
        stale_after: float = 60.0,
    ) -> None:
        self.path = Path(path)
        self.timeout = max(0.0, float(timeout))
        self.poll_interval = max(0.01, float(poll_interval))
        self.stale_after = max(1.0, float(stale_after))
        self.token = uuid.uuid4().hex

    def __enter__(self) -> "InterProcessFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            if self._try_acquire():
                return self
            if self._remove_stale_lock():
                continue
            if time.monotonic() >= deadline:
                raise FileLockTimeout(f"等待文件锁超时: {self.path}")
            time.sleep(self.poll_interval)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()

    def _try_acquire(self) -> bool:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(self.path, flags, 0o600)
        except FileExistsError:
            return False
        try:
            payload = json.dumps({
                "pid": os.getpid(),
                "token": self.token,
                "created_at": time.time(),
            }).encode("utf-8")
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True

    def _remove_stale_lock(self) -> bool:
        payload = self._read_payload()
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        if age < self.stale_after:
            return False
        pid = payload.get("pid")
        if isinstance(pid, int) and _process_alive(pid):
            return False
        try:
            self.path.unlink()
            return True
        except OSError:
            return False

    def release(self) -> None:
        payload = self._read_payload()
        if payload.get("token") != self.token:
            return
        try:
            self.path.unlink()
        except OSError:
            pass

    def _read_payload(self) -> dict[str, object]:
        try:
            raw = self.path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


__all__ = ["FileLockTimeout", "InterProcessFileLock"]
