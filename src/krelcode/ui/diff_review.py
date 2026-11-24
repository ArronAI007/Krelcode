"""Bounded, redacted diff review and pager primitives for the terminal UI.

The review layer is intentionally terminal-agnostic.  It turns unified diffs
into a small file/hunk model, can safely preview common file-writing tool calls
without mutating the workspace, and exposes a stateful pager used by the REPL.
No third-party rendering dependency is required.
"""

from __future__ import annotations

import difflib
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence, cast

from ..core.messages import ToolCall
from ..core.redact import SecretRedactor, redact_obvious_secrets
from ..safety.environment import scrub_environment
from ..core.secret_guard import classify_secret_path, redact_secret_text
from .diff_review_state import CurrentDiff, empty_current_diff

MAX_DIFF_CHARS = 240_000
MAX_PREVIEW_FILE_BYTES = 1_000_000
MAX_UNTRACKED_FILES = 200
MAX_FILES = 80
MAX_HUNKS_PER_FILE = 300
MAX_LINES_PER_HUNK = 4_000
MAX_GIT_STDERR_CHARS = 16_000

GREEN = "\x1b[32m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"

_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class DiffLine:
    kind: str  # context | add | remove
    text: str
    old_line: Optional[int] = None
    new_line: Optional[int] = None


@dataclass(frozen=True)
class DiffHunk:
    header: str
    lines: tuple[DiffLine, ...] = ()


@dataclass(frozen=True)
class DiffFile:
    old_path: str = ""
    new_path: str = ""
    hunks: tuple[DiffHunk, ...] = ()
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    note: str = ""
    status: str = "modified"

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or "(unknown)"


@dataclass(frozen=True)
class DiffReview:
    files: tuple[DiffFile, ...] = ()
    source: str = "tool"
    title: str = ""
    reason: str = ""
    notice: str = ""
    truncated: bool = False
    redacted: bool = False

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def additions(self) -> int:
        return sum(item.additions for item in self.files)

    @property
    def deletions(self) -> int:
        return sum(item.deletions for item in self.files)

    @property
    def has_changes(self) -> bool:
        return any(item.hunks or item.binary or item.note for item in self.files)

    @property
    def stats(self) -> str:
        return (
            f"{self.file_count} files · +{self.additions} −{self.deletions}"
            + (" · redacted" if self.redacted else "")
            + (" · truncated" if self.truncated else "")
        )

    def summary_lines(self, limit: int = 5) -> tuple[str, ...]:
        if not self.files:
            return (self.notice or "没有可审阅的文件差异",)
        lines = [self.stats]
        for item in self.files[:max(1, limit)]:
            marker = {
                "added": "+", "deleted": "-", "modified": "~",
                "renamed": "→", "binary": "■",
            }.get(item.status, "~")
            suffix = ""
            if item.binary:
                suffix = " · binary"
            elif item.additions or item.deletions:
                suffix = f" · +{item.additions} −{item.deletions}"
            if item.note:
                suffix += f" · {item.note}"
            lines.append(f"{marker} {item.path}{suffix}")
        if len(self.files) > limit:
            lines.append(f"… +{len(self.files) - limit} files")
        if self.notice:
            lines.append(self.notice)
        return tuple(lines)

    def preview_lines(self, limit: int = 8) -> tuple[str, ...]:
        """Return a compact, redacted first-file diff preview for approvals."""
        if not self.files:
            return (self.notice or "没有可显示的文件差异",)
        file = self.files[0]
        lines: list[str] = []
        if file.binary:
            lines.append(f"[binary] {file.path}")
        for hunk in file.hunks:
            lines.append(hunk.header)
            for item in hunk.lines:
                marker = {"add": "+", "remove": "-"}.get(item.kind, " ")
                lines.append(f"{marker}{item.text}")
                if len(lines) >= max(1, limit):
                    break
            if len(lines) >= max(1, limit):
                break
        if not lines:
            lines.append(f"{file.path}: {file.note or '(no textual hunks)'}")
        total_lines = sum(1 + len(hunk.lines) for hunk in file.hunks)
        if (self.file_count > 1 or total_lines > max(1, limit)) and lines:
            lines[-1] = "…（完整内容见交互式审阅器）"
        return tuple(lines)


