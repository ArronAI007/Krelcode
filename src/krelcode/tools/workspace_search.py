"""Workspace text-search backends with a security-equivalent fallback.

The public tool contract is still implemented by ``GrepTool``.  This module
separates the backend concern so ripgrep can accelerate the hot path without
weakening the existing file boundary:

* candidate files are enumerated and filtered by the Python security helpers;
* ripgrep receives only those already-safe regular files, never a raw directory;
* a JSON parse or unsupported-regex failure discards ripgrep output and reruns
  the same bounded search in Python;
* every emitted match is re-checked against the workspace and secret policy.

The Python backend remains the zero-dependency reference implementation.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from krelcode.contracts.tool import TurnAbortSignal
from .fsutils import is_hidden_file, is_pruned_dir
from .paths import safe_file_in_workspace
from ..core.process import process_isolation_kwargs, stop_process_tree
from ..core.secret_guard import classify_secret_path

MAX_LINE_CHARS = 300
_RIPGREP_BATCH_FILES = 256
_RIPGREP_BATCH_CHARS = 96_000
_RIPGREP_STREAM_LIMIT = 16 * 1024 * 1024


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class SearchBatch:
    matches: Tuple[SearchMatch, ...]
    truncated: bool
    files_scanned: int
    backend: str


class RipgrepExecutionError(RuntimeError):
    """The trusted ripgrep backend could not produce a compatible result."""


class WorkspaceSearcher:
    """Run a bounded workspace regex search with automatic backend fallback."""

    def __init__(self, *, backend: str = "auto", rg_path: Optional[str] = None) -> None:
        if backend not in {"auto", "ripgrep", "python"}:
            raise ValueError("backend 必须是 auto / ripgrep / python")
        self.backend = backend
        self._rg_path = rg_path
        self._rg_checked = rg_path is not None

    async def search(
        self,
        *,
        pattern: str,
        regex: re.Pattern[str],
        base: Path,
        workspace_root: Path,
        include: Optional[str],
        ignore_case: bool,
        max_results: int,
        abort: TurnAbortSignal,
    ) -> SearchBatch:
        files = tuple(_iter_safe_search_files(
            base, workspace_root, include=include))
        abort.check()
        if not files:
            backend = "python"
            if self.backend != "python" and self._resolve_rg() is not None:
                backend = "ripgrep"
            return SearchBatch((), False, 0, backend)

        if self.backend != "python" and _ripgrep_pattern_compatible(pattern):
            rg = self._resolve_rg()
            if rg is None and self.backend == "ripgrep":
                return await _python_search(
                    regex=regex,
                    files=files,
                    workspace_root=workspace_root,
                    max_results=max_results,
                    abort=abort,
                    backend="python",
                )
            if rg is not None:
                try:
                    return await _ripgrep_search(
                        rg_path=rg,
                        pattern=pattern,
                        regex=regex,
                        files=files,
                        workspace_root=workspace_root,
                        ignore_case=ignore_case,
                        max_results=max_results,
                        abort=abort,
                    )
                except RipgrepExecutionError:
                    # Unsupported Rust-regex features, transient file removal or
                    # a malformed backend response must not change semantics.
                    pass

        return await _python_search(
            regex=regex,
            files=files,
            workspace_root=workspace_root,
            max_results=max_results,
            abort=abort,
            backend="python",
        )

    def _resolve_rg(self) -> Optional[str]:
        if not self._rg_checked:
            self._rg_path = shutil.which("rg")
            self._rg_checked = True
        return self._rg_path


def _iter_safe_search_files(
    base: Path,
    workspace_root: Path,
    *,
    include: Optional[str],
) -> Iterable[Path]:
    """Yield sorted, real, non-secret candidate files without reading contents."""
    root = workspace_root.resolve()
    candidates: List[Tuple[str, Path]] = []
    for current, dirs, files in os.walk(base):
        dirs[:] = sorted(
            name for name in dirs
            if not is_pruned_dir(name)
            and not Path(current, name).is_symlink()
        )
        for name in sorted(files):
            if is_hidden_file(name):
                continue
            if include and not fnmatch.fnmatch(name, include):
                continue
            path = Path(current) / name
            if not safe_file_in_workspace(path, root):
                continue
            if classify_secret_path(path, root) is not None:
                continue
            if _is_probably_binary(path):
                continue
            try:
                resolved = path.resolve(strict=True)
                relative = resolved.relative_to(root).as_posix()
            except (OSError, RuntimeError, ValueError):
                continue
            candidates.append((relative, resolved))
    candidates.sort(key=lambda item: (item[0], str(item[1])))
    return tuple(path for _, path in candidates)


def _is_probably_binary(path: Path) -> bool:
    """Apply the legacy NUL-byte guard before ripgrep may inspect the file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return False
    try:
        return b"\x00" in os.read(fd, 8192)
    except OSError:
        return False
    finally:
        os.close(fd)


