"""Sandbox shell, environment and command-audit parity across platforms."""

from __future__ import annotations

from pathlib import Path

from krelcode.safety.commands import classify_command
from krelcode.safety.sandbox import ExecutionSandbox
from krelcode.tools.process import ProcessPlatform


def windows_platform() -> ProcessPlatform:
    return ProcessPlatform(
        family="windows",
        system="windows",
        shell=r"C:\Windows\System32\cmd.exe",
        shell_args=("/d", "/s", "/c"),
        shell_dialect="cmd",
    )


def posix_platform(system: str = "macos") -> ProcessPlatform:
    return ProcessPlatform(
        family="posix",
        system=system,
        shell="/bin/sh",
        shell_args=("-c",),
        shell_dialect="posix",
    )


def test_windows_shell_preparation_propagates_isolation(tmp_path):
    sandbox = ExecutionSandbox(
        tmp_path,
        backend="static",
        process_platform=windows_platform(),
    )

    prepared = sandbox.prepare_shell("dir /b", tmp_path)

    assert prepared.argv == [
        r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c", "dir /b"]
    assert prepared.process_kwargs == {"creationflags": 0x00000200}


def test_windows_sandbox_redirects_profile_and_shell_environment(tmp_path):
    sandbox = ExecutionSandbox(
        tmp_path,
        backend="static",
        process_platform=windows_platform(),
    )

    env = sandbox.prepare_shell("set", tmp_path).env
    runtime = (tmp_path / ".krelcode" / "sandbox").resolve()

    assert Path(env["USERPROFILE"]).resolve() == runtime / "home"
    assert Path(env["APPDATA"]).resolve() == runtime / "home" / "AppData" / "Roaming"
    assert Path(env["LOCALAPPDATA"]).resolve() == runtime / "home" / "AppData" / "Local"
    assert env["COMSPEC"] == r"C:\Windows\System32\cmd.exe"


def test_windows_static_audit_blocks_dynamic_nested_and_multiline_commands(
        tmp_path):
    sandbox = ExecutionSandbox(
        tmp_path,
        backend="static",
        process_platform=windows_platform(),
    )
    blocked = [
        r"%USERPROFILE%\secret.txt",
        r"%PATH%\..\secret.txt",
        "!SECRET!",
        "powershell -EncodedCommand AAAA",
        "pwsh -ec AAAA",
        r'cmd /c "type ..\secret.txt"',
        r'powershell -Command "type ..\secret.txt"',
        'powershell -com "type safe.txt; curl https://example.com"',
        "type safe.txt\ndir C:\\",
    ]

    for command in blocked:
        assert sandbox.audit_command(command, tmp_path), command


def test_windows_static_audit_allows_workspace_reads_and_safe_switches(
        tmp_path):
    (tmp_path / "README.md").write_text("demo", encoding="utf-8")
    (tmp_path / "safe.cmd").write_text("@echo off\n", encoding="utf-8")
    sandbox = ExecutionSandbox(
        tmp_path,
        backend="static",
        process_platform=windows_platform(),
    )

    assert sandbox.audit_command("dir /b", tmp_path) is None
    assert sandbox.audit_command("type README.md", tmp_path) is None
    assert sandbox.audit_command(
        r"cmd /d /s /c safe.cmd one two", tmp_path) is None
    assert sandbox.audit_exec(
        [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c",
         str(tmp_path / "safe.cmd"), "one"],
        tmp_path,
    ) is None


def test_windows_command_risk_classification_handles_cmd_syntax():
    risk, _ = classify_command("dir /s", shell_dialect="cmd")
    assert risk == "allow"

    risk, _ = classify_command(
        r"del C:\Windows\System32\drivers\etc\hosts", shell_dialect="cmd")
    assert risk == "deny"

    risk, _ = classify_command("format.com C:", shell_dialect="cmd")
    assert risk == "deny"

    risk, _ = classify_command(
        "echo safe\ncurl https://example.com", shell_dialect="cmd")
    assert risk == "ask"


def test_sandbox_capabilities_are_honest_per_backend(tmp_path):
    static = ExecutionSandbox(
        tmp_path,
        backend="static",
        process_platform=posix_platform("macos"),
    )
    assert static.capabilities.filesystem_isolation is False
    assert static.capabilities.network_isolation is False
    assert static.capabilities.static_path_audit is True
    assert static.capabilities.process_tree_isolation is True

    static.backend = "seatbelt"
    capabilities = static.capabilities
    assert capabilities.backend == "seatbelt"
    assert capabilities.filesystem_isolation is True
    assert capabilities.network_isolation is True

    static.network = True
    assert static.capabilities.filesystem_isolation is True
    assert static.capabilities.network_isolation is False


def test_rebind_preserves_process_platform(tmp_path):
    parent = ExecutionSandbox(
        tmp_path / "parent",
        backend="static",
        process_platform=windows_platform(),
    )
    child_root = tmp_path / "child"
    child_root.mkdir()

    child = parent.rebind(child_root)

    assert child.process_platform is parent.process_platform
    assert child.prepare_shell("dir", child_root).argv[:3] == [
        r"C:\Windows\System32\cmd.exe", "/d", "/s"]
