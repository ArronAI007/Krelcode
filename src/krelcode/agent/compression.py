"""上下文压缩：两道防线——先驱逐旧工具结果（免费、确定性），仍超预算再 LLM 摘要。

策略（对齐主流 compact 做法）：
1. **工具结果驱逐**：只保留最近 keep_recent_tools 条工具输出，更早的
   替换为占位标记（消息本身保留 → tool_call/result 配对永远完整；
   不调 LLM，零成本，通常足以解决问题——工具输出是上下文膨胀主因）
2. **LLM 摘要**：仍超预算时，把中段历史摘要成一条消息：
    [system] [中段 → LLM 摘要] [最近 keep_recent 条原样保留]

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

# 工具结果驱逐（第一道防线）
DEFAULT_KEEP_RECENT_TOOLS = 6      # 保留最近 N 条工具输出原文
MIN_EVICT_CHARS = 200              # 短结果驱逐无收益，跳过
EVICTION_MARKER = "[旧工具输出已驱逐：原 {n} 字符；如需细节请重新执行工具或 read_file]"


class ContextManager:
    def __init__(
        self,
        llm: LLMProvider,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        keep_recent_tools: int = DEFAULT_KEEP_RECENT_TOOLS,
    ) -> None:
        self.llm = llm
        self.max_tokens = max_tokens
        self.keep_recent = max(2, keep_recent)
        self.keep_recent_tools = max(1, keep_recent_tools)

    def over_budget(self, history: List[Message]) -> bool:
        return estimate_history_tokens(history) > self.max_tokens

    def evict_old_tool_results(self, history: List[Message]) -> int:
        """第一道防线：驱逐旧工具输出（原地替换 content，保留消息本身）。

        只动「最近 keep_recent_tools 条之前、且原文超过 MIN_EVICT_CHARS、
        且尚未被驱逐过」的 tool 消息；配对结构（assistant.tool_calls ↔ tool）
        完全不受影响。返回驱逐条数。
        """
        tool_indices = [
            i for i, m in enumerate(history)
            if m.role == "tool" and m.content
            and not m.content.startswith("[旧工具输出已驱逐")
        ]
        if len(tool_indices) <= self.keep_recent_tools:
            return 0
        evicted = 0
        for i in tool_indices[:-self.keep_recent_tools]:
            message = history[i]
            if len(message.content or "") < MIN_EVICT_CHARS:
                continue  # 太短，驱逐无收益
            message.content = EVICTION_MARKER.format(n=len(message.content))
            evicted += 1
        return evicted

    async def maybe_compress(self, history: List[Message], force: bool = False) -> bool:
        """超预算（或 force=True，即 REPL /compact）则原地压缩；返回是否压缩。

        顺序：先驱逐旧工具结果（免费），仍超预算（或 force）再 LLM 摘要。
        """
        if not force and not self.over_budget(history):
            return False

        evicted = self.evict_old_tool_results(history)
        if not force and not self.over_budget(history):
            return evicted > 0     # 驱逐已解决，无需摘要

        head_count = 1 if history and history[0].role == "system" else 0
        if len(history) - head_count < MIN_MESSAGES_TO_COMPRESS:
            return evicted > 0   # 摘要无料可压，但驱逐可能已生效

        cut = max(head_count, len(history) - self.keep_recent)
        cut = _safe_cut(history, cut, head_count)
        if cut <= head_count:
            return evicted > 0   # 没有可压缩的中段，同上

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
