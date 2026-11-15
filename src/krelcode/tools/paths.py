"""共享路径助手：workspace 解析、越界防护与安全文件读写。

规则：
- 相对路径基于 ctx.workspace_root 解析
- 解析（含符号链接）后必须位于 workspace_root 内，否则拒绝
- 文件读写拒绝符号链接切换、多目录项硬链接、超大文件和只读覆盖
- edit 使用读取快照做乐观并发校验，写入通过同目录文件描述符原子替换
"""

from __future__ import annotations

import errno
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..core.errors import KrelcodeError
from .base import ToolContext


class PathOutsideWorkspaceError(KrelcodeError):
    """目标路径越出工作区。"""

    def __init__(self, target: Path, root: Path) -> None:
        super().__init__(f"路径越出工作区: {target}（工作区: {root}）")
        self.target = target
        self.root = root


class UnsafeFileError(KrelcodeError):
    """目标不是可安全访问的普通文件。"""


class FileChangedError(KrelcodeError):
    """文件在读取与写入之间发生变化，拒绝覆盖新版本。"""


MAX_TEXT_FILE_BYTES = 10_000_000
MAX_WRITE_BYTES = 5_000_000


@dataclass(frozen=True)
class FileSnapshot:
    """用于乐观并发校验的文件身份与版本快照。"""

    device: int
    inode: int
    size: int
    mtime_ns: int
    mode: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "FileSnapshot":
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            size=value.st_size,
            mtime_ns=value.st_mtime_ns,
            mode=value.st_mode,
        )


def resolve_in_workspace(ctx: ToolContext, raw: str) -> Path:
    """把工具入参路径解析为工作区内绝对路径；越界抛 PathOutsideWorkspaceError。"""
    if not raw or not str(raw).strip():
        raise ValueError("路径不能为空")
    root = ctx.workspace_root.resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise PathOutsideWorkspaceError(resolved, root)
    return resolved


def safe_file_in_workspace(path: Path, workspace_root: Path) -> bool:
    """遍历器专用：文件 resolve 后必须仍在工作区且是可读普通文件。

    `os.walk(followlinks=False)` 不会递归 symlink 目录，但仍会把指向外部的
    symlink 文件放进 files；搜索/解析类工具必须在打开前再经过本函数。
    """
    try:
        root = Path(workspace_root).resolve()
        resolved = Path(path).resolve(strict=True)
        value = resolved.stat()
        return (
            stat.S_ISREG(value.st_mode)
            and value.st_nlink == 1
            and (resolved == root or root in resolved.parents)
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _validate_regular_file(path: Path, value: os.stat_result) -> None:
    if not stat.S_ISREG(value.st_mode):
        raise UnsafeFileError(f"目标不是普通文件: {path}")
    if value.st_nlink != 1:
        raise UnsafeFileError(
            f"拒绝操作硬链接文件（可能有 {value.st_nlink} 个目录项）: {path}")
    if value.st_size > MAX_TEXT_FILE_BYTES:
        raise UnsafeFileError(
            f"文件过大（{value.st_size} bytes，上限 {MAX_TEXT_FILE_BYTES}）: {path}")


def read_text_file_with_snapshot(path: Path) -> tuple[str, FileSnapshot]:
    """通过 no-follow 文件描述符读取文本，并返回一致性快照。"""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ELOOP:
            raise UnsafeFileError(f"拒绝跟随符号链接: {path}") from exc
        raise
    try:
        before_stat = os.fstat(fd)
        _validate_regular_file(path, before_stat)
        before = FileSnapshot.from_stat(before_stat)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_TEXT_FILE_BYTES:
                raise UnsafeFileError(
                    f"文件读取超过上限 {MAX_TEXT_FILE_BYTES} bytes: {path}")
            chunks.append(chunk)
        after = FileSnapshot.from_stat(os.fstat(fd))
    except UnicodeDecodeError as e:
        raise ValueError(f"无法按 UTF-8 解码（可能是二进制文件）: {path.name} ({e})") from e
    finally:
        os.close(fd)

    if after != before or total != before.size:
        raise FileChangedError(f"文件读取期间发生变化: {path}")
    try:
        text = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"无法按 UTF-8 解码（可能是二进制文件）: {path.name} ({e})") from e
    return text, before


def read_text_file(path: Path) -> str:
    """读文本文件；拒绝硬链接、符号链接切换和超大文件。"""
    text, _ = read_text_file_with_snapshot(path)
    return text


def _snapshot_at(parent_fd: int, name: str, path: Path) -> FileSnapshot:
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        value = os.fstat(fd)
        _validate_regular_file(path, value)
        return FileSnapshot.from_stat(value)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("写入返回 0 字节")
        view = view[written:]


def atomic_write_text(
    path: Path,
    text: str,
    *,
    expected: Optional[FileSnapshot] = None,
) -> FileSnapshot:
    """同目录原子写文本；拒绝硬链接并在替换前校验 expected 快照。

    temp 文件和 replace 都相对同一个已打开的父目录 fd 完成，避免父目录在
    校验后被替换成符号链接而把写入重定向到工作区外。
    """
    data = text.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise UnsafeFileError(
            f"写入内容过大（{len(data)} bytes，上限 {MAX_WRITE_BYTES}）")
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_fd = os.open(path.parent, parent_flags)
    temp_name = f".{path.name[:80]}.{os.getpid()}.{time.time_ns()}.tmp"
    temp_fd: Optional[int] = None
    replaced = False
    try:
        current: Optional[FileSnapshot]
        try:
            current = _snapshot_at(parent_fd, path.name, path)
        except FileNotFoundError:
            current = None
        if expected is not None and current != expected:
            raise FileChangedError(
                f"文件在读取后发生变化，已中止写入: {path}")
        if current is not None and not (current.mode & 0o222):
            raise UnsafeFileError(f"拒绝覆盖只读文件: {path}")

        mode = (current.mode & 0o777) if current is not None else 0o644
        temp_fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        _write_all(temp_fd, data)
        os.fchmod(temp_fd, mode)
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None

        verify: Optional[FileSnapshot]
        try:
            verify = _snapshot_at(parent_fd, path.name, path)
        except FileNotFoundError:
            verify = None
        if verify != current:
            raise FileChangedError(
                f"文件在写入期间发生变化，已中止原子替换: {path}")

        os.replace(
            temp_name, path.name,
            src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
        )
        replaced = True
        try:
            os.fsync(parent_fd)
        except OSError:
            pass  # 部分文件系统不支持目录 fsync；rename 已原子完成
        return _snapshot_at(parent_fd, path.name, path)
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if not replaced:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)
