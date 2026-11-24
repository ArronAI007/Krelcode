"""Live workspace fingerprints used to invalidate restored context."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Sequence

from krelcode.agent.context.context_checkpoint_models import (
    MAX_TREE_ENTRIES,
    WorkspaceFingerprint,
)
from krelcode.core.process import process_isolation_kwargs


_MANAGED_INSTRUCTION_RELPATH = Path(".krelcode") / "managed" / "AGENTS.md"


def workspace_fingerprint(
    workspace_root: Path,
    *,
    rules_content: str = "",
    rules_sources: Sequence[str] = (),
) -> WorkspaceFingerprint:
    root = Path(workspace_root).resolve()
    managed = root / _MANAGED_INSTRUCTION_RELPATH
    managed_digest = ""
    if managed.is_file():
        try:
            managed_digest = hashlib.sha256(managed.read_bytes()).hexdigest()
        except OSError:
            pass
    return WorkspaceFingerprint(
        branch=_git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        head=_git(root, "rev-parse", "HEAD"),
        status_digest=_digest(_git(root, "status", "--porcelain=v1")),
        tree_digest=_workspace_tree_digest(root),
        rules_digest=_digest(rules_content),
        rules_sources=tuple(str(item) for item in rules_sources),
        managed_digest=managed_digest,
        captured_at=time.time(),
    )



def _digest(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest() if value else ""



def _git(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            **process_isolation_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _workspace_tree_digest(root: Path) -> str:
    """Cheap directory fingerprint for stale-state invalidation evidence."""
    skip = {".git", ".krelcode", "__pycache__", "node_modules", ".venv", "venv"}
    digest = hashlib.sha256()
    count = 0
    try:
        for base, dirs, files in os.walk(root):
            dirs[:] = sorted(
                item for item in dirs
                if item not in skip and not item.startswith(".pytest")
            )
            base_path = Path(base)
            for name in sorted(files):
                if name.endswith((".pyc", ".pyo")):
                    continue
                path = base_path / name
                try:
                    stat = path.stat()
                    rel = path.relative_to(root).as_posix()
                except (OSError, ValueError):
                    continue
                digest.update(
                    f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8"))
                count += 1
                if count >= MAX_TREE_ENTRIES:
                    return digest.hexdigest()
    except OSError:
        pass
    return digest.hexdigest()


__all__ = ["workspace_fingerprint"]
