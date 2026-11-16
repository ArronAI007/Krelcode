"""上下文压缩：两道防线——先做确定性 Tool Microcompact，仍超预算再 LLM 摘要。

策略（对齐主流 compact 做法）：
1. **Tool Microcompact**：由独立 ``ToolMicrocompactor`` 保护最近/当前轮/
   错误/写编辑/计划结果，把较旧大输出分级替换为结构化摘要或单行引用
   （消息本身保留 → tool_call/result 配对永远完整；不调 LLM、幂等，
   通常足以解决问题——工具输出是上下文膨胀主因）
2. **LLM 摘要**：仍超预算时，把中段历史摘要成一条消息：
    [system] [中段 → LLM 摘要] [最近 keep_recent 条原样保留]

边界安全：切点绝不落在 assistant(tool_calls) 与其 tool 结果之间——
若保留区以 tool 消息开头，把切点前移到配对完整处，否则 provider 会拒绝请求。

失败安全：摘要 LLM 调用出错时退化为「丢弃中段 + 占位说明」，
宁可丢上下文也不能让主循环崩掉（错误恢复原则）。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import List, Optional

from ..core.messages import Message
from ..llm.base import LLMProvider, collect
from .tokens import estimate_history_tokens, estimate_text_tokens
from .tool_microcompact import (
    DEFAULT_KEEP_RECENT_TOOLS as MICROCOMPACT_KEEP_RECENT_TOOLS,
    MicrocompactReport,
    ToolMicrocompactor,
)

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
DEFAULT_KEEP_RECENT_TOOLS = MICROCOMPACT_KEEP_RECENT_TOOLS
MIN_EVICT_CHARS = 200              # 兼容旧调用：短结果驱逐无收益
EVICTION_MARKER = "[旧工具输出已驱逐：原 {n} 字符；如需细节请重新执行工具或 read_file]"


@dataclass(frozen=True)
class CompactBoundary:
    """一次 compact 的 history 零基索引边界。"""

    head_end: int
    middle_start: int
    middle_end: int
    tail_start: int
    tail_end: int
    safe_adjusted: bool = False

    def summary_line(self) -> str:
        head = self.head_end
        compact = max(0, self.middle_end - self.middle_start)
        keep = max(0, self.tail_end - self.tail_start)
        safe = " · 已回避孤立 tool 边界" if self.safe_adjusted else ""
        return f"边界: head={head} · compact={compact} · keep={keep}{safe}"


@dataclass(frozen=True)
class CompactPreview:
    force: bool
    eligible: bool
    reason: str
    before_tokens: int
    after_eviction_tokens: int
    after_tokens: int
    evicted_tools: int
    strategy: str
    boundary: Optional[CompactBoundary] = None
    preview_text: str = ""

    def render(
        self,
        *,
        max_chars: int = 8_000,
        include_messages: bool = True,
    ) -> str:
        lines = [
            f"compact preview: {'force' if self.force else 'auto'}"
            f" · before {self.before_tokens} → estimated {self.after_tokens} tokens",
            f"eligible: {'yes' if self.eligible else 'no'} · strategy: {self.strategy}"
            f" · evicted tools: {self.evicted_tools}",
        ]
        if self.reason:
            lines.append(f"reason: {self.reason}")
        if self.boundary is not None:
            lines.append(self.boundary.summary_line())
        if include_messages:
            body = self.preview_text
            if len(body) > max_chars:
                body = body[:max_chars] + "\n…[compact preview truncated]"
            if body:
                lines.extend(["--- messages to compact ---", body])
        return "\n".join(lines)


class ContextManager:
    def __init__(
        self,
        llm: LLMProvider,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        keep_recent_tools: int = DEFAULT_KEEP_RECENT_TOOLS,
        microcompactor: Optional[ToolMicrocompactor] = None,
    ) -> None:
        self.llm = llm
        self.max_tokens = max_tokens
        self.keep_recent = max(2, keep_recent)
        self.keep_recent_tools = max(1, keep_recent_tools)
        self.microcompactor = microcompactor or ToolMicrocompactor(
            keep_recent=self.keep_recent_tools,
            min_compact_chars=MIN_EVICT_CHARS,
        )
        self.last_compact_preview: Optional[CompactPreview] = None
        self.last_microcompact_report: Optional[MicrocompactReport] = None

    def over_budget(
        self,
        history: List[Message],
        *,
        extra_tokens: int = 0,
        output_reserve_tokens: int = 0,
        model_context_window: Optional[int] = None,
    ) -> bool:
        """Check the full request budget, not just serialized history.

        ``extra_tokens`` covers assembled system context, tool schemas and
        provider wire overhead. The model-window constraint additionally
        reserves the provider's real output limit.
        """
        request_tokens = estimate_history_tokens(history) + max(0, extra_tokens)
        return self._exceeds_constraints(
            request_tokens,
            output_reserve_tokens=output_reserve_tokens,
            model_context_window=model_context_window,
        )

    def _exceeds_constraints(
        self,
        request_tokens: int,
        *,
        output_reserve_tokens: int = 0,
        model_context_window: Optional[int] = None,
    ) -> bool:
        if request_tokens > self.max_tokens:
            return True
        return bool(
            model_context_window is not None
            and request_tokens + max(0, output_reserve_tokens)
            > model_context_window
        )

    def evict_old_tool_results(
        self, history: List[Message], *, current_turn_id: str = "",
    ) -> int:
        """Deterministic tool microcompaction used by the legacy API.

        The actual policy lives in ``ToolMicrocompactor``: recent, error,
        write/edit and plan/user-input results are protected, while older
        large outputs become bounded structured summaries or references.
        Messages are never removed, so tool-call/result pairing is preserved.
        """
        report = self.microcompactor.compact(
            history, trigger="compact", current_turn_id=current_turn_id)
        self.last_microcompact_report = report
        return report.compacted_count

    def preview_compact(
        self,
        history: List[Message],
        force: bool = True,
        *,
        extra_tokens: int = 0,
        output_reserve_tokens: int = 0,
        model_context_window: Optional[int] = None,
        current_turn_id: str = "",
    ) -> CompactPreview:
        """非破坏性 preview：展示驱逐/摘要边界和待压缩消息。"""
        before = estimate_history_tokens(history)
        work = deepcopy(history)
        evicted = self.microcompactor.compact(
            work, trigger="preview",
            current_turn_id=current_turn_id,
        ).compacted_count
        after_eviction = estimate_history_tokens(work)
        head_count = 1 if work and work[0].role == "system" else 0
        if not force and not self._exceeds_constraints(
            after_eviction + max(0, extra_tokens),
            output_reserve_tokens=output_reserve_tokens,
            model_context_window=model_context_window,
        ):
            preview = CompactPreview(
                force=False, eligible=False,
                reason="历史在预算内，无需 compact",
                before_tokens=before, after_eviction_tokens=after_eviction,
                after_tokens=after_eviction, evicted_tools=evicted,
                strategy="none")
            self.last_compact_preview = preview
            return preview
        if len(work) - head_count < MIN_MESSAGES_TO_COMPRESS:
            preview = CompactPreview(
                force=force, eligible=False,
                reason=(f"可压缩消息不足（需要至少 {MIN_MESSAGES_TO_COMPRESS} 条），"
                        "工具结果驱逐可能已生效"),
                before_tokens=before, after_eviction_tokens=after_eviction,
                after_tokens=after_eviction, evicted_tools=evicted,
                strategy="evict_tools" if evicted else "none")
            self.last_compact_preview = preview
            return preview
        requested_cut = max(head_count, len(work) - self.keep_recent)
        cut = _safe_cut(work, requested_cut, head_count)
        if cut <= head_count:
            preview = CompactPreview(
                force=force, eligible=False,
                reason="没有可压缩的中段消息",
                before_tokens=before, after_eviction_tokens=after_eviction,
                after_tokens=after_eviction, evicted_tools=evicted,
                strategy="evict_tools" if evicted else "none")
            self.last_compact_preview = preview
            return preview
        boundary = CompactBoundary(
            head_end=head_count,
            middle_start=head_count,
            middle_end=cut,
            tail_start=cut,
            tail_end=len(work),
            safe_adjusted=cut != requested_cut,
        )
        middle = work[head_count:cut]
        projected = (
            work[:head_count]
            + [Message.user(SUMMARY_PREFIX + "[待生成摘要]")]
            + work[cut:]
        )
        preview = CompactPreview(
            force=force,
            eligible=True,
            reason="",
            before_tokens=before,
            after_eviction_tokens=after_eviction,
            after_tokens=estimate_history_tokens(projected),
            evicted_tools=evicted,
            strategy="summarize",
            boundary=boundary,
            preview_text=render_messages(middle),
        )
        self.last_compact_preview = preview
        return preview

    async def maybe_compress(
        self,
        history: List[Message],
        force: bool = False,
        *,
        extra_tokens: int = 0,
        output_reserve_tokens: int = 0,
        model_context_window: Optional[int] = None,
        microcompact: bool = True,
        current_turn_id: str = "",
    ) -> bool:
        """超预算（或 force=True，即 REPL /compact）则原地压缩；返回是否压缩。

        顺序：先做免费 Tool Microcompact，仍超预算（或 force）再 LLM 摘要。
        AgentLoop 的真实请求流程可先显式执行 microcompact，再以
        ``microcompact=False`` 只触发完整摘要，避免重复扫描。
        """
        if microcompact:
            preview = self.preview_compact(
                history, force=force, extra_tokens=extra_tokens,
                output_reserve_tokens=output_reserve_tokens,
                model_context_window=model_context_window,
                current_turn_id=current_turn_id)
        else:
            preview = CompactPreview(
                force=force, eligible=True, reason="",
                before_tokens=estimate_history_tokens(history),
                after_eviction_tokens=estimate_history_tokens(history),
                after_tokens=estimate_history_tokens(history),
                evicted_tools=0, strategy="summarize",
            )
        if not force and not self.over_budget(
            history, extra_tokens=extra_tokens,
            output_reserve_tokens=output_reserve_tokens,
            model_context_window=model_context_window,
        ):
            return False

        evicted = (
            self.evict_old_tool_results(
                history, current_turn_id=current_turn_id)
            if microcompact else 0
        )
        if not force and not self.over_budget(
            history, extra_tokens=extra_tokens,
            output_reserve_tokens=output_reserve_tokens,
            model_context_window=model_context_window,
        ):
            self.last_compact_preview = replace(
                preview, after_tokens=estimate_history_tokens(history),
                strategy="evict_tools" if evicted else preview.strategy,
                boundary=None, preview_text="")
            return evicted > 0     # 驱逐已解决，无需摘要

        head_count = 1 if history and history[0].role == "system" else 0
        if len(history) - head_count < MIN_MESSAGES_TO_COMPRESS:
            self.last_compact_preview = replace(
                preview, after_tokens=estimate_history_tokens(history),
                evicted_tools=evicted)
            return evicted > 0   # 摘要无料可压，但驱逐可能已生效

        cut = max(head_count, len(history) - self.keep_recent)
        cut = _safe_cut(history, cut, head_count)
        if cut <= head_count:
            self.last_compact_preview = replace(
                preview, after_tokens=estimate_history_tokens(history),
                evicted_tools=evicted)
            return evicted > 0   # 没有可压缩的中段，同上

        middle = history[head_count:cut]
        summary_text = await self._summarize(middle)
        summary_message = Message.user(SUMMARY_PREFIX + summary_text)
        history[head_count:cut] = [summary_message]
        self.last_compact_preview = replace(
            preview, after_tokens=estimate_history_tokens(history),
            evicted_tools=evicted, strategy="summarize")
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