def _ripgrep_pattern_compatible(pattern: str) -> bool:
    """Conservatively reject Python-only regex features unsupported by Rust.

    Python must remain the semantic reference. Letting ripgrep consume a
    pattern whose syntax it interprets differently can silently omit matches,
    so suspicious constructs are routed to the Python backend before spawning
    a subprocess. The detector is intentionally conservative: a false negative
    hurts performance, while a false positive could change search results.
    """
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            if index + 1 >= len(pattern):
                return False
            escaped = pattern[index + 1]
            if escaped.isdigit() or escaped in {"Z", "G", "K", "R", "X"}:
                return False
            if escaped == "N" and index + 2 < len(pattern) and pattern[index + 2] == "{":
                return False
            if escaped == "g" and index + 2 < len(pattern) and pattern[index + 2] in "<'":
                return False
            index += 2
            continue
        if char == "[" and not in_class:
            in_class = True
            index += 1
            continue
        if char == "]" and in_class:
            in_class = False
            index += 1
            continue
        if in_class:
            index += 1
            continue
        if char == "(" and pattern.startswith("(?", index):
            prefix = pattern[index:index + 4]
            if prefix.startswith(("(?=", "(?!", "(?(", "(?#", "(?>", "(?P=")):
                return False
            if pattern.startswith("(?<=", index) or pattern.startswith("(?<!", index):
                return False
            if pattern.startswith(("(?a", "(?L"), index):
                return False
        if char in "*+?}" and index + 1 < len(pattern) and pattern[index + 1] == "+":
            return False
        index += 1
    return True


async def _ripgrep_search(
    *,
    rg_path: str,
    pattern: str,
    regex: re.Pattern[str],
    files: Sequence[Path],
    workspace_root: Path,
    ignore_case: bool,
    max_results: int,
    abort: TurnAbortSignal,
) -> SearchBatch:
    matches: List[SearchMatch] = []
    truncated = False
    for batch in _file_batches(files):
        abort.check()
        batch_matches, reached_limit = await _ripgrep_batch(
            rg_path=rg_path,
            pattern=pattern,
            regex=regex,
            files=batch,
            workspace_root=workspace_root,
            ignore_case=ignore_case,
            remaining=max_results - len(matches),
            abort=abort,
        )
        matches.extend(batch_matches)
        if reached_limit:
            truncated = True
            break
        if len(matches) >= max_results:
            truncated = True
            break
    return SearchBatch(
        matches=tuple(matches[:max_results]),
        truncated=truncated,
        files_scanned=len(files),
        backend="ripgrep",
    )


async def _ripgrep_batch(
    *,
    rg_path: str,
    pattern: str,
    regex: re.Pattern[str],
    files: Sequence[Path],
    workspace_root: Path,
    ignore_case: bool,
    remaining: int,
    abort: TurnAbortSignal,
) -> Tuple[List[SearchMatch], bool]:
    command = [
        rg_path,
        "--json",
        "--no-config",
        "--no-messages",
        "--no-ignore",
        "--no-follow",
        "--sort", "path",
        "--color", "never",
        "--encoding", "utf-8",
    ]
    if ignore_case:
        command.append("--ignore-case")
    command.extend(["-e", pattern, "--", *map(str, files)])
    env = {
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
    }
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=_RIPGREP_STREAM_LIMIT,
            **process_isolation_kwargs(),
        )
    except (FileNotFoundError, OSError) as exc:
        raise RipgrepExecutionError(f"无法启动 ripgrep: {exc}") from exc
    matches: List[SearchMatch] = []
    reached_limit = False
    try:
        assert process.stdout is not None
        while True:
            abort.check()
            raw = await process.stdout.readline()
            if not raw:
                break
            try:
                event = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                raise RipgrepExecutionError("ripgrep JSON 输出损坏") from exc
            if event.get("type") != "match":
                continue
            parsed = _parse_ripgrep_match(
                event, workspace_root=workspace_root, regex=regex)
            if parsed is None:
                continue
            matches.append(parsed)
            if len(matches) >= remaining:
                reached_limit = True
                break
        if reached_limit:
            await _stop_process(process)
            return matches, True
        returncode = await process.wait()
        if returncode not in {0, 1}:
            stderr = b""
            if process.stderr is not None:
                stderr = await process.stderr.read()
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise RipgrepExecutionError(
                f"ripgrep 执行失败: {detail[:300] or 'unknown error'}")
        return matches, False
    except asyncio.CancelledError:
        await _stop_process(process)
        raise
    except Exception:
        await _stop_process(process)
        raise


