"""Cross-platform process launch and tree termination contracts."""

from __future__ import annotations

import asyncio
import os
import signal
from types import SimpleNamespace

import pytest

import krelcode.core.process as process_module
from krelcode.core.process import (
    ProcessPlatform,
    detect_process_platform,
    quote_shell_value,
    stop_process_tree,
    terminate_process_tree,
)


class FakeProcess:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode = None
        self.killed = False
        self.terminated = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class EscalatingProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__()
        self.wait_calls = 0

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            await asyncio.sleep(10)
        self.returncode = -9
        return self.returncode


def windows_platform() -> ProcessPlatform:
    return ProcessPlatform(
        family="windows",
        system="windows",
        shell=r"C:\Windows\System32\cmd.exe",
        shell_args=("/d", "/s", "/c"),
        shell_dialect="cmd",
    )


def test_detect_posix_platforms_and_shell_argv():
    linux = detect_process_platform(
        os_name="posix", sys_platform="linux", environ={})
    macos = detect_process_platform(
        os_name="posix", sys_platform="darwin", environ={})

    assert (linux.family, linux.system, linux.shell_dialect) == (
        "posix", "linux", "posix")
    assert (macos.family, macos.system) == ("posix", "macos")
    assert linux.shell == "/bin/sh"
    assert linux.shell_argv("printf ok") == ["/bin/sh", "-c", "printf ok"]


def test_detect_windows_prefers_cmd_comspec_and_falls_back_when_untrusted():
    resolved = detect_process_platform(
        os_name="nt",
        environ={"COMSPEC": r"C:\Windows\System32\cmd.exe"},
        which=lambda _name: None,
    )
    assert resolved.family == "windows"
    assert resolved.shell_dialect == "cmd"
    assert resolved.shell_argv("dir") == [
        r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c", "dir"]

    fallback = detect_process_platform(
        os_name="nt",
        environ={"COMSPEC": r"C:\Users\me\evil.exe"},
        which=lambda _name: r"C:\Windows\System32\cmd.exe",
    )
    assert fallback.shell == r"C:\Windows\System32\cmd.exe"


def test_process_isolation_kwargs_are_platform_specific():
    posix = detect_process_platform(
        os_name="posix", sys_platform="linux", environ={})
    windows = windows_platform()

    assert posix.subprocess_kwargs() == {"start_new_session": True}
    assert windows.subprocess_kwargs() == {"creationflags": 0x00000200}


def test_quote_shell_value_uses_native_shell_syntax():
    posix = detect_process_platform(
        os_name="posix", sys_platform="linux", environ={})
    windows = windows_platform()

    assert quote_shell_value("a b", platform=posix) == "'a b'"
    assert quote_shell_value("a&b", platform=windows) == '"a&b"'
    assert quote_shell_value("50%", platform=windows) == "50%%"
    with pytest.raises(ValueError):
        quote_shell_value("line1\nline2", platform=windows)


def test_posix_termination_uses_process_group(monkeypatch):
    calls = []
    proc = FakeProcess()
    monkeypatch.setattr(os, "getpgid", lambda pid: 999)
    monkeypatch.setattr(
        os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))

    assert terminate_process_tree(proc, platform=detect_process_platform(
        os_name="posix", sys_platform="linux", environ={})) is True
    assert calls == [(999, signal.SIGKILL)]
    assert proc.killed is False


def test_windows_termination_uses_taskkill_tree_then_kills_leader(
        monkeypatch):
    proc = FakeProcess()
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(process_module.shutil, "which", lambda _name: "taskkill")
    monkeypatch.setattr(process_module.subprocess, "run", run)

    assert terminate_process_tree(
        proc, force=True, platform=windows_platform()) is True
    assert commands[0][0] == [
        "taskkill", "/PID", "4242", "/T", "/F"]
    assert proc.killed is False

    monkeypatch.setattr(
        process_module.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(returncode=1),
    )
    assert terminate_process_tree(
        proc, force=True, platform=windows_platform()) is True
    assert proc.killed is True


def test_graceful_stop_escalates_to_force_after_timeout(monkeypatch):
    proc = EscalatingProcess()
    attempts = []

    def taskkill(pid, *, force):
        attempts.append((pid, force))
        return True

    monkeypatch.setattr(process_module, "_terminate_windows_tree", taskkill)

    stopped = asyncio.run(stop_process_tree(
        proc,
        force=False,
        timeout=0.01,
        platform=windows_platform(),
    ))

    assert stopped is True
    assert attempts == [(4242, False), (4242, True)]
    assert proc.wait_calls == 2


def test_finished_process_is_not_terminated():
    proc = FakeProcess()
    proc.returncode = 0

    assert terminate_process_tree(proc) is True
    assert asyncio.run(stop_process_tree(proc)) is True
    assert proc.killed is False