def parse_unified_diff(
    text: str,
    *,
    source: str = "tool",
    title: str = "",
    reason: str = "",
    notice: str = "",
    redacted: bool = False,
) -> DiffReview:
    """Parse bounded unified diff text into files, hunks and line numbers."""
    raw = str(text or "")
    truncated = len(raw) > MAX_DIFF_CHARS
    if truncated:
        raw = raw[:MAX_DIFF_CHARS]
    if not raw.strip():
        return DiffReview(
            source=source, title=title, reason=reason,
            notice=notice, truncated=truncated, redacted=redacted)

    files: list[DiffFile] = []
    current: CurrentDiff = empty_current_diff()
    hunk_lines: list[DiffLine] = []
    old_line = 0
    new_line = 0
    file_truncated = False
    review_truncated = False

    def finish_hunk() -> None:
        nonlocal hunk_lines, file_truncated
        if not current or not hunk_lines and not current.get("hunk_header"):
            return
        header = str(current.pop("hunk_header", "") or "")
        if len(hunk_lines) > MAX_LINES_PER_HUNK:
            file_truncated = True
            hunk_lines = hunk_lines[:MAX_LINES_PER_HUNK]
        hunks = cast(list[DiffHunk], list(current.get("hunks", ())))
        if len(hunks) < MAX_HUNKS_PER_FILE:
            hunks.append(DiffHunk(header=header, lines=tuple(hunk_lines)))
        else: file_truncated = True
        current["hunks"] = cast(list[object], hunks)
        hunk_lines = []

    def finish_file() -> None:
        nonlocal current, file_truncated, review_truncated
        finish_hunk()
        if not current:
            return
        if len(files) >= MAX_FILES:
            review_truncated = True
            current = empty_current_diff()
            file_truncated = False
            return
        old_path = str(current.get("old_path", "") or "")
        new_path = str(current.get("new_path", "") or "")
        status = str(current.get("status", "") or "")
        if not status:
            if pending_old is not None and pending_old == "/dev/null":
                status = "added"
            elif pending_new is not None and pending_new == "/dev/null":
                status = "deleted"
            elif current.get("rename"):
                status = "renamed"
            else:
                status = "modified"
        binary = bool(current.get("binary"))
        hunks = cast(tuple[DiffHunk, ...], tuple(current.get("hunks", ())))
        additions = sum(
            1 for hunk in hunks for line in hunk.lines if line.kind == "add")
        deletions = sum(
            1 for hunk in hunks for line in hunk.lines if line.kind == "remove")
        notes = list(current.get("notes", ()))
        if file_truncated:
            notes.append("truncated")
            review_truncated = True
        files.append(DiffFile(
            old_path="" if old_path == "/dev/null" else old_path,
            new_path="" if new_path == "/dev/null" else new_path,
            hunks=hunks,
            additions=additions,
            deletions=deletions,
            binary=binary,
            note=", ".join(dict.fromkeys(notes)),
            status=status,
        ))
        current = empty_current_diff()
        file_truncated = False

    pending_old: Optional[str] = None
    pending_new: Optional[str] = None

    for line in raw.splitlines():
        if line.startswith("diff --git "):
            finish_file()
            current = {"hunks": [], "notes": [], "from_git": True}
            try:
                parts = shlex.split(line)
                if len(parts) >= 4:
                    pending_old = _clean_diff_path(parts[2])
                    pending_new = _clean_diff_path(parts[3])
                    current["old_path"] = pending_old
                    current["new_path"] = pending_new
            except ValueError:
                pending_old = pending_new = None
            continue

        if line.startswith("--- "):
            if current and (
                current.get("hunks")
                or current.get("binary")
                or (not current.get("from_git") and (
                    current.get("new_path")
                    or current.get("old_path") not in (None, "", pending_old)))
            ):
                finish_file()
            if not current:
                current = {"hunks": [], "notes": []}
            pending_old = _clean_diff_path(line[4:])
            current["old_path"] = pending_old
            if pending_old == "/dev/null":
                current["status"] = "added"
            continue

        if line.startswith("+++ "):
            if not current:
                current = {"hunks": [], "notes": []}
            pending_new = _clean_diff_path(line[4:])
            current["new_path"] = pending_new
            if pending_new == "/dev/null":
                current["status"] = "deleted"
            continue

        if line.startswith("new file mode"):
            if current:
                current["status"] = "added"
            continue
        if line.startswith("deleted file mode"):
            if current:
                current["status"] = "deleted"
            continue
        if line.startswith("rename from ") or line.startswith("rename to "):
            if current:
                current["rename"] = True
                current["status"] = "renamed"
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            if not current:
                current = {"hunks": [], "notes": []}
            current["binary"] = True
            current.setdefault("status", "binary")
            continue

        match = _HUNK_RE.match(line)
        if match:
            finish_hunk()
            old_start = int(match.group(1))
            new_start = int(match.group(3))
            old_line, new_line = old_start, new_start
            if not current:
                current = {"hunks": [], "notes": []}
            current["hunk_header"] = line
            continue

        if current and current.get("hunk_header"):
            if line.startswith("+"):
                hunk_lines.append(DiffLine("add", line[1:], None, new_line))
                new_line += 1
            elif line.startswith("-"):
                hunk_lines.append(DiffLine("remove", line[1:], old_line, None))
                old_line += 1
            elif line.startswith(" "):
                hunk_lines.append(DiffLine(
                    "context", line[1:], old_line, new_line))
                old_line += 1
                new_line += 1
            elif line.startswith("\\"):
                return_note = "no newline marker"
                if return_note not in current["notes"]:
                    current["notes"].append(return_note)
            continue
        if current and line.startswith("\\"):
            return_note = "no newline marker"
            if return_note not in current["notes"]:
                current["notes"].append(return_note)

    finish_file()
    return DiffReview(
        files=tuple(files),
        source=source,
        title=title,
        reason=reason,
        notice=notice,
        truncated=truncated or review_truncated,
        redacted=redacted,
    )


