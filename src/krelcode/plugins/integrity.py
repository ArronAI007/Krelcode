"""Deterministic plugin-tree hashing with fail-closed path safety checks."""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


SIGNATURE_FILENAME = "plugin.sig.json"
REGISTRY_MARKER_FILENAME = ".krelcode-registry.json"
MAX_INTEGRITY_FILES = 5_000
MAX_INTEGRITY_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
_IGNORED_NAMES = {SIGNATURE_FILENAME, REGISTRY_MARKER_FILENAME, ".DS_Store"}
_IGNORED_DIRS = {".git", "__pycache__"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


class PluginIntegrityError(ValueError):
    """Plugin contents cannot be represented as a safe deterministic package."""


@dataclass(frozen=True)
class FileDigest:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class PluginIntegrity:
    manifest_sha256: str
    files_sha256: str
    files_count: int
    files: tuple[FileDigest, ...]

    @property
    def digest(self) -> str:
        payload = f"{self.manifest_sha256}\n{self.files_sha256}".encode("ascii")
        return hashlib.sha256(payload).hexdigest()


def _is_ignored(relative: PurePosixPath) -> bool:
    if any(part in _IGNORED_DIRS for part in relative.parts[:-1]):
        return True
    if relative.parent == PurePosixPath(".") and relative.name in {
        SIGNATURE_FILENAME, REGISTRY_MARKER_FILENAME
    }:
        return True
    if relative.name == ".DS_Store":
        return True
    return relative.suffix in _IGNORED_SUFFIXES


def safe_relative_path(root: Path, path: Path) -> str:
    """Return a POSIX relative path or reject traversal/symlink escape."""

    root = Path(root).resolve()
    candidate = Path(path)
    try:
        resolved = candidate.resolve(strict=False)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise PluginIntegrityError(f"插件路径越界: {candidate}") from exc
    if not relative.parts or relative == Path("."):
        raise PluginIntegrityError("插件文件路径不能是根目录")
    return relative.as_posix()


def iter_package_files(
    root: Path,
    *,
    max_files: int = MAX_INTEGRITY_FILES,
    max_bytes: int = MAX_INTEGRITY_BYTES,
) -> tuple[FileDigest, ...]:
    """Hash every package file except signatures and generated artifacts."""

    root = Path(root).resolve()
    if not root.is_dir():
        raise PluginIntegrityError(f"插件目录不存在: {root}")
    files: list[FileDigest] = []
    total = 0
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for dirname in sorted(dirnames):
            path = current_path / dirname
            if path.is_symlink():
                raise PluginIntegrityError(f"插件不能包含符号链接: {path}")
            relative_dir = path.relative_to(root)
            if dirname in _IGNORED_DIRS:
                continue
            if relative_dir.parts and relative_dir.parts[0] == ".git":
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for filename in sorted(filenames):
            path = current_path / filename
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if path.is_symlink():
                raise PluginIntegrityError(f"插件不能包含符号链接: {path}")
            if _is_ignored(relative):
                continue
            try:
                stat = path.stat()
            except OSError as exc:
                raise PluginIntegrityError(f"无法读取插件文件: {path}: {exc}") from exc
            if not path.is_file():
                raise PluginIntegrityError(f"插件包含非普通文件: {path}")
            if stat.st_size > MAX_FILE_BYTES:
                raise PluginIntegrityError(
                    f"插件单文件过大: {relative} ({stat.st_size} bytes)")
            total += stat.st_size
            if len(files) + 1 > max_files:
                raise PluginIntegrityError(f"插件文件过多（>{max_files}）")
            if total > max_bytes:
                raise PluginIntegrityError(
                    f"插件体积过大（>{max_bytes // 1024 // 1024} MiB）")
            digest = hashlib.sha256()
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError as exc:
                raise PluginIntegrityError(f"无法读取插件文件: {path}: {exc}") from exc
            files.append(FileDigest(relative.as_posix(), digest.hexdigest(), stat.st_size))
    files.sort(key=lambda item: item.path)
    return tuple(files)


def hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PluginIntegrityError(f"无法读取文件: {path}: {exc}") from exc
    return digest.hexdigest()


def package_integrity(root: Path, manifest_path: Path | None = None) -> PluginIntegrity:
    root = Path(root).resolve()
    manifest = assert_manifest_is_regular(root, manifest_path)
    manifest_relative = safe_relative_path(root, manifest)
    manifest_hash = hash_file(manifest)
    files = tuple(
        item for item in iter_package_files(root)
        if item.path != manifest_relative
    )
    package_digest = hashlib.sha256()
    for item in files:
        package_digest.update(
            f"{item.path}\0{item.sha256}\0{item.size}\n".encode("utf-8"))
    return PluginIntegrity(
        manifest_sha256=manifest_hash,
        files_sha256=package_digest.hexdigest(),
        files_count=len(files),
        files=files,
    )


def assert_manifest_is_regular(root: Path, manifest_path: Path | None = None) -> Path:
    root = Path(root).resolve()
    manifest = Path(manifest_path) if manifest_path is not None else root / "plugin.toml"
    if manifest.is_symlink() or not manifest.is_file():
        raise PluginIntegrityError("plugin.toml 必须是插件目录内的普通文件")
    safe_relative_path(root, manifest)
    return manifest


def purge_generated_files(root: Path) -> None:
    """Remove interpreter caches before verified plugin code is imported.

    A stale attacker-controlled ``.pyc`` with matching source metadata could
    otherwise execute despite a valid source/package signature. Generated files
    are intentionally outside the signed tree, so the verified load path clears
    them before handing control to importlib.
    """

    root = Path(root).resolve()
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for dirname in list(dirnames):
            path = current_path / dirname
            if path.is_symlink():
                raise PluginIntegrityError(f"插件不能包含符号链接: {path}")
            if dirname == "__pycache__":
                shutil.rmtree(path)
                dirnames.remove(dirname)
        for filename in filenames:
            path = current_path / filename
            if path.suffix not in _IGNORED_SUFFIXES:
                continue
            if path.is_symlink():
                raise PluginIntegrityError(f"插件不能包含符号链接: {path}")
            try:
                path.unlink()
            except OSError as exc:
                raise PluginIntegrityError(f"无法清理插件缓存: {path}: {exc}") from exc
