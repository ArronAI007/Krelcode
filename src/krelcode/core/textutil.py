"""通用文本工具（core 层，供 tools/agent 复用）。"""

from __future__ import annotations


def truncate_middle(text: str, max_chars: int) -> str:
    """头尾保留、中间截断（命令输出/大结果通用）。"""
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    tail = max_chars - head
    omitted = len(text) - head - tail
    return text[:head] + f"\n…[中间截断 {omitted} 字符]…\n" + text[-tail:]