def make_unified_diff(before: str, after: str, path: str) -> str:
    """Generate a bounded unified diff without shelling out."""
    lines = list(difflib.unified_diff(
        str(before or "").splitlines(),
        str(after or "").splitlines(),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
        n=3,
    ))
    text = "\n".join(lines)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n…[diff truncated]"
    return text


def review_before_after(
    before: str,
    after: str,
    *,
    path: str,
    source: str = "tool",
    title: str = "",
    reason: str = "",
    redactor: Optional[SecretRedactor] = None,
    notice: str = "",
) -> DiffReview:
    diff = make_unified_diff(before, after, path)
    redacted = False
    cleaned, was_redacted = _redact_text(diff, redactor)
    redacted = redacted or was_redacted
    return parse_unified_diff(
        cleaned,
        source=source,
        title=title,
        reason=reason,
        notice=notice,
        redacted=redacted,
    )


def build_tool_call_review(
    tool_call: ToolCall,
    workspace_root: Path,
    *,
    redactor: Optional[SecretRedactor] = None,
    max_file_bytes: int = MAX_PREVIEW_FILE_BYTES,
) -> DiffReview:
    """Build a no-write preview for common file mutation tools."""
    name = tool_call.name
    args = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
    title = f"{name} · 待写入审阅"
    reason = "文件修改需要人工审阅"

    if name not in {"edit_file", "write_file"}:
        return DiffReview(
            source="tool",
            title=title,
            reason=reason,
            notice="该工具没有可静态生成的文件 diff；请审阅参数后决定是否执行。",
        )

    raw_path = str(args.get("path") or args.get("file_path") or "").strip()
    try:
        path = _resolve_workspace_path(raw_path, workspace_root)
    except (OSError, ValueError) as exc:
        return DiffReview(
            source="tool", title=title, reason=reason,
            notice=f"无法解析目标路径: {exc}")

    category = classify_secret_path(path, workspace_root)
    if category == "block":
        return DiffReview(
            source="tool", title=title, reason=reason,
            notice="目标属于整体敏感文件；为避免泄漏，内容 diff 已隐藏。",
            redacted=True,
        )

    existed = path.exists()
    if existed:
        try:
            value = path.lstat()
            if not stat.S_ISREG(value.st_mode):
                raise ValueError("目标不是普通文件")
            if value.st_size > max_file_bytes:
                return DiffReview(
                    source="tool", title=title, reason=reason,
                    notice=(
                        f"文件大小 {value.st_size} bytes 超过审阅上限 "
                        f"{max_file_bytes}；内容 diff 已隐藏。"))
            before = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            return DiffReview(
                source="tool", title=title, reason=reason,
                notice=f"无法读取原文件以生成 diff: {exc}")
    else:
        before = ""


    if name == "write_file":
        content = args.get("content")
        if not isinstance(content, str):
            return DiffReview(
                source="tool", title=title, reason=reason,
                notice="write_file.content 不是字符串，无法生成预览。")
        after = content
    else:
        old = args.get("old_string")
        new = args.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return DiffReview(
                source="tool", title=title, reason=reason,
                notice="edit_file 的 old_string/new_string 必须都是字符串。")
        if not old:
            return DiffReview(
                source="tool", title=title, reason=reason,
                notice="old_string 为空，无法生成可验证的预写入 diff。")
        count = before.count(old)
        if count == 0:
            return DiffReview(
                source="tool", title=title, reason=reason,
                notice="old_string 未在文件当前内容中精确匹配；执行时可能失败。")
        if count > 1 and not bool(args.get("replace_all", False)):
            return DiffReview(
                source="tool", title=title, reason=reason,
                notice=f"old_string 匹配 {count} 次且未启用 replace_all；执行时预计会中止。")
        after = before.replace(old, new) if bool(args.get("replace_all", False)) else before.replace(old, new, 1)

    if category in {"redact_env", "redact_toml"}:
        before = redact_secret_text(before, category)
        after = redact_secret_text(after, category)

    rel = _relative_path(path, workspace_root)
    if before == after:
        return DiffReview(
            source="tool", title=title, reason=reason,
            notice="预览显示内容未变化。")
    return review_before_after(
        before,
        after,
        path=rel,
        source="tool",
        title=title,
        reason=reason,
        redactor=redactor,
        notice=(
            f"目标: {rel} · 新文件" if not existed
            else f"目标: {rel}"),
    )


