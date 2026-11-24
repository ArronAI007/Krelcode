"""Cross-platform process launch, isolation and tree termination.

All foreground commands, background jobs, Skill scripts, hooks and long-lived
stdio clients use this module instead of carrying their own POSIX/Windows
branches. The public contract is intentionally small:

* ``ProcessPlatform.shell_argv()`` builds the platform-native shell command;
* ``ProcessPlatform.subprocess_kwargs()`` creates an isolated process group;
* ``terminate_process_tree()`` terminates the whole tree on POSIX and Windows;
* ``stop_process_tree()`` performs termination plus a bounded async wait.
"""

from __future__ import annotations

import asyncio
import ntpath
import os
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Mapping, Optional

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_WINDOWS_TASKKILL_TIMEOUT = 5.0
_TASKKILL_SUCCESS_CODES = frozenset({0, 128})


@dataclass(frozen=True)
class ProcessPlatform:
    """Resolved process and shell semantics for the current host."""

    family: str
    system: str
    shell: str
    shell_args: tuple[str, ...]
    shell_dialect: str

    @property
    def windows(self) -> bool:
        return self.family == "windows"

    def shell_argv(self, command: str) -> list[str]:
        return [self.shell, *self.shell_args, command]

    def subprocess_kwargs(self) -> dict[str, object]:
        """Return isolation flags accepted by asyncio subprocess APIs."""
        if self.windows:
            return {"creationflags": _CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}


def detect_process_platform(
    *,
    os_name: Optional[str] = None,
    sys_platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    which=shutil.which,
) -> ProcessPlatform:
    """Resolve process semantics with injectable inputs for cross-platform tests."""
    os_name = os.name if os_name is None else os_name
    sys_platform = sys.platform if sys_platform is None else sys_platform
    env = os.environ if environ is None else environ
    if os_name == "nt":
        configured = str(env.get("COMSPEC") or "").strip()
        if configured and ntpath.basename(configured).lower() in {
                "cmd", "cmd.exe"}:
            shell = configured
        else:
            shell = str(which("cmd.exe") or "cmd.exe")
        return ProcessPlatform(
            family="windows",
            system="windows",
            shell=shell,
            shell_args=("/d", "/s", "/c"),
            shell_dialect="cmd",
        )
    system = (
        "macos" if sys_platform == "darwin"
        else "linux" if sys_platform.startswith("linux")
        else "posix"
    )
    shell = str(env.get("KRELCODE_POSIX_SHELL") or "/bin/sh")
    return ProcessPlatform(
        family="posix",
        system=system,
        shell=shell,
        shell_args=("-c",),
        shell_dialect="posix",
    )


def current_process_platform() -> ProcessPlatform:
    return detect_process_platform()


def shell_argv(command: str) -> list[str]:
    return current_process_platform().shell_argv(command)


def process_isolation_kwargs() -> dict[str, object]:
    return current_process_platform().subprocess_kwargs()


def quote_shell_value(
    value: str, *, platform: Optional[ProcessPlatform] = None,
) -> str:
    """Quote one template value for the active shell without changing argv."""
    resolved = platform or current_process_platform()
    text = str(value)
    if "\x00" in text or "\r" in text or "\n" in text:
        raise ValueError("shell 模板值不能包含 NUL 或换行")
    if not resolved.windows:
        return shlex.quote(text)
    # cmd.exe has no safe argv-like quoting primitive. Keep metacharacters
    # inside one quoted token and neutralize batch percent expansion.
    escaped = text.replace('"', '""').replace("%", "%%")
    if not escaped or any(char in escaped for char in ' \t&|<>^()!'):
        return f'"{escaped}"'
    return escaped


def terminate_process_tree(
    proc: asyncio.subprocess.Process,
    *,
    force: bool = True,
    sig: Optional[int] = None,
    platform: Optional[ProcessPlatform] = None,
) -> bool:
    """Terminate the full process tree without POSIX-only calls on Windows.

    ``force`` controls graceful versus immediate termination. ``sig`` remains
    supported for POSIX callers/tests, but production code should prefer the
    platform-neutral ``force`` flag.
    """
    resolved = platform or current_process_platform()
    if getattr(proc, "returncode", None) is not None:
        return True
    if resolved.family == "posix":
        target_signal = signal.SIGKILL if force else signal.SIGTERM
        if sig is not None:
            target_signal = sig
        try:
            # start_new_session=True normally makes pgid == pid. If the leader
            # already exited while descendants hold pipes, fall back to pid.
            try:
                pgid = os.getpgid(proc.pid)
            except ProcessLookupError:
                pgid = proc.pid
            os.killpg(pgid, target_signal)
            return True
        except (ProcessLookupError, PermissionError, OSError, AttributeError):
            pass
    elif resolved.family == "windows":
        if _terminate_windows_tree(proc.pid, force=force):
            return True

    try:
        if force:
            proc.kill()
        else:
            proc.terminate()
        return True
    except (AttributeError, ProcessLookupError, OSError):
        return False


async def stop_process_tree(
    proc: asyncio.subprocess.Process,
    *,
    force: bool = True,
    timeout: float = 5.0,
    platform: Optional[ProcessPlatform] = None,
) -> bool:
    """Terminate a process tree and wait briefly for its leader to exit."""
    if proc.returncode is not None:
        return True
    terminate_process_tree(proc, force=force, platform=platform)
    try:
        await asyncio.wait_for(proc.wait(), timeout=max(0.05, float(timeout)))
        return True
    except asyncio.TimeoutError:
        if force:
            return False
        # Escalate a graceful shutdown only after the bounded grace period.
        # This keeps callers from accidentally leaking a descendant tree when
        # the leader ignores SIGTERM/TM_CLOSE.
        terminate_process_tree(proc, force=True, platform=platform)
        try:
            await asyncio.wait_for(
                proc.wait(), timeout=min(1.0, max(0.05, float(timeout))))
            return True
        except asyncio.TimeoutError:
            return False


def _terminate_windows_tree(pid: int, *, force: bool) -> bool:
    executable = shutil.which("taskkill") or "taskkill"
    command = [executable, "/PID", str(int(pid)), "/T"]
    if force:
        command.append("/F")
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": _WINDOWS_TASKKILL_TIMEOUT,
        "check": False,
    }
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags
    try:
        completed = subprocess.run(command, **kwargs)
        return completed.returncode in _TASKKILL_SUCCESS_CODES
    except (OSError, subprocess.SubprocessError):
        return False


__all__ = [
    "ProcessPlatform",
    "current_process_platform",
    "detect_process_platform",
    "process_isolation_kwargs",
    "quote_shell_value",
    "shell_argv",
    "stop_process_tree",
    "terminate_process_tree",
]