def _parse_ripgrep_match(
    event: dict[str, object],
    *,
    workspace_root: Path,
    regex: re.Pattern[str],
) -> Optional[SearchMatch]:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    path_data = data.get("path")
    lines_data = data.get("lines")
    if not isinstance(path_data, dict) or not isinstance(lines_data, dict):
        return None
    raw_path = path_data.get("text")
    raw_line = lines_data.get("text")
    line_number = data.get("line_number")
    if not isinstance(raw_path, str) or not isinstance(raw_line, str):
        return None
    if not isinstance(line_number, int) or line_number < 1:
        return None
    text = raw_line.rstrip("\r\n")
    if "\x00" in text or regex.search(text) is None:
        return None
    path = Path(raw_path)
    if not safe_file_in_workspace(path, workspace_root):
        return None
    if classify_secret_path(path, workspace_root) is not None:
        return None
    try:
        relative = path.resolve(strict=True).relative_to(
            workspace_root.resolve()).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None
    if len(text) > MAX_LINE_CHARS:
        text = text[:MAX_LINE_CHARS] + "…"
    return SearchMatch(path=relative, line=line_number, text=text)


async def _python_search(
    *,
    regex: re.Pattern[str],
    files: Sequence[Path],
    workspace_root: Path,
    max_results: int,
    abort: TurnAbortSignal,
    backend: str,
) -> SearchBatch:
    matches: List[SearchMatch] = []
    truncated = False
    for path in files:
        abort.check()
        if not safe_file_in_workspace(path, workspace_root):
            continue
        if classify_secret_path(path, workspace_root) is not None:
            continue
        fd: Optional[int] = None
        try:
            relative = path.resolve(strict=True).relative_to(
                workspace_root.resolve()).as_posix()
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                continue
            with os.fdopen(
                fd, "r", encoding="utf-8", errors="ignore",
            ) as handle:
                fd = None
                for line_number, line in enumerate(handle, start=1):
                    if "\x00" in line:
                        break
                    if regex.search(line) is None:
                        continue
                    text = line.rstrip("\r\n")
                    if len(text) > MAX_LINE_CHARS:
                        text = text[:MAX_LINE_CHARS] + "…"
                    matches.append(SearchMatch(relative, line_number, text))
                    if len(matches) >= max_results:
                        truncated = True
                        break
        except (OSError, RuntimeError, ValueError):
            continue
        finally:
            if fd is not None:
                os.close(fd)
        if truncated:
            break
    return SearchBatch(
        matches=tuple(matches),
        truncated=truncated,
        files_scanned=len(files),
        backend=backend,
    )


def _file_batches(files: Sequence[Path]) -> Iterable[Sequence[Path]]:
    batch: List[Path] = []
    chars = 0
    for path in files:
        size = len(str(path)) + 1
        if batch and (
            len(batch) >= _RIPGREP_BATCH_FILES
            or chars + size > _RIPGREP_BATCH_CHARS
        ):
            yield tuple(batch)
            batch = []
            chars = 0
        batch.append(path)
        chars += size
    if batch:
        yield tuple(batch)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    await stop_process_tree(process, force=True, timeout=1.0)


__all__ = [
    "RipgrepExecutionError",
    "SearchBatch",
    "SearchMatch",
    "WorkspaceSearcher",
]
