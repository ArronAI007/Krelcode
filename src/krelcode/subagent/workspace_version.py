"""Capture content-light workspace versions for delegated-result validation."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

from ..contracts.delegation import WorkspaceVersion

_IGNORED_PARTS = frozenset({
    ".git", ".krelcode", "__pycache__", ".mypy_cache", ".pytest_cache",
    "node_modules", ".venv", "venv",
})
_MAX_FILES = 20_000


def capture_workspace_version(root: Path) -> WorkspaceVersion:
    workspace = Path(root).resolve()
    captured_at = time.time()
    head = _git(workspace, "rev-parse", "HEAD")
    if head is not None:
        status = _git(workspace, "status", "--porcelain=v1", "-z") or ""
        dirty_digest = hashlib.sha256(
            status.encode("utf-8", errors="replace")).hexdigest()
        workspace_digest = hashlib.sha256(
            f"git\0{head}\0{status}".encode("utf-8", errors="replace")
        ).hexdigest()
        return WorkspaceVersion(
            git_head=head,
            dirty_digest=dirty_digest,
            workspace_digest=workspace_digest,
            captured_at=captured_at,
            is_git=True,
        )

    digest = hashlib.sha256()
    count = 0
    paths: list[Path] = []
    try:
        for path in workspace.rglob("*"):
            relative = path.relative_to(workspace)
            if any(part in _IGNORED_PARTS for part in relative.parts):
                continue
            if path.is_file():
                paths.append(relative)
            if len(paths) >= _MAX_FILES:
                break
    except OSError:
        pass
    paths.sort()
    for path in paths:
        if count >= _MAX_FILES:
            break
        try:
            stat = (workspace / path).stat()
        except OSError:
            continue
        digest.update(
            f"{path.as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}\n"
            .encode("utf-8", errors="replace"))
        count += 1
    return WorkspaceVersion(
        workspace_digest=digest.hexdigest(),
        captured_at=captured_at,
        is_git=False,
        metadata={"file_count": count, "truncated": count >= _MAX_FILES},
    )


def _git(workspace: Path, *args: str) -> str | None:
    env = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    }
    try:
        result = subprocess.run(
            ("git", "-C", str(workspace), *args),
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


__all__ = ["capture_workspace_version"]
