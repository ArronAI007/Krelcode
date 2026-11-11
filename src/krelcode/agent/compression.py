"""上下文压缩：预算超限时把中段历史摘要化，保住头尾。

策略（对齐主流 compact 做法）：
    [system] [中段 → LLM 摘要成一条消息] [最近 keep_recent 条原样保留]

边界安全：切点绝不落在 assistant(tool_calls) 与其 tool 结果之间——
若保留区以 tool 消息开头，把切点前移到配对完整处，否则 provider 会拒绝请求。

失败安全：摘要 LLM 调用出错时退化为「丢弃中段 + 占位说明」，
宁可丢上下文也不能让主循环崩掉（错误恢复原则）。
"""

from __future__ import annotations

from typing import List, Optional

from ..core.messages import Message
from ..llm.base import LLMProvider, collect
from .tokens import estimate_history_tokens

SUMMARY_SYSTEM_PROMPT = (
    "You are a conversation summarizer for a coding agent. "
    "Summarize the conversation so far in dense bullet points, preserving: "
    "the user's goal, decisions made, files created/modified (with paths), "
    "commands run and their key results, errors encountered and fixes, "
    "and anything needed to continue the work. Reply in the conversation's language."
)

SUMMARY_PREFIX = "[上下文摘要] "
DEFAULT_MAX_TOKENS = 100_000
DEFAULT_KEEP_RECENT = 10
MIN_MESSAGES_TO_COMPRESS = 6  # 太短的历史压缩无意义（防抖动/死循环）
MAX_MESSAGE_RENDER_CHARS = 2_000


class ContextManager:
    def __init__(
        self,
        llm: LLMProvider,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        keep_recent: int = DEFAULT_KEEP_RECENT,
    ) -> None:
        self.llm = llm
        self.max_tokens = max_tokens
        self.keep_recent = max(2, keep_recent)

    def over_budget(self, history: List[Message]) -> bool:
        return estimate_history_tokens(history) > self.max_tokens

    async def maybe_compress(self, history: List[Message], force: bool = False) -> bool:
        """超预算（或 force=True，即 REPL /compact）则原地压缩；返回是否压缩。"""
        if not force and not self.over_budget(history):
            return False

        head_count = 1 if history and history[0].role == "system" else 0
        if len(history) - head_count < MIN_MESSAGES_TO_COMPRESS:
            return False

        cut = max(head_count, len(history) - self.keep_recent)
        cut = _safe_cut(history, cut, head_count)
        if cut <= head_count:
            return False  # 没有可压缩的中段

        middle = history[head_count:cut]
        summary_text = await self._summarize(middle)
        summary_message = Message.user(SUMMARY_PREFIX + summary_text)
        history[head_count:cut] = [summary_message]
        return True

    async def _summarize(self, middle: List[Message]) -> str:
        rendered = render_messages(middle)
        try:
            finished = await collect(
                self.llm,
                [
                    Message.system(SUMMARY_SYSTEM_PROMPT),
                    Message.user(rendered),
                ],
            )
            text = (finished.message.content or "").strip()
            if text:
                return text
        except Exception:
            pass  # 失败安全：退化为占位摘要
        return (
            f"[摘要失败，已丢弃 {len(middle)} 条较早消息。"
            "如需其中细节，请重新读取相关文件。]"
        )


def _safe_cut(history: List[Message], cut: int, head_count: int) -> int:
    """把切点前移，直到保留区不以孤儿 tool 消息开头。"""
    while cut > head_count and cut < len(history) and history[cut].role == "tool":
        cut += 1
    # 若切点前一条是带 tool_calls 的 assistant，其结果在保留区外会被摘要——
    # 该 assistant 消息本身在中段（被摘要），配对完整，无需处理。
    return cut


def render_messages(messages: List[Message], max_chars: int = MAX_MESSAGE_RENDER_CHARS) -> str:
    """把中段历史渲染成给摘要 LLM 的紧凑文本。"""
    lines: List[str] = []
    for m in messages:
        parts = [f"[{m.role}]"]
        if m.content:
            content = m.content
            if len(content) > max_chars:
                content = content[:max_chars] + f"…[截断，原长 {len(m.content)}]"
            parts.append(content)
        if m.tool_calls:
            calls = ", ".join(f"{tc.name}({_short_args(tc)})" for tc in m.tool_calls)
            parts.append(f"调用工具: {calls}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def _short_args(tc, limit: int = 200) -> str:
    import json
    try:
        raw = json.dumps(tc.arguments, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        raw = repr(tc.arguments)
    return raw if len(raw) <= limit else raw[:limit] + "…"
