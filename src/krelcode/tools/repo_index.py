"""Incremental, content-bounded repo-map index.

The index stores only symbol metadata keyed by workspace-relative path and
file identity.  A repeat map call reparses only files whose inode, size,
mtime/ctime or mode changed; deletes are removed from the same workspace scope.
No source body is retained after extraction.
"""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Tuple

from .fsutils import is_hidden_file, is_pruned_dir
from .paths import read_text_file_with_snapshot, safe_file_in_workspace
from ..core.secret_guard import classify_secret_path
from .symbols import SOURCE_EXTENSIONS, Symbol, parse_symbols

MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class IndexFingerprint:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    mode: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "IndexFingerprint":
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            size=value.st_size,
            mtime_ns=value.st_mtime_ns,
            ctime_ns=value.st_ctime_ns,
            mode=stat.S_IMODE(value.st_mode),
        )


@dataclass(frozen=True)
class IndexedFile:
    relative_path: str
    fingerprint: IndexFingerprint
    symbols: Tuple[Symbol, ...]
    backend: str


@dataclass(frozen=True)
class RepoMapSnapshot:
    files: Tuple[IndexedFile, ...]
    parsed_files: int
    reused_files: int
    removed_files: int
    parser_backends: Mapping[str, int]


class RepoMapIndex:
    """Thread-safe in-process symbol cache for one tool/tree instance."""

    def __init__(self) -> None:
        self._entries: Dict[Tuple[str, str], IndexedFile] = {}
        self._lock = threading.RLock()

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def build(self, base: Path, workspace_root: Path) -> RepoMapSnapshot:
        root = workspace_root.resolve()
        scope = _scope_prefix(base, root)
        parsed = 0
        reused = 0
        seen: set[Tuple[str, str]] = set()
        with self._lock:
            for path in _iter_source_files(base, root):
                try:
                    resolved = path.resolve(strict=True)
                    before = IndexFingerprint.from_stat(resolved.stat())
                    relative = resolved.relative_to(root).as_posix()
                except (OSError, RuntimeError, ValueError):
                    continue
                key = (str(root), relative)
                cached = self._entries.get(key)
                if cached is not None and cached.fingerprint == before:
                    reused += 1
                    seen.add(key)
                    continue
                try:
                    source, _ = read_text_file_with_snapshot(resolved)
                    after = IndexFingerprint.from_stat(resolved.stat())
                except (OSError, RuntimeError, ValueError):
                    continue
                if after != before:
                    # The path changed between the preflight stat and the
                    # descriptor-verified read. Skip this pass; the next build
                    # reparses the stable replacement instead of associating
                    # old symbols with a new fingerprint.
                    continue
                parsed_symbols = parse_symbols(resolved, source)
                self._entries[key] = IndexedFile(
                    relative_path=relative,
                    fingerprint=after,
                    symbols=parsed_symbols.symbols,
                    backend=parsed_symbols.backend,
                )
                parsed += 1
                seen.add(key)

            stale = [
                key for key in self._entries
                if key[0] == str(root)
                and _under_scope(key[1], scope)
                and key not in seen
            ]
            for key in stale:
                del self._entries[key]

            entries = sorted(
                (
                    entry for (entry_root, _), entry in self._entries.items()
                    if entry_root == str(root) and _under_scope(entry.relative_path, scope)
                ),
                key=lambda entry: entry.relative_path,
            )
            backend_counts: Dict[str, int] = {}
            for entry in entries:
                backend_counts[entry.backend] = backend_counts.get(entry.backend, 0) + 1
            return RepoMapSnapshot(
                files=tuple(entries),
                parsed_files=parsed,
                reused_files=reused,
                removed_files=len(stale),
                parser_backends={key: backend_counts[key] for key in sorted(backend_counts)},
            )


def _iter_source_files(base: Path, workspace_root: Path) -> Iterable[Path]:
    """Yield safe source files; contents are read only after cache invalidation."""
    for current, dirs, files in os.walk(base):
        dirs[:] = sorted(
            name for name in dirs
            if not is_pruned_dir(name)
            and not Path(current, name).is_symlink()
        )
        for name in sorted(files):
            if is_hidden_file(name):
                continue
            path = Path(current) / name
            if path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            if not safe_file_in_workspace(path, workspace_root):
                continue
            if classify_secret_path(path, workspace_root) is not None:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path


def _scope_prefix(base: Path, root: Path) -> str:
    try:
        relative = base.resolve().relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return "."
    return "" if relative == "." else relative


def _under_scope(relative: str, scope: str) -> bool:
    if not scope:
        return True
    return relative == scope or relative.startswith(scope + "/")


__all__ = [
    "IndexFingerprint",
    "IndexedFile",
    "MAX_FILE_BYTES",
    "RepoMapIndex",
    "RepoMapSnapshot",
]
