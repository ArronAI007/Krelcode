"""会话层有序消息队列。

队列只管理顺序，不依赖终端、AgentLoop 或 InputAction，便于 REPL、未来 TUI
与无头测试复用。面对用户的序号始终是 1-based。
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Generic, Iterator, TypeVar


T = TypeVar("T")


class PromptQueue(Generic[T]):
    def __init__(self) -> None:
        self._items: Deque[T] = deque()

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __iter__(self) -> Iterator[T]:
        return iter(self._items)

    @property
    def entries(self) -> tuple[T, ...]:
        return tuple(self._items)

    def append(self, item: T) -> int:
        self._items.append(item)
        return len(self._items)

    def popleft(self) -> T:
        return self._items.popleft()

    def clear(self) -> None:
        self._items.clear()

    def _position(self, number: int) -> int:
        index = number - 1
        if index < 0 or index >= len(self._items):
            raise IndexError(f"队列中没有第 {number} 条消息")
        return index

    def delete(self, number: int) -> T:
        index = self._position(number)
        item = self._items[index]
        del self._items[index]
        return item

    def move(self, source: int, target: int) -> None:
        source_index = self._position(source)
        target_index = self._position(target)
        item = self._items[source_index]
        del self._items[source_index]
        self._items.insert(target_index, item)