def collect_git_review(
    workspace_root: Path,
    *,
    staged: bool = False,
    paths: Sequence[str] = (),
    redactor: Optional[SecretRedactor] = None,
    timeout: float = 12.0,
) -> DiffReview:
    """Collect bounded worktree/staged diff plus safe untracked text files."""
    root = Path(workspace_root).resolve()
    git = shutil.which("git")
    if git is None:
        return DiffReview(
            source="git", title="Git diff", notice="未找到 git 可执行文件。")
    command = [git, "diff", "--no-ext-diff", "--no-color", "--unified=3"]
    if staged:
        command.append("--cached")
    command.append("--")
    safe_paths = tuple(
        item for item in paths if item and not item.startswith("-") and "\x00" not in item)
    command.extend(safe_paths)
    try:
        returncode, stdout, stderr, truncated = _run_bounded_git(
            command, root, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return DiffReview(
            source="git", title="Git diff",
            notice=f"无法读取 Git diff: {type(exc).__name__}: {exc}")
    if returncode != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        return DiffReview(
            source="git", title="Git diff",
            notice=message[:500] or f"git diff 退出码 {returncode}")

    raw = stdout.decode("utf-8", errors="replace")
    pieces = [raw] if raw.strip() else []
    if not staged:
        untracked, untracked_truncated = _untracked_diff_pieces(
            root, safe_paths, budget=max(0, MAX_DIFF_CHARS - len(raw)))
        pieces.extend(untracked)
        truncated = truncated or untracked_truncated
    combined = "\n".join(piece for piece in pieces if piece.strip())
    if len(combined) > MAX_DIFF_CHARS:
        combined = combined[:MAX_DIFF_CHARS] + "\n…[diff truncated]"
        truncated = True
    cleaned, was_redacted = _redact_text(combined, redactor)
    title = "Git staged diff" if staged else "Git worktree diff"
    review = parse_unified_diff(
        cleaned,
        source="git",
        title=title,
        notice="工作区没有未提交差异。" if not combined.strip() else "",
        redacted=was_redacted,
    )
    if truncated:
        review = DiffReview(
            files=review.files,
            source=review.source,
            title=review.title,
            reason=review.reason,
            notice=review.notice,
            truncated=True,
            redacted=review.redacted,
        )
    return review


def _run_bounded_git(
    command: Sequence[str],
    root: Path,
    *,
    timeout: float,
) -> tuple[int, bytes, bytes, bool]:
    """Run git through temp files and read only a bounded prefix per stream.

    ``subprocess.PIPE`` would retain the entire repository diff in memory before
    the parser's limit applies. Temp files keep process plumbing simple and
    cross-platform while this function exposes only the bounded bytes needed by
    the review model.
    """
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        completed = subprocess.run(
            list(command),
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            timeout=timeout,
            check=False,
            env=scrub_environment(os.environ),
        )
        stdout_file.seek(0)
        stdout = stdout_file.read(MAX_DIFF_CHARS + 1)
        stderr_file.seek(0)
        stderr = stderr_file.read(MAX_GIT_STDERR_CHARS + 1)
    truncated = len(stdout) > MAX_DIFF_CHARS
    return completed.returncode, stdout[:MAX_DIFF_CHARS], stderr[:MAX_GIT_STDERR_CHARS], truncated


def _untracked_diff_pieces(
    root: Path,
    paths: Sequence[str],
    *,
    budget: int = MAX_DIFF_CHARS,
) -> tuple[list[str], bool]:
    git = shutil.which("git")
    if git is None:
        return [], False
    command = [git, "ls-files", "--others", "--exclude-standard", "-z", "--"]
    command.extend(paths)
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
            env=scrub_environment(os.environ),
        )
    except (OSError, subprocess.SubprocessError):
        return [], False
    if completed.returncode != 0:
        return [], False
    pieces: list[str] = []
    remaining = max(0, budget)
    for raw_name in completed.stdout.split(b"\x00")[:MAX_UNTRACKED_FILES]:
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", errors="replace")
        category = classify_secret_path(name, root)
        if category == "block":
            continue
        path = root / name
        try:
            value = path.lstat()
            if not stat.S_ISREG(value.st_mode) or value.st_size > MAX_PREVIEW_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            continue
        if category in {"redact_env", "redact_toml"}:
            text = redact_secret_text(text, category)
        body = make_unified_diff("", text, name)
        piece = (
            f"diff --git a/{name} b/{name}\n"
            f"new file mode 100644\n{body}")
        if remaining <= 0:
            return pieces, True
        if len(piece) > remaining:
            pieces.append(piece[:remaining] + "\n…[diff truncated]")
            return pieces, True
        pieces.append(piece)
        remaining -= len(piece)
    return pieces, False


