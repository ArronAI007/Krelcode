"""工具执行沙箱：静态命令审计 + 可选 OS 级进程隔离。

静态命令、路径和用户安装 scope 的审计实现在 ``sandbox_audit``；本模块负责
准备受限环境、OS 后端 argv/profile 与命令执行能力协商。
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, cast

from ..contracts.governance import SandboxResourceLimits
from .environment import scrub_environment
from .sandbox_install import inferred_user_install_roots
from .sandbox_runtime import SandboxRuntimePolicy
from .sandbox_audit import (
    SYSTEM_READ_ROOTS,
    _inside,
    _is_install_network_command,
    _validated_user_install_roots,
    audit_argv_paths,
    audit_command,
    detect_user_install_command,
)
from ..core.process import (
    ProcessIsolationKwargs,
    ProcessPlatform,
    current_process_platform,
)


class SandboxError(ValueError):
    """Sandbox 配置或准备失败。"""


class SandboxPermissionProfile(str, Enum):
    """Per-command filesystem capability scope.

    ``WORKSPACE`` is the default. ``USER_INSTALL`` adds only selected
    user-owned installation directories; it is not equivalent to disabling the
    Sandbox and never permits system-wide writes or privilege escalation.
    """

    WORKSPACE = "workspace"
    USER_INSTALL = "user-install"


class SandboxIsolationLevel(str, Enum):
    STATIC = "static"
    PROCESS = "process"
    OS = "os"
    CONTAINER = "container"
    MICROVM = "microvm"
    VM = "vm"


@dataclass(frozen=True)
class SandboxedCommand:
    argv: List[str]
    env: dict[str, str]
    backend: str
    process_kwargs: ProcessIsolationKwargs = field(
        default_factory=lambda: cast(ProcessIsolationKwargs, {}))
    permission_profile: SandboxPermissionProfile = (
        SandboxPermissionProfile.WORKSPACE)


@dataclass(frozen=True)
class SandboxCapabilities:
    """Content-free capability report shared by doctor, UI and tests."""

    backend: str
    platform: str
    shell_dialect: str
    filesystem_isolation: bool
    network_isolation: bool
    process_tree_isolation: bool = True
    static_path_audit: bool = True
    user_install: bool = False
    isolation_level: SandboxIsolationLevel = SandboxIsolationLevel.STATIC
    cpu_limit: bool = False
    memory_limit: bool = False
    storage_limit: bool = False
    process_limit: bool = False


class ExecutionSandbox:
    """为 shell/exec 子进程准备受限 argv、环境与网络策略。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        backend: str = "auto",
        network: bool = False,
        process_platform: Optional[ProcessPlatform] = None,
        user_home: Optional[Path] = None,
        allow_user_install: bool = True,
        install_network: bool = True,
        user_install_roots: Iterable[Path] = (),
        resource_limits: Optional[SandboxResourceLimits] = None,
        allowed_domains: Iterable[str] = (),
        container_image: str = "python:3.12-slim",
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.network = bool(network)
        self.user_home = Path(user_home or Path.home()).expanduser().resolve()
        self.allow_user_install = bool(allow_user_install)
        self.install_network = bool(install_network)
        self.user_install_roots = _validated_user_install_roots(
            self.user_home, user_install_roots)
        self.resource_limits = resource_limits or SandboxResourceLimits()
        self.allowed_domains = tuple(
            dict.fromkeys(
                str(item).strip().lower()
                for item in allowed_domains
                if str(item).strip()
            )
        )
        self.container_image = str(container_image or "python:3.12-slim")
        self.runtime_policy = SandboxRuntimePolicy(
            self.resource_limits,
            allowed_domains=self.allowed_domains,
            container_image=self.container_image,
        )
        self.process_platform = process_platform or current_process_platform()
        requested = (backend or "auto").strip().lower()
        if requested not in {
            "auto", "static", "seatbelt", "bubblewrap", "container",
        }:
            raise SandboxError(f"未知 sandbox backend: {backend!r}")
        self.requested_backend = requested
        self.backend = _detect_backend(requested)

    def rebind(self, workspace_root: Path) -> "ExecutionSandbox":
        """子代理 worktree 使用新根目录，保留后端与安装策略。"""
        return ExecutionSandbox(
            workspace_root,
            backend=self.requested_backend,
            network=self.network,
            process_platform=self.process_platform,
            user_home=self.user_home,
            allow_user_install=self.allow_user_install,
            install_network=self.install_network,
            user_install_roots=self.user_install_roots,
            resource_limits=self.resource_limits,
            allowed_domains=self.allowed_domains,
            container_image=self.container_image,
        )

    @property
    def capabilities(self) -> SandboxCapabilities:
        isolated = self.backend in {"seatbelt", "bubblewrap", "container"}
        isolation_level = (
            SandboxIsolationLevel.CONTAINER
            if self.backend == "container"
            else SandboxIsolationLevel.OS
            if isolated
            else SandboxIsolationLevel.STATIC
        )
        return SandboxCapabilities(
            backend=self.backend,
            platform=self.process_platform.system,
            shell_dialect=self.process_platform.shell_dialect,
            filesystem_isolation=isolated,
            network_isolation=isolated and not self.network,
            process_tree_isolation=True,
            static_path_audit=True,
            user_install=self.allow_user_install,
            isolation_level=isolation_level,
            cpu_limit=self.resource_limits.cpu_seconds is not None,
            memory_limit=self.resource_limits.memory_mb is not None,
            storage_limit=self.resource_limits.storage_mb is not None,
            process_limit=self.resource_limits.process_limit is not None,
        )

    def resolve_permission_profile(
        self,
        command: str,
        requested: SandboxPermissionProfile | str | None = None,
    ) -> SandboxPermissionProfile:
        """Resolve auto-detected or explicitly requested command scope."""
        if requested is not None:
            try:
                profile = SandboxPermissionProfile(requested)
            except ValueError as exc:
                raise SandboxError(
                    f"未知 Sandbox permission profile: {requested!r}") from exc
        else:
            profile = (
                SandboxPermissionProfile.USER_INSTALL
                if detect_user_install_command(
                    command,
                    self.user_home,
                    self.user_install_roots,
                    shell_dialect=self.process_platform.shell_dialect,
                )
                else SandboxPermissionProfile.WORKSPACE
            )
        if (
            profile is SandboxPermissionProfile.USER_INSTALL
            and not self.allow_user_install
        ):
            raise SandboxError(
                "Sandbox 已禁用用户级安装权限；请由用户启用 "
                "[sandbox].user_install=true 后重试")
        return profile

    def audit_command(
        self,
        command: str,
        cwd: Path,
        *,
        permission_profile: SandboxPermissionProfile | str | None = None,
    ) -> Optional[str]:
        """返回硬性违规原因；None 表示可交给隔离后端执行。"""
        try:
            profile = self.resolve_permission_profile(
                command, permission_profile)
        except SandboxError as exc:
            return str(exc)
        extra_roots = (
            self._install_roots_for_command(command)
            if profile is SandboxPermissionProfile.USER_INSTALL
            else ()
        )
        egress_violation = self.runtime_policy.egress_violation(command)
        if egress_violation and self._network_allowed(
            command,
            profile,
            explicitly_requested=permission_profile is not None,
        ):
            return egress_violation
        return audit_command(
            command,
            Path(cwd),
            self.workspace_root,
            network_allowed=self._network_allowed(
                command,
                profile,
                explicitly_requested=permission_profile is not None,
            ),
            shell_dialect=self.process_platform.shell_dialect,
            user_home=self.user_home if extra_roots else None,
            extra_write_roots=extra_roots,
        )

    def audit_exec(
        self,
        argv: Sequence[str],
        cwd: Path,
        *,
        allowed_read_roots: Iterable[Path] = (),
    ) -> Optional[str]:
        roots = [
            self.workspace_root,
            *[Path(path).resolve() for path in allowed_read_roots],
        ]
        if not any(_inside(Path(cwd), root) for root in roots):
            return f"cwd 越出 Sandbox 允许范围: {Path(cwd).resolve()}"
        return audit_argv_paths(
            argv,
            Path(cwd),
            roots,
            skip_program=True,
            network_allowed=self.network,
            windows_paths=self.process_platform.windows,
        )

    def prepare_shell(
        self,
        command: str,
        cwd: Path,
        *,
        permission_profile: SandboxPermissionProfile | str | None = None,
    ) -> SandboxedCommand:
        profile = self.resolve_permission_profile(command, permission_profile)
        egress_violation = self.runtime_policy.egress_violation(command)
        network_allowed = self._network_allowed(
            command,
            profile,
            explicitly_requested=permission_profile is not None,
        )
        if egress_violation and network_allowed:
            raise SandboxError(egress_violation)
        install_roots = (
            self._install_roots_for_command(command)
            if profile is SandboxPermissionProfile.USER_INSTALL
            else ()
        )
        return self._prepare(
            self.process_platform.shell_argv(command),
            Path(cwd),
            extra_read_roots=(),
            permission_profile=profile,
            install_roots=install_roots,
            network_allowed=network_allowed,
        )

    def prepare_exec(
        self,
        argv: Sequence[str],
        cwd: Path,
        *,
        extra_read_roots: Iterable[Path] = (),
    ) -> SandboxedCommand:
        return self._prepare(
            list(argv),
            Path(cwd),
            extra_read_roots=[
                Path(path).resolve() for path in extra_read_roots],
            permission_profile=SandboxPermissionProfile.WORKSPACE,
            install_roots=(),
            network_allowed=self.network,
        )

    def _prepare(
        self,
        argv: List[str],
        cwd: Path,
        *,
        extra_read_roots: Sequence[Path],
        permission_profile: SandboxPermissionProfile,
        install_roots: Sequence[Path],
        network_allowed: bool,
    ) -> SandboxedCommand:
        cwd = cwd.resolve()
        allowed_roots = [self.workspace_root, *extra_read_roots]
        if not any(_inside(cwd, root) for root in allowed_roots):
            raise SandboxError(f"cwd 越出 Sandbox 允许范围: {cwd}")
        if install_roots:
            self._ensure_user_install_roots(install_roots)
        try:
            self.runtime_policy.ensure_storage(self.workspace_root)
        except ValueError as exc:
            raise SandboxError(str(exc)) from exc
        env = self._sandbox_env(cwd, permission_profile, install_roots)
        process_kwargs = self.runtime_policy.process_kwargs(self.process_platform)
        if self.backend == "seatbelt":
            profile = self._seatbelt_profile(
                extra_read_roots,
                permission_profile=permission_profile,
                install_roots=install_roots,
                network_allowed=network_allowed,
            )
            wrapped = [
                shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec",
                "-p",
                profile,
                "--",
                *argv,
            ]
            return SandboxedCommand(
                wrapped,
                env,
                self.backend,
                process_kwargs,
                permission_profile,
            )
        if self.backend == "bubblewrap":
            wrapped = self._bubblewrap_argv(
                argv,
                cwd,
                extra_read_roots,
                permission_profile=permission_profile,
                install_roots=install_roots,
                network_allowed=network_allowed,
            )
            return SandboxedCommand(
                wrapped,
                env,
                self.backend,
                process_kwargs,
                permission_profile,
            )
        if self.backend == "container":
            wrapped = self.runtime_policy.container_argv(
                argv,
                cwd,
                workspace_root=self.workspace_root,
                permission_profile=permission_profile,
                install_roots=install_roots,
                network_allowed=network_allowed,
                platform=self.process_platform,
            )
            return SandboxedCommand(
                wrapped,
                env,
                self.backend,
                self.process_platform.subprocess_kwargs(),
                permission_profile,
            )
        return SandboxedCommand(
            argv,
            env,
            self.backend,
            process_kwargs,
            permission_profile,
        )

    def _network_allowed(
        self,
        command: str,
        permission_profile: SandboxPermissionProfile,
        *,
        explicitly_requested: bool = False,
    ) -> bool:
        if permission_profile is SandboxPermissionProfile.WORKSPACE:
            return self.network
        return bool(
            self.network
            or (
                self.install_network
                and (
                    explicitly_requested
                    or _is_install_network_command(
                        command,
                        shell_dialect=self.process_platform.shell_dialect,
                    )
                )
            )
        )

    def _install_roots_for_command(self, command: str) -> tuple[Path, ...]:
        inferred = inferred_user_install_roots(
            command,
            self.user_home,
            self.user_install_roots,
            shell_dialect=self.process_platform.shell_dialect,
        )
        return tuple(dict.fromkeys((*self.user_install_roots, *inferred)))

    def _ensure_user_install_roots(self, roots: Sequence[Path]) -> None:
        try:
            for root in roots:
                root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SandboxError(
                f"无法创建用户安装目录: {exc}") from exc

    def _sandbox_env(
        self,
        cwd: Path,
        permission_profile: SandboxPermissionProfile = (
            SandboxPermissionProfile.WORKSPACE),
        install_roots: Sequence[Path] = (),
    ) -> dict[str, str]:
        base = scrub_environment(os.environ)
        runtime = self.workspace_root / ".krelcode" / "sandbox"
        fake_home = runtime / "home"
        install_home = runtime / "user-install-home"
        user_install = (
            permission_profile is SandboxPermissionProfile.USER_INSTALL)
        home = install_home if user_install else fake_home
        tmp = runtime / "tmp"
        cache = runtime / "cache"
        appdata = home / "AppData"
        local_appdata = appdata / "Local"
        roaming_appdata = appdata / "Roaming"
        try:
            for path in (fake_home, install_home, tmp, cache):
                path.mkdir(parents=True, exist_ok=True)
            if user_install:
                self._link_user_install_roots(install_home, install_roots)
            if home == fake_home or self.process_platform.windows:
                for path in (local_appdata, roaming_appdata):
                    path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SandboxError(f"无法创建 Sandbox 运行目录: {exc}") from exc
        env = dict(base)
        updates = {
            "HOME": str(home),
            "PWD": str(cwd),
            "TMPDIR": str(tmp),
            "TMP": str(tmp),
            "TEMP": str(tmp),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(cache),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "PIP_CACHE_DIR": str(cache / "pip"),
            "UV_CACHE_DIR": str(cache / "uv"),
            "npm_config_cache": str(cache / "npm"),
            "GOCACHE": str(cache / "go-build"),
            "CARGO_HOME": str(cache / "cargo"),
            "GRADLE_USER_HOME": str(cache / "gradle"),
        }
        if user_install:
            user_bin = install_home / ".local" / "bin"
            path_value = env.get("PATH", "")
            updates["XDG_STATE_HOME"] = str(
                install_home / ".local" / "state")
            if str(user_bin) not in path_value.split(os.pathsep):
                updates["PATH"] = (
                    str(user_bin)
                    + (os.pathsep + path_value if path_value else "")
                )
        if self.process_platform.windows:
            updates.update({
                "USERPROFILE": str(home),
                "HOMEDRIVE": home.drive or "",
                "HOMEPATH": (
                    str(home)[len(home.drive):]
                    if home.drive
                    else str(home)
                ),
                "APPDATA": str(roaming_appdata),
                "LOCALAPPDATA": str(local_appdata),
                "COMSPEC": self.process_platform.shell,
            })
        env.update(updates)
        return env

    def _link_user_install_roots(
        self,
        install_home: Path,
        install_roots: Sequence[Path],
    ) -> None:
        """Expose only allowlisted user-install roots through a synthetic HOME."""
        for root in install_roots:
            try:
                relative = root.relative_to(self.user_home)
            except ValueError:
                continue
            link = install_home / relative
            try:
                link.parent.mkdir(parents=True, exist_ok=True)
                if link.is_symlink():
                    if link.resolve() == root:
                        continue
                    link.unlink()
                elif link.exists():
                    if link.resolve() == root:
                        continue
                    continue
                link.symlink_to(root, target_is_directory=True)
            except OSError as exc:
                raise SandboxError(
                    f"无法挂载用户安装目录 {root}: {exc}") from exc

    def _seatbelt_profile(
        self,
        extra_read_roots: Sequence[Path],
        *,
        permission_profile: SandboxPermissionProfile = (
            SandboxPermissionProfile.WORKSPACE),
        install_roots: Optional[Sequence[Path]] = None,
        network_allowed: Optional[bool] = None,
    ) -> str:
        runtime = self.workspace_root / ".krelcode" / "sandbox"
        if install_roots is None:
            install_roots = (
                self.user_install_roots
                if permission_profile is SandboxPermissionProfile.USER_INSTALL
                else ()
            )
        read_roots = list(dict.fromkeys([
            *[root for root in SYSTEM_READ_ROOTS if Path(root).exists()],
            str(Path(sys.executable).resolve().parent),
            str(Path(sys.prefix).resolve()),
            str(Path(sys.base_prefix).resolve()),
            str(self.workspace_root),
            *[str(path) for path in extra_read_roots],
            *[str(path) for path in install_roots],
        ]))
        lines = [
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow signal)",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow file-read-metadata)",
        ]
        lines.append("(allow file-read* " + " ".join(
            f'(subpath "{_seatbelt_quote(path)}")'
            for path in read_roots) + ")")
        lines.append("(allow file-write* " + " ".join(
            f'(subpath "{_seatbelt_quote(path)}")'
            for path in (
                str(self.workspace_root),
                str(runtime),
                *[str(root) for root in install_roots],
            )) + ")")
        lines.append('(allow file-write* (literal "/dev/null"))')
        allow_network = (
            self.network if network_allowed is None else network_allowed)
        if allow_network:
            lines.append("(allow network-outbound)")
        return "\n".join(lines)

    def _bubblewrap_argv(
        self,
        argv: Sequence[str],
        cwd: Path,
        extra_read_roots: Sequence[Path],
        *,
        permission_profile: SandboxPermissionProfile = (
            SandboxPermissionProfile.WORKSPACE),
        install_roots: Optional[Sequence[Path]] = None,
        network_allowed: Optional[bool] = None,
    ) -> List[str]:
        bwrap = shutil.which("bwrap") or "bwrap"
        out = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--cap-drop",
            "ALL",
        ]
        allow_network = (
            self.network if network_allowed is None else network_allowed)
        if allow_network:
            out.append("--share-net")
        out += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
        bind_roots: List[Path] = []
        for raw in SYSTEM_READ_ROOTS:
            root = Path(raw)
            if str(root) in {"/dev", "/proc"}:
                continue
            if root.exists() and not any(
                    _inside(root, seen) for seen in bind_roots):
                bind_roots.append(root)
                out += ["--ro-bind", str(root), str(root)]
        prefix = Path(sys.prefix).resolve()
        if not any(_inside(prefix, seen) for seen in bind_roots):
            bind_roots.append(prefix)
            out += ["--ro-bind", str(prefix), str(prefix)]
        out += [
            "--bind", str(self.workspace_root), str(self.workspace_root)]
        if install_roots is None:
            install_roots = (
                self.user_install_roots
                if permission_profile is SandboxPermissionProfile.USER_INSTALL
                else ()
            )
        if install_roots:
            for root in install_roots:
                out += ["--bind", str(root), str(root)]
        for root in extra_read_roots:
            if _inside(root, self.workspace_root) or any(
                    _inside(root, seen) for seen in bind_roots):
                continue
            out += ["--ro-bind", str(root), str(root)]
        out += ["--chdir", str(cwd), "--", *argv]
        return out


