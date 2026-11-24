"""Small failure-safe Git helpers shared by agent and session layers."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Optional

from .process import process_isolation_kwargs, stop_process_tree


async def git_command(root: Path, *args: str) -> Optional[str]:
    """Run one bounded git command and return stripped stdout on success."""
    if shutil.which("git") is None:
        return None
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            **process_isolation_kwargs(),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except asyncio.TimeoutError:
        if proc is not None:
            await stop_process_tree(proc, force=True, timeout=1)
        return None
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", "replace").strip()


async def git_is_repo(root: Path) -> bool:
    """Return whether *root* is inside a Git work tree."""
    if shutil.which("git") is None:
        return False
    return await git_command(Path(root), "rev-parse", "--is-inside-work-tree") == "true"


async def ensure_local_exclude(root: Path, entry: str = ".krelcode/") -> bool:
    """Idempotently add *entry* to the repository-local Git exclude file."""
    root = Path(root)
    git_dir = await git_command(root, "rev-parse", "--git-dir")
    if not git_dir:
        return False
    exclude_path = (
        Path(git_dir) / "info" / "exclude"
        if Path(git_dir).is_absolute()
        else root / git_dir / "info" / "exclude"
    )
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.is_file() else ""
        if entry in {line.strip() for line in existing.splitlines()}:
            return False
        with exclude_path.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(f"{entry}\n")
        return True
    except OSError:
        return False


__all__ = ["ensure_local_exclude", "git_command", "git_is_repo"]
