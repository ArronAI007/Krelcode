"""交互编辑核心：无终端依赖的文本缓冲、历史存储与增量搜索。

终端 I/O、ANSI 重绘仍留在 ``ui.repl``。本模块只负责可单测的编辑语义，
便于未来复用到 TUI、Web 或 IDE 前端。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


def sanitize_pasted_text(text: str) -> str:
    """把剪贴板文本规范为可安全写入编辑缓冲的纯文本。

    终端转义序列不能作为普通输入保存，否则下一次重绘时可能被终端解释成
    光标移动、清屏等控制指令。粘贴只保留可打印字符和换行。
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for char in normalized:
        if char == "\n":
            out.append(char)
        elif char == "\t":
            out.append("    ")
        elif char.isprintable():
            out.append(char)
    return "".join(out)


@dataclass
class EditBuffer:
    """带光标的多行文本缓冲；光标是 Python 字符索引。"""

    text: str = ""
    cursor: int = 0

    def __post_init__(self) -> None:
        self.cursor = max(0, min(self.cursor, len(self.text)))

    @property
    def before(self) -> str:
        return self.text[:self.cursor]

    @property
    def after(self) -> str:
        return self.text[self.cursor:]

    def set_text(self, text: str, cursor: Optional[int] = None) -> None:
        self.text = text
        self.cursor = len(text) if cursor is None else max(0, min(cursor, len(text)))

    def insert(self, text: str) -> None:
        if not text:
            return
        self.text = self.before + text + self.after
        self.cursor += len(text)

    def clear(self) -> None:
        self.text = ""
        self.cursor = 0

    def move_left(self) -> bool:
        if self.cursor <= 0:
            return False
        self.cursor -= 1
        return True

    def move_right(self) -> bool:
        if self.cursor >= len(self.text):
            return False
        self.cursor += 1
        return True

    def _line_bounds(self) -> tuple[int, int]:
        start = self.text.rfind("\n", 0, self.cursor) + 1
        end = self.text.find("\n", self.cursor)
        if end < 0:
            end = len(self.text)
        return start, end

    def move_home(self) -> bool:
        start, _ = self._line_bounds()
        if self.cursor == start:
            return False
        self.cursor = start
        return True

    def move_end(self) -> bool:
        _, end = self._line_bounds()
        if self.cursor == end:
            return False
        self.cursor = end
        return True

    def move_line_up(self) -> bool:
        """向上移动一行并尽量保持列位置。"""
        line, column = self.cursor_line_column()
        if line <= 0:
            return False
        current_start = self.text.rfind("\n", 0, self.cursor) + 1
        previous_end = current_start - 1
        previous_start = self.text.rfind("\n", 0, previous_end) + 1
        self.cursor = min(previous_start + column, previous_end)
        return True

    def move_line_down(self) -> bool:
        """向下移动一行并尽量保持列位置。"""
        line, column = self.cursor_line_column()
        if self.text.count("\n") <= line:
            return False
        current_start = self.text.rfind("\n", 0, self.cursor) + 1
        current_end = self.text.find("\n", self.cursor)
        if current_end < 0:
            current_end = len(self.text)
        next_start = current_end + 1
        next_end = self.text.find("\n", next_start)
        if next_end < 0:
            next_end = len(self.text)
        self.cursor = min(next_start + column, next_end)
        return True

    @staticmethod
    def _word_char(char: str) -> bool:
        return char.isalnum() or char == "_"

    def move_word_left(self) -> bool:
        position = self.cursor
        while position > 0 and not self._word_char(self.text[position - 1]):
            position -= 1
        while position > 0 and self._word_char(self.text[position - 1]):
            position -= 1
        if position == self.cursor:
            return False
        self.cursor = position
        return True

    def move_word_right(self) -> bool:
        position = self.cursor
        while position < len(self.text) and not self._word_char(self.text[position]):
            position += 1
        while position < len(self.text) and self._word_char(self.text[position]):
            position += 1
        if position == self.cursor:
            return False
        self.cursor = position
        return True

    def backspace(self) -> bool:
        if self.cursor <= 0:
            return False
        self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
        self.cursor -= 1
        return True

    def delete(self) -> bool:
        if self.cursor >= len(self.text):
            return False
        self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
        return True

    def delete_word_before(self) -> bool:
        original = self.cursor
        self.move_word_left()
        if self.cursor == original:
            return False
        self.text = self.text[:self.cursor] + self.text[original:]
        return True

    def delete_word_after(self) -> bool:
        original = self.cursor
        self.move_word_right()
        if self.cursor == original:
            return False
        self.text = self.text[:original] + self.text[self.cursor:]
        self.cursor = original
        return True

    def delete_to_start(self) -> bool:
        if self.cursor <= 0:
            return False
        self.text = self.text[self.cursor:]
        self.cursor = 0
        return True

    def delete_to_end(self) -> bool:
        if self.cursor >= len(self.text):
            return False
        self.text = self.text[:self.cursor]
        return True

    def cursor_line_column(self) -> tuple[int, int]:
        """返回光标的 0-based 行号与该行中的字符列号。"""
        line = self.text.count("\n", 0, self.cursor)
        start = self.text.rfind("\n", 0, self.cursor) + 1
        return line, self.cursor - start