@dataclass
class DiffPager:
    """Navigation state for a modal diff review surface."""

    review: DiffReview
    can_decide: bool = False
    active: bool = True
    file_index: int = 0
    scroll: int = 0
    line_numbers: bool = True
    _last_body_count: int = field(default=0, init=False, repr=False)
    _last_page_size: int = field(default=20, init=False, repr=False)

    @property
    def current_file(self) -> Optional[DiffFile]:
        if not self.review.files:
            return None
        self.file_index = max(0, min(self.file_index, len(self.review.files) - 1))
        return self.review.files[self.file_index]

    def close(self) -> None:
        self.active = False

    def switch_file(self, delta: int) -> bool:
        if not self.review.files:
            return False
        old = self.file_index
        self.file_index = (self.file_index + delta) % len(self.review.files)
        self.scroll = 0
        return self.file_index != old

    def select_file(self, index: int) -> bool:
        if not self.review.files:
            return False
        target = max(0, min(index, len(self.review.files) - 1))
        changed = target != self.file_index
        self.file_index = target
        self.scroll = 0
        return changed

    def scroll_by(self, delta: int) -> bool:
        max_scroll = max(0, self._last_body_count - self._last_page_size)
        target = max(0, min(self.scroll + delta, max_scroll))
        changed = target != self.scroll
        self.scroll = target
        return changed

    def page(self, direction: int) -> bool:
        step = max(1, self._last_page_size - 1)
        return self.scroll_by(direction * step)

    def go_start(self) -> bool:
        changed = self.scroll != 0
        self.scroll = 0
        return changed

    def go_end(self) -> bool:
        target = max(0, self._last_body_count - self._last_page_size)
        changed = target != self.scroll
        self.scroll = target
        return changed

    def toggle_line_numbers(self) -> None:
        self.line_numbers = not self.line_numbers

    def handle_key(self, key: str) -> str:
        """Return ``approve``/``reject``/``close`` or an empty string."""
        if key in {"q", "escape"}:
            self.close()
            return "close"
        if key in {"y", "enter"} and self.can_decide:
            return "approve"
        if key == "n" and self.can_decide:
            return "reject"
        if key in {"j", "down"}:
            self.scroll_by(1)
        elif key in {"k", "up"}:
            self.scroll_by(-1)
        elif key in {"pagedown", " ", "d"}:
            self.page(1)
        elif key in {"pageup", "b"}:
            self.page(-1)
        elif key in {"g", "home"}:
            self.go_start()
        elif key in {"G", "end"}:
            self.go_end()
        elif key in {"]", "tab", "right"}:
            self.switch_file(1)
        elif key in {"[", "shift-tab", "left"}:
            self.switch_file(-1)
        elif key == "#":
            self.toggle_line_numbers()
        return ""

    def render(self, width: int = 80, height: int = 24, *, color: bool = False) -> tuple[str, ...]:
        """Render a bounded overlay; includes border, body and key hints."""
        width = max(24, width)
        height = max(8, height)
        page_size = max(1, height - 7)
        self._last_page_size = page_size
        file = self.current_file
        body = self._body_lines(file)
        self._last_body_count = len(body)
        max_scroll = max(0, len(body) - page_size)
        self.scroll = max(0, min(self.scroll, max_scroll))
        viewport = body[self.scroll:self.scroll + page_size]

        title = self.review.title or "Diff review"
        suffix = f" {self.review.stats} "
        top_inner = max(1, width - 2)
        title_text = f" {title}{suffix} "
        top = "╭" + title_text + "─" * max(0, top_inner - _display_width(title_text)) + "╮"
        lines = [_clip(top, width)]
        index = f"{self.file_index + 1}/{max(1, len(self.review.files))}"
        path_text = file.path if file is not None else (self.review.notice or "无差异")
        state = "待审批" if self.can_decide else "只读"
        summary = f" {index} · {path_text} · {state} · {self.review.stats} "
        lines.append(_box_line(summary, width, color=CYAN if color else False))
        if file is not None and file.note:
            lines.append(_box_line(f" {file.note} ", width, color=DIM if color else False))
        else:
            lines.append("├" + "─" * top_inner + "┤")
        rendered_body = list(viewport)
        if not rendered_body:
            rendered_body.append(self.review.notice or "没有可显示的 hunk")
        while len(rendered_body) < page_size:
            rendered_body.append("")
        lines.extend(
            _box_line(line, width, color=_line_color(line, color))
            for line in rendered_body[:page_size]
        )
        scroll_text = (
            f" {self.scroll + 1 if body else 0}-{min(len(body), self.scroll + page_size)}"
            f"/{len(body)} · j/k ↑/↓ 滚动 · PgUp/PgDn 翻页 · [/] 文件"
        )
        lines.append("├" + "─" * top_inner + "┤")
        lines.append(_box_line(scroll_text, width, color=DIM if color else False))
        controls = " y 允许 · n 拒绝 · q 返回 " if self.can_decide else " q 返回 · # 行号 "
        lines.append(_box_line(controls, width, color=BOLD if color else False))
        lines.append("╰" + "─" * top_inner + "╯")
        return tuple(lines)

    def _body_lines(self, file: Optional[DiffFile]) -> list[str]:
        if file is None:
            return []
        if file.binary:
            return ["[binary file — content not rendered]"]
        if not file.hunks:
            return [file.note or "(no textual hunks)"]
        out: list[str] = []
        for hunk in file.hunks:
            out.append(hunk.header)
            for item in hunk.lines:
                marker = {"add": "+", "remove": "-"}.get(item.kind, " ")
                text = f"{marker}{item.text}"
                if self.line_numbers:
                    old = str(item.old_line) if item.old_line is not None else ""
                    new = str(item.new_line) if item.new_line is not None else ""
                    text = f"{old:>5} {new:>5} {text}"
                out.append(text)
        return out