@functools.lru_cache(maxsize=16)
def _detect_backend(requested: str, platform: Optional[str] = None) -> str:
    platform = platform or sys.platform
    if requested == "static":
        return "static"
    if requested in {"auto", "seatbelt"}:
        executable = shutil.which("sandbox-exec")
        if platform == "darwin" and executable and _probe_seatbelt(executable):
            return "seatbelt"
        if requested == "seatbelt":
            raise SandboxError(
                "seatbelt 不可用：仅支持已允许 sandbox-exec 的 macOS")
    if requested in {"auto", "bubblewrap"}:
        executable = shutil.which("bwrap")
        if (
            platform.startswith("linux")
            and executable
            and _probe_bubblewrap(executable)
        ):
            return "bubblewrap"
        if requested == "bubblewrap":
            raise SandboxError(
                "bubblewrap 不可用：需要 Linux、bwrap 和可用的用户命名空间")
    if requested == "container":
        executable = (
            shutil.which("docker")
            or shutil.which("podman")
            or shutil.which("nerdctl")
        )
        if executable:
            return "container"
        raise SandboxError(
            "container backend 需要 docker、podman 或 nerdctl")
    return "static"


def _probe_seatbelt(executable: str) -> bool:
    try:
        completed = subprocess.run(
            [
                executable,
                "-p",
                "(version 1)\n(allow default)\n",
                "--",
                "/bin/true",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=scrub_environment(os.environ),
            timeout=3,
            check=False,
            **current_process_platform().subprocess_kwargs(),
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _probe_bubblewrap(executable: str) -> bool:
    try:
        completed = subprocess.run(
            [
                executable,
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--",
                "/bin/true",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=scrub_environment(os.environ),
            timeout=3,
            check=False,
            **current_process_platform().subprocess_kwargs(),
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _seatbelt_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "ExecutionSandbox",
    "SandboxCapabilities",
    "SandboxError",
    "SandboxIsolationLevel",
    "SandboxPermissionProfile",
    "SandboxedCommand",
]
