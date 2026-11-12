"""token 估算（零依赖启发式）。

不引入 tiktoken：对上下文预算管理来说，±20% 的精度足够触发压缩决策。
启发式：CJK 字符 ≈ 1 token/字；其他文本 ≈ 1 token/4 字符（GPT/Claude 的
BPE 对英文代码的经验比例）。
"""

from __future__ import annotations

import json
from typing import Iterable

from ..core.messages import Message

CHARS_PER_TOKEN = 4


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF      # 汉字
        or 0x3400 <= cp <= 0x4DBF   # 扩展A
        or 0x3040 <= cp <= 0x30FF   # 日文假名
        or 0xAC00 <= cp <= 0xD7AF   # 韩文
        or 0xFF00 <= cp <= 0xFFEF   # 全角
    )


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if is_cjk(ch))
    other = len(text) - cjk
    return cjk + (other + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def estimate_message_tokens(message: Message) -> int:
    total = 4  # 每条消息的结构性开销（role/分隔符）
    total += estimate_text_tokens(message.content or "")
    if message.tool_calls:
        try:
            raw = json.dumps(
                [(tc.name, tc.arguments) for tc in message.tool_calls],
                ensure_ascii=False,
            )
        except (TypeError, ValueError):
            raw = repr(message.tool_calls)
        total += estimate_text_tokens(raw)
    return total


def estimate_history_tokens(history: Iterable[Message]) -> int:
    return sum(estimate_message_tokens(m) for m in history)