def _line_color(line: str, enabled: bool) -> str | bool:
    if not enabled or not line:
        return False
    stripped = line.lstrip()
    if stripped.startswith("@@"):
        return CYAN
    # Pager diff rows use ``old new +content`` after optional line numbers.
    marker = line[12:13] if len(line) > 12 else line[:1]
    if marker == "+":
        return GREEN
    if marker == "-":
        return RED
    return False


def _box_line(text: str, width: int, *, color: str | bool = False) -> str:
    inner = max(1, width - 2)
    clipped = _clip(str(text), inner)
    padded = clipped + " " * max(0, inner - _display_width(clipped))
    if color:
        return "│" + str(color) + padded + RESET + "│"
    return "│" + padded + "│"


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if _display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    out: list[str] = []
    used = 0
    for char in _ANSI_RE.sub("", text):
        char_width = _char_width(char)
        if used + char_width > width - 1:
            break
        out.append(char)
        used += char_width
    return "".join(out) + "…"


def _display_width(text: str) -> int:
    return sum(_char_width(char) for char in _ANSI_RE.sub("", text))


def _char_width(char: str) -> int:
    if unicodedata.east_asian_width(char) in "WF":
        return 2
    if unicodedata.combining(char):
        return 0
    return 1


def _clean_diff_path(value: str) -> str:
    path = value.split("\t", 1)[0].strip().strip('"')
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def _resolve_workspace_path(raw: str, workspace_root: Path) -> Path:
    if not raw:
        raise ValueError("路径不能为空")
    root = Path(workspace_root).resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"路径越出工作区: {resolved}")
    return resolved


def _relative_path(path: Path, workspace_root: Path) -> str:
    try:
        return path.resolve().relative_to(Path(workspace_root).resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _redact_text(
    text: str,
    redactor: Optional[SecretRedactor],
) -> tuple[str, bool]:
    original = str(text or "")
    cleaned = redact_obvious_secrets(original)
    if redactor is not None:
        cleaned = redactor.redact(cleaned)
    return cleaned, cleaned != original
