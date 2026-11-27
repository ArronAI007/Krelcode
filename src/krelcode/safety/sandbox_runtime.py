"""Sandbox resource ceilings, egress allowlists and container wrapping."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, cast

from ..contracts.governance import SandboxResourceLimits
from ..core.process import ProcessIsolationKwargs, ProcessPlatform


class SandboxRuntimePolicy:
    """Translate Sandbox policy into process/container launch controls."""

    def __init__(
        self,
        resource_limits: SandboxResourceLimits,
        *,
        allowed_domains: Iterable[str] = (),
        container_image: str = "python:3.12-slim",
    ) -> None:
        self.resource_limits = resource_limits
        self.allowed_domains = tuple(dict.fromkeys(
            str(item).strip().lower()
            for item in allowed_domains
            if str(item).strip()
        ))
        self.container_image = str(container_image or "python:3.12-slim")

    def process_kwargs(
        self,
        platform: ProcessPlatform,
    ) -> ProcessIsolationKwargs:
        kwargs = dict(platform.subprocess_kwargs())
        preexec = self._resource_preexec(platform)
        if preexec is not None:
            kwargs["preexec_fn"] = preexec
        return cast(ProcessIsolationKwargs, kwargs)

    def egress_violation(self, command: str) -> Optional[str]:
        if not self.allowed_domains:
            return None
        urls = re.findall(r"https?://[^\s\"'`;|&)]+", command, re.IGNORECASE)
        for raw in urls:
            match = re.match(r"https?://([^/:?#]+)", raw, re.IGNORECASE)
            if match is None:
                continue
            host = match.group(1).lower().rstrip(".")
            if not any(
                host == allowed or host.endswith("." + allowed)
                for allowed in self.allowed_domains
            ):
                return f"网络目标域名不在 allowlist: {host}"
        return None

    def ensure_storage(self, workspace_root: Path) -> None:
        limit_mb = self.resource_limits.storage_mb
        if limit_mb is None:
            return
        limit_bytes = limit_mb * 1024 * 1024
        used = _bounded_directory_size(
            workspace_root / ".krelcode" / "sandbox",
            limit_bytes=limit_bytes,
        )
        if used > limit_bytes:
            raise ValueError(
                f"Sandbox 临时存储已超过配额: {used} > {limit_bytes} bytes")

    def container_argv(
        self,
        argv: Sequence[str],
        cwd: Path,
        *,
        workspace_root: Path,
        permission_profile: str,
        install_roots: Sequence[Path] = (),
        network_allowed: bool,
        platform: ProcessPlatform,
    ) -> List[str]:
        executable = (
            shutil.which("docker")
            or shutil.which("podman")
            or shutil.which("nerdctl")
        )
        if not executable:
            raise ValueError("container backend 需要 docker、podman 或 nerdctl")
        limits = self.resource_limits
        out = [
            executable,
            "run",
            "--rm",
            "--init",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--network",
            "bridge" if network_allowed else "none",
            "--workdir",
            str(cwd),
            "--volume",
            f"{workspace_root}:{workspace_root}:rw",
        ]
        if not platform.windows:
            out += ["--user", f"{os.getuid()}:{os.getgid()}"]
        if limits.memory_mb is not None:
            out += ["--memory", f"{limits.memory_mb}m"]
        if limits.process_limit is not None:
            out += ["--pids-limit", str(limits.process_limit)]
        if limits.cpu_seconds is not None:
            out += ["--ulimit", f"cpu={limits.cpu_seconds}"]
        if limits.storage_mb is not None:
            out += [
                "--tmpfs",
                f"/tmp:rw,size={limits.storage_mb}m",
                "--ulimit",
                f"fsize={limits.storage_mb * 1024 * 1024}",
            ]
        if permission_profile == "user-install":
            for root in install_roots:
                out += ["--volume", f"{root}:{root}:rw"]
        out += [self.container_image, *argv]
        return out

    def _resource_preexec(
        self,
        platform: ProcessPlatform,
    ) -> Optional[Callable[[], None]]:
        limits = self.resource_limits
        if platform.windows or not any((
            limits.cpu_seconds,
            limits.memory_mb,
            limits.storage_mb,
            limits.process_limit,
        )):
            return None

        def apply_limits() -> None:
            try:
                import resource
            except ImportError:  # pragma: no cover
                return
            _set_limit(
                resource,
                "RLIMIT_CPU",
                limits.cpu_seconds,
            )
            _set_limit(
                resource,
                "RLIMIT_AS",
                limits.memory_mb * 1024 * 1024
                if limits.memory_mb is not None else None,
            )
            _set_limit(
                resource,
                "RLIMIT_FSIZE",
                limits.storage_mb * 1024 * 1024
                if limits.storage_mb is not None else None,
            )
            _set_limit(resource, "RLIMIT_NPROC", limits.process_limit)

        return apply_limits


def _set_limit(resource: object, name: str, value: Optional[int]) -> None:
    if value is None or not hasattr(resource, name):
        return
    try:
        setter = getattr(resource, "setrlimit")
        setter(getattr(resource, name), (value, value))
    except (OSError, ValueError):
        pass


def _bounded_directory_size(root: Path, *, limit_bytes: int) -> int:
    if not root.is_dir():
        return 0
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
            if total > limit_bytes:
                return total
    except OSError:
        return total
    return total


__all__ = ["SandboxRuntimePolicy"]