class InputHistory:
    """用户 prompt 历史；支持内存和可选 JSONL 持久化。"""

    def __init__(
        self,
        limit: int = 500,
        *,
        path: Optional[Path] = None,
        redact: Optional[Callable[[str], str]] = None,
    ) -> None:
        self._entries: list[str] = []
        self._limit = max(1, limit)
        self._path = Path(path) if path is not None else None
        self._redact = redact
        self._index: Optional[int] = None
        self._draft = ""
        self._load()

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        loaded: list[str] = []
        for line in lines[-self._limit:]:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                text = record.get("text", "") if isinstance(record, dict) else str(record)
            except (json.JSONDecodeError, TypeError):
                text = line
            text = str(text).strip()
            if self._redact is not None:
                text = self._redact(text)
            if text and not text.startswith("/") and (not loaded or loaded[-1] != text):
                loaded.append(text)
        self._entries = loaded[-self._limit:]

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temp = self._path.with_suffix(
                f"{self._path.suffix}.{os.getpid()}.tmp")
            payload = "".join(
                json.dumps({"text": text}, ensure_ascii=False) + "\n"
                for text in self._entries
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(temp, flags, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
            finally:
                os.chmod(temp, 0o600)
            os.replace(temp, self._path)
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def add(self, text: str) -> None:
        text = text.strip()
        if not text or text.startswith("/"):
            return
        if self._redact is not None:
            text = self._redact(text).strip()
            if not text:
                return
        if self._entries and self._entries[-1] == text:
            self.reset_navigation()
            return
        self._entries.append(text)
        if len(self._entries) > self._limit:
            del self._entries[:len(self._entries) - self._limit]
        self._save()
        self.reset_navigation()

    def reset_navigation(self) -> None:
        self._index = None
        self._draft = ""

    def previous(self, current: str) -> str:
        if not self._entries:
            return current
        if self._index is None:
            self._draft = current
            self._index = len(self._entries) - 1
        elif self._index > 0:
            self._index -= 1
        return self._entries[self._index]

    def next(self, current: str) -> str:
        if self._index is None:
            return current
        if self._index + 1 < len(self._entries):
            self._index += 1
            return self._entries[self._index]
        draft = self._draft
        self.reset_navigation()
        return draft

    def search(self, query: str, *, before: Optional[int] = None) -> Optional[tuple[int, str]]:
        """从新到旧查找包含 query 的条目；before 为排他上界。"""
        needle = query.casefold()
        start = len(self._entries) - 1 if before is None else min(before - 1, len(self._entries) - 1)
        for index in range(start, -1, -1):
            text = self._entries[index]
            if needle in text.casefold():
                return index, text
        return None


@dataclass
class HistorySearch:
    """Ctrl-R 增量搜索的临时状态。"""

    original_text: str
    original_cursor: int
    query: str = ""
    match_index: Optional[int] = None
    match_text: str = ""
    matched: bool = False

    @classmethod
    def begin(cls, buffer: EditBuffer) -> "HistorySearch":
        return cls(original_text=buffer.text, original_cursor=buffer.cursor)

    def find(self, history: InputHistory, *, older: bool = False) -> bool:
        before = self.match_index if older else None
        result = history.search(self.query, before=before)
        if result is None:
            self.matched = False
            return False
        self.match_index, self.match_text = result
        self.matched = True
        return True

    def cycle(self, history: InputHistory) -> bool:
        if self.match_index is None:
            return self.find(history)
        return self.find(history, older=True)

    def cancel(self, buffer: EditBuffer) -> None:
        buffer.set_text(self.original_text, self.original_cursor)
