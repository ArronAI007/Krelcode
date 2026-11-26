"""上下文压缩：确定性微压缩 + 可验证的结构化交接摘要。

策略（对齐主流 compact 做法）：
1. **Tool Microcompact**：由独立 ``ToolMicrocompactor`` 保护最近/当前轮/
   错误/写编辑/计划结果，把较旧大输出分级替换为结构化摘要或单行引用
   （消息本身保留 → tool_call/result 配对永远完整；不调 LLM、幂等，
   通常足以解决问题——工具输出是上下文膨胀主因）
2. **Structured Compact v2**：仍超预算时，在 turn 边界把“已有摘要 +
   已完成 turns”合并为一条机器可校验的 CompactSummary；当前 turn 原样保留。

边界安全：切点绝不落在 assistant(tool_calls) 与其 tool 结果之间——
若保留区以 tool 消息开头，把切点前移到配对完整处，否则 provider 会拒绝请求。

失败安全：摘要 LLM 调用或 Schema 校验失败时，从 Working Context 与结构化
工具元数据生成降级摘要；已有摘要始终参与合并，避免信息逐轮衰减。
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from krelcode.core.messages import Message, ToolCall
from krelcode.core.redact import SecretRedactor, redact_obvious_secrets
from krelcode.llm.base import LLMProvider, collect
from krelcode.agent.value_utils import short_json
from krelcode.agent.context.compact_summary import (
    CompactFile,
    CompactSummary,
    CompactSummaryError,
    CompactTestStatus,
    SUMMARY_PREFIX,
    build_history_fallback,
    build_working_context_fallback,
    is_compact_summary_message,
    merge_summaries,
    summary_from_history,
)
from krelcode.core.tokens import (
    estimate_history_tokens,
    estimate_message_tokens,
)
from krelcode.agent.prompt.prompt_surface_contract import (
    COMPACT_SURFACE_CONTRACT,
    PromptSurfaceContract,
    PromptSurfaceExecutor,
    PromptSurfaceResult,
    observe_surface_result,
)
from krelcode.agent.context.tool_microcompact import (
    DEFAULT_KEEP_RECENT_TOOLS as MICROCOMPACT_KEEP_RECENT_TOOLS,
    MicrocompactReport,
    ToolMicrocompactor,
)

SUMMARY_SYSTEM_PROMPT = """\
You are the structured compact engine for a coding agent. Return exactly one
JSON object, with no markdown prose, matching this schema:
{
  "goal": "current user goal",
  "completed": ["work that is already finished and should not be repeated"],
  "decisions": ["durable decisions"],
  "files": [{"path": "relative/path", "status": "created|modified|read|touched", "note": "why it matters"}],
  "tests": [{"name": "test command or check", "status": "passed|failed|skipped|unknown", "detail": "exit/result detail"}],
  "open_issues": ["unfinished work or blockers"],
  "next_steps": ["concrete next actions"],
  "critical_constraints": ["requirements, boundaries, or risks that must not be violated"]
}
Merge the previous summary with the new completed turns. Never drop an old
completed item, constraint, decision, file, test status, goal, or open issue
unless the new evidence explicitly resolves or supersedes it. Keep paths and
test status exact. Reply in the conversation's language. All fields are
required; use empty arrays when unknown. Do not add version or any other field.
"""

SUMMARY_PREFIX = "[上下文摘要] "
DEFAULT_MAX_TOKENS = 100_000
DEFAULT_KEEP_RECENT = 10
DEFAULT_KEEP_RECENT_TOKENS = 0
DEFAULT_LOW_WATERMARK_RATIO = 0.60

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
    turn_aligned: bool = False

    def summary_line(self) -> str:
        head = self.head_end
        compact = max(0, self.middle_end - self.middle_start)
        keep = max(0, self.tail_end - self.tail_start)
        safe = " · 已回避孤立 tool 边界" if self.safe_adjusted else ""
        aligned = " · turn 对齐" if self.turn_aligned else ""
        return f"边界: head={head} · compact={compact} · keep={keep}{aligned}{safe}"


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
    high_watermark_tokens: int = 0
    low_watermark_tokens: int = 0
    source_savings: Tuple[Tuple[str, int], ...] = ()

    @property
    def low_watermark_reached(self) -> bool:
        return bool(
            self.low_watermark_tokens > 0
            and self.after_tokens <= self.low_watermark_tokens)

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
            f"watermarks: high {self.high_watermark_tokens}"
            f" → low {self.low_watermark_tokens} tokens"
            f" · target {'reached' if self.low_watermark_reached else 'not reached'}",
        ]
        if self.reason:
            lines.append(f"reason: {self.reason}")
        if self.boundary is not None:
            lines.append(self.boundary.summary_line())
        if self.source_savings:
            savings = " · ".join(
                f"{source}={tokens} tokens"
                for source, tokens in self.source_savings)
            lines.append(f"source savings: {savings}")
        if include_messages:
            body = self.preview_text
            if len(body) > max_chars:
                body = body[:max_chars] + "\n…[compact preview truncated]"
            if body:
                lines.extend(["--- messages to compact ---", body])
        return "\n".join(lines)


@dataclass(frozen=True)
class CompactResult:
    """Detailed, content-free result of one successful summary step."""

    trigger: str
    summary: CompactSummary
    boundary: CompactBoundary
    before_tokens: int
    after_tokens: int
    source_savings: Tuple[Tuple[str, int], ...]
    previous_generation: int = 0
    high_watermark_tokens: int = 0
    low_watermark_tokens: int = 0

    @property
    def generation(self) -> int:
        return self.summary.generation

    @property
    def summary_source(self) -> str:
        return self.summary.source

    def savings_dict(self) -> Dict[str, int]:
        return dict(self.source_savings)


class ContextManager:
    def __init__(
        self,
        llm: LLMProvider,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        keep_recent: int = DEFAULT_KEEP_RECENT,
        keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS,
        keep_recent_tools: int = DEFAULT_KEEP_RECENT_TOOLS,
        microcompactor: Optional[ToolMicrocompactor] = None,
        *,
        low_watermark_ratio: float = DEFAULT_LOW_WATERMARK_RATIO,
        working_context_provider: Optional[Callable[[], object]] = None,
        redactor: Optional[SecretRedactor] = None,
        surface_contract: PromptSurfaceContract = COMPACT_SURFACE_CONTRACT,
        prompt_surface_registry: Optional[object] = None,
    ) -> None:
        self.llm = llm
        self.max_tokens = max_tokens
        self.keep_recent = max(2, keep_recent)
        self.keep_recent_tokens = max(0, int(keep_recent_tokens))
        self.keep_recent_tools = max(1, keep_recent_tools)
        self.low_watermark_ratio = min(
            0.95, max(0.1, float(low_watermark_ratio)))
        self.working_context_provider = working_context_provider
        self.redactor = redactor
        self.surface_contract = surface_contract
        self.prompt_surface_registry = prompt_surface_registry
        self.last_surface_result: Optional[PromptSurfaceResult] = None
        self.microcompactor = microcompactor or ToolMicrocompactor(
            keep_recent=self.keep_recent_tools,
            min_compact_chars=MIN_EVICT_CHARS,
        )
        self.last_compact_preview: Optional[CompactPreview] = None
        self.last_microcompact_report: Optional[MicrocompactReport] = None
        self.last_compact_result: Optional[CompactResult] = None
        self.last_summary: Optional[CompactSummary] = None
        self.last_summary_source = ""
        self.last_generation = 0

    @property
    def high_watermark_tokens(self) -> int:
        return max(1, int(self.max_tokens))

    @property
    def low_watermark_tokens(self) -> int:
        return max(1, int(self.high_watermark_tokens * self.low_watermark_ratio))

    def rebuild_state(self, history: Sequence[Message]) -> Optional[CompactSummary]:
        """Synchronize in-memory summary state after resume/fork/rewind."""
        summary = summary_from_history(history)
        self.last_summary = summary
        self.last_summary_source = summary.source if summary else ""
        self.last_generation = summary.generation if summary else 0
        self.last_compact_result = None
        return summary

    def restore_summary(self, summary: object) -> None:
        """Install a persisted compact summary without rebuilding history."""
        if not isinstance(summary, CompactSummary):
            raise TypeError("summary must be a CompactSummary")
        self.last_summary = summary
        self.last_summary_source = summary.source
        self.last_generation = summary.generation
        self.last_compact_result = None

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
        microcompact: bool = True,
    ) -> CompactPreview:
        """Preview source-level savings without mutating authoritative history."""
        before = estimate_history_tokens(history)
        work = deepcopy(history)
        evicted = 0
        if microcompact:
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
                strategy="evict_tools" if evicted else "none",
                high_watermark_tokens=self.high_watermark_tokens,
                low_watermark_tokens=self.low_watermark_tokens,
                source_savings=_source_savings(history, work),
            )
            self.last_compact_preview = preview
            return preview
        if len(work) - head_count < MIN_MESSAGES_TO_COMPRESS:
            preview = CompactPreview(
                force=force, eligible=False,
                reason=(f"可压缩消息不足（需要至少 {MIN_MESSAGES_TO_COMPRESS} 条），"
                        "工具结果微压缩可能已生效"),
                before_tokens=before, after_eviction_tokens=after_eviction,
                after_tokens=after_eviction, evicted_tools=evicted,
                strategy="evict_tools" if evicted else "none",
                high_watermark_tokens=self.high_watermark_tokens,
                low_watermark_tokens=self.low_watermark_tokens,
                source_savings=_source_savings(history, work),
            )
            self.last_compact_preview = preview
            return preview

        low_target = max(
            0, self.low_watermark_tokens - max(0, int(extra_tokens)))
        cut, turn_aligned, reason = _choose_compact_cut(
            work,
            head_count,
            self.keep_recent,
            force=force,
            low_watermark_tokens=low_target,
            current_turn_id=current_turn_id,
            require_new_content=not force,
            keep_recent_tokens=self.keep_recent_tokens,
        )
        middle = work[head_count:cut] if cut is not None else []
        has_new_content = any(
            not is_compact_summary_message(message.content)
            for message in middle)
        if cut is None or (not has_new_content and not force):
            reason = reason or (
                "当前 turn 尚无新的已完成内容；等待下一个 turn 边界，"
                "避免同类工具结果反复触发摘要")
            preview = CompactPreview(
                force=force, eligible=False, reason=reason,
                before_tokens=before, after_eviction_tokens=after_eviction,
                after_tokens=after_eviction, evicted_tools=evicted,
                strategy="evict_tools" if evicted else "none",
                high_watermark_tokens=self.high_watermark_tokens,
                low_watermark_tokens=self.low_watermark_tokens,
                source_savings=_source_savings(history, work),
            )
            self.last_compact_preview = preview
            return preview
        if not has_new_content and force:
            requested = max(head_count, len(work) - self.keep_recent)
            forced_cut = _safe_cut(work, requested, head_count)
            if forced_cut > head_count:
                cut = forced_cut
                turn_aligned = False
        if cut is None or cut <= head_count:
            preview = CompactPreview(
                force=force, eligible=False,
                reason="没有可压缩的中段消息",
                before_tokens=before, after_eviction_tokens=after_eviction,
                after_tokens=after_eviction, evicted_tools=evicted,
                strategy="evict_tools" if evicted else "none",
                high_watermark_tokens=self.high_watermark_tokens,
                low_watermark_tokens=self.low_watermark_tokens,
                source_savings=_source_savings(history, work),
            )
            self.last_compact_preview = preview
            return preview

        boundary = CompactBoundary(
            head_end=head_count,
            middle_start=head_count,
            middle_end=cut,
            tail_start=cut,
            tail_end=len(work),
            safe_adjusted=(
                not turn_aligned
                and cut != max(head_count, len(work) - self.keep_recent)),
            turn_aligned=turn_aligned,
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
            high_watermark_tokens=self.high_watermark_tokens,
            low_watermark_tokens=self.low_watermark_tokens,
            source_savings=_source_savings(history, projected),
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
        focus: str = "",
    ) -> bool:
        """Apply microcompaction and, at a turn boundary, Structured Compact v2.

        Auto compaction is hysteretic: the high watermark is the configured
        request budget, and the compaction target is the low watermark.  When
        only the existing summary and the active turn remain, auto mode waits
        for a new completed turn instead of repeatedly summarizing the same
        tool results.
        """
        preview = self.preview_compact(
            history, force=force, extra_tokens=extra_tokens,
            output_reserve_tokens=output_reserve_tokens,
            model_context_window=model_context_window,
            current_turn_id=current_turn_id,
            microcompact=microcompact)
        if not force and not self.over_budget(
            history, extra_tokens=extra_tokens,
            output_reserve_tokens=output_reserve_tokens,
            model_context_window=model_context_window,
        ):
            return False

        before_eviction_tokens = estimate_history_tokens(history)
        before_eviction_tool_tokens = _tool_result_tokens(history)
        evicted = (
            self.evict_old_tool_results(
                history, current_turn_id=current_turn_id)
            if microcompact else 0
        )
        eviction_savings = _source_savings_from_counts(
            before_tokens=before_eviction_tokens,
            after_tokens=estimate_history_tokens(history),
            before_tool_tokens=before_eviction_tool_tokens,
            after_tool_tokens=_tool_result_tokens(history),
        )
        if not force and not self.over_budget(
            history, extra_tokens=extra_tokens,
            output_reserve_tokens=output_reserve_tokens,
            model_context_window=model_context_window,
        ):
            self.last_compact_preview = replace(
                preview, after_tokens=estimate_history_tokens(history),
                strategy="evict_tools" if evicted else preview.strategy,
                boundary=None, preview_text="",
                source_savings=eviction_savings,
            )
            return evicted > 0

        if not preview.eligible or preview.boundary is None:
            self.last_compact_preview = replace(
                preview,
                after_tokens=estimate_history_tokens(history),
                evicted_tools=evicted,
                strategy="evict_tools" if evicted else preview.strategy,
                source_savings=eviction_savings,
            )
            return evicted > 0

        head_count = preview.boundary.head_end
        cut = preview.boundary.middle_end
        middle = history[head_count:cut]
        previous = summary_from_history(middle)
        compacted_messages = (
            (previous.compacted_messages if previous is not None else 0)
            + sum(
                1 for message in middle
                if not is_compact_summary_message(message.content))
        )
        before_tokens = estimate_history_tokens(history)
        before_tool_tokens = _tool_result_tokens(history)
        summary = await self._summarize(
            middle,
            previous=previous,
            compacted_messages=compacted_messages,
            focus=focus,
        )
        history[head_count:cut] = [Message(
            role="user",
            content=summary.render(),
            provider_meta={
                "compact_source": summary.source,
                "compact_generation": summary.generation,
                "compact_messages": summary.compacted_messages,
            },
        )]
        after_tokens = estimate_history_tokens(history)
        savings = _source_savings_from_counts(
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            before_tool_tokens=before_tool_tokens,
            after_tool_tokens=_tool_result_tokens(history),
        )
        boundary = preview.boundary
        self.last_summary = summary
        self.last_summary_source = summary.source
        self.last_generation = summary.generation
        self.last_compact_result = CompactResult(
            trigger="force" if force else "auto",
            summary=summary,
            boundary=boundary,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            source_savings=savings,
            previous_generation=previous.generation if previous else 0,
            high_watermark_tokens=self.high_watermark_tokens,
            low_watermark_tokens=self.low_watermark_tokens,
        )
        self.last_compact_preview = replace(
            preview,
            boundary=boundary,
            after_tokens=after_tokens,
            evicted_tools=evicted,
            strategy="summarize",
            source_savings=savings,
        )
        return True

    async def _summarize(
        self,
        middle: List[Message],
        *,
        previous: Optional[CompactSummary],
        compacted_messages: int,
        focus: str = "",
    ) -> CompactSummary:
        rendered = render_messages(middle)
        snapshot = None
        if self.working_context_provider is not None:
            try:
                snapshot = self.working_context_provider()
            except Exception:
                snapshot = None
        previous_json = json.dumps(
            previous.as_contract_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":")) if previous else "{}"
        working_text = str(getattr(snapshot, "text", "") or "")
        prompt = (
            "<previous_summary>" + previous_json + "</previous_summary>\n"
            "<working_context>" + working_text + "</working_context>\n"
            + (f"<focus>{str(focus).strip()}</focus>\n" if str(focus).strip() else "")
            + "<conversation_to_compact>\n" + rendered
            + "\n</conversation_to_compact>"
        )
        async def run_surface(safe_prompt: object) -> str:
            finished = await collect(
                self.llm,
                [
                    Message.system(SUMMARY_SYSTEM_PROMPT),
                    Message.user(str(safe_prompt)),
                ],
            )
            return str(finished.message.content or "")

        def observe(_surface_id: str, result: PromptSurfaceResult) -> None:
            observe_surface_result(self.prompt_surface_registry, result)

        surface_result = await PromptSurfaceExecutor().execute(
            "aux:compact",
            self.surface_contract,
            prompt,
            run_surface,
            redactor=self.redactor,
            observer=observe,
        )
        self.last_surface_result = surface_result
        model_summary: Optional[CompactSummary] = None
        raw_text = surface_result.text.strip()
        if surface_result.ok and raw_text:
            try:
                model_summary = CompactSummary.from_model_json_text(raw_text)
            except (Exception, CompactSummaryError):
                model_summary = None
        if model_summary is None and not raw_text:
            raw_text = (
                f"摘要失败（{surface_result.status}），"
                "已使用结构化现场生成降级摘要。"
            )

        working_fallback = build_working_context_fallback(
            snapshot,
            previous=previous,
            raw_model_text="" if model_summary else raw_text,
            compacted_messages=compacted_messages,
        )
        history_fallback = build_history_fallback(
            middle,
            previous=previous,
            raw_model_text="" if model_summary else raw_text,
            compacted_messages=compacted_messages,
        )
        merged = merge_summaries(
            previous, working_fallback, history_fallback, model_summary)
        generation = (previous.generation if previous else 0) + 1
        source = (
            "model" if model_summary is not None
            else "working_context" if snapshot is not None
            else "history_fallback")
        merged = replace(
            merged,
            generation=generation,
            source=source,
            compacted_messages=compacted_messages,
            created_at=time.time(),
        )
        merged = _redact_compact_summary(
            merged,
            self.redactor.redact if self.redactor is not None else None,
        )
        return merged


def _redact_compact_summary(
    summary: CompactSummary,
    redact: Optional[Callable[[str], str]] = None,
) -> CompactSummary:
    """Redact every persisted summary field without parsing rendered JSON."""

    def clean(value: object) -> str:
        text = redact_obvious_secrets(str(value or ""))
        return redact(text) if redact is not None else text

    return replace(
        summary,
        goal=clean(summary.goal),
        completed=tuple(clean(value) for value in summary.completed),
        decisions=tuple(clean(value) for value in summary.decisions),
        files=tuple(
            CompactFile(
                path=clean(item.path),
                status=item.status,
                note=clean(item.note),
            )
            for item in summary.files
        ),
        commands=tuple(clean(value) for value in summary.commands),
        tests=tuple(
            CompactTestStatus(
                name=clean(item.name),
                status=item.status,
                detail=clean(item.detail),
            )
            for item in summary.tests
        ),
        errors=tuple(clean(value) for value in summary.errors),
        open_issues=tuple(clean(value) for value in summary.open_issues),
        next_steps=tuple(clean(value) for value in summary.next_steps),
        critical_constraints=tuple(
            clean(value) for value in summary.critical_constraints),
        handoff_notes=tuple(clean(value) for value in summary.handoff_notes),
    )


def _choose_compact_cut(
    history: List[Message],
    head_count: int,
    keep_recent: int,
    *,
    force: bool,
    low_watermark_tokens: int = 0,
    current_turn_id: str = "",
    require_new_content: bool = False,
    keep_recent_tokens: int = 0,
) -> Tuple[Optional[int], bool, str]:
    """Choose a safe completed-turn boundary, targeting the low watermark.

    The active turn is never included.  At minimum the cut starts at that
    turn's user message; when tool metadata lets us identify the active turn
    precisely, steering messages inside the turn cannot expose its prefix.
    """
    if len(history) <= head_count:
        return None, False, "没有可压缩消息"
    requested = max(head_count, len(history) - max(2, keep_recent))
    if keep_recent_tokens > 0:
        tail_start = len(history)
        while (
            tail_start > head_count
            and estimate_history_tokens(history[tail_start - 1:])
            <= keep_recent_tokens
        ):
            tail_start -= 1
        requested = min(requested, max(head_count, tail_start))
    active_start = _active_turn_start(history, current_turn_id, head_count)
    boundaries = [
        index for index in range(head_count + 1, active_start + 1)
        if history[index].role == "user"
        and not is_compact_summary_message(history[index].content)
        and (keep_recent_tokens <= 0 or index <= requested)
    ]
    if require_new_content:
        boundaries = [
            index for index in boundaries
            if any(
                not is_compact_summary_message(message.content)
                for message in history[head_count:index])
        ]
    if boundaries:
        preferred = min(
            boundaries, key=lambda index: (abs(index - requested), -index))
        cut = preferred
        if low_watermark_tokens > 0:
            projected = _projected_compact_tokens(
                history, head_count, preferred)
            if projected > low_watermark_tokens:
                reachable = [
                    index for index in boundaries
                    if _projected_compact_tokens(history, head_count, index)
                    <= low_watermark_tokens
                ]
                cut = max(reachable) if reachable else max(boundaries)
        return cut, True, ""
    if force:
        cut = _safe_cut(history, requested, head_count)
        if cut > head_count:
            return cut, False, ""
    if require_new_content:
        return None, False, (
            "当前 turn 尚无新的已完成内容；等待下一个 turn 边界，"
            "避免同类工具结果反复触发摘要")
    return None, False, "没有安全的 turn 边界可供摘要"


def _active_turn_start(
    history: Sequence[Message], current_turn_id: str, head_count: int,
) -> int:
    """Return the latest legal cut index, preserving the whole active turn."""
    last_user = len(history) - 1
    for index in range(len(history) - 1, head_count, -1):
        if (
            history[index].role == "user"
            and not is_compact_summary_message(history[index].content)
        ):
            last_user = index
            break
    if not current_turn_id:
        return last_user
    for index in range(head_count, len(history)):
        message = history[index]
        meta = message.tool_meta or {}
        if str(meta.get("turn_id", "")) != current_turn_id:
            continue
        for candidate in range(index, head_count, -1):
            prior = history[candidate]
            if (
                prior.role == "user"
                and not is_compact_summary_message(prior.content)
            ):
                return candidate
        break
    return last_user


def _projected_compact_tokens(
    history: Sequence[Message], head_count: int, cut: int,
) -> int:
    projected = (
        list(history[:head_count])
        + [Message.user(SUMMARY_PREFIX + "[待生成摘要]")]
        + list(history[cut:])
    )
    return estimate_history_tokens(projected)


def _tool_result_tokens(messages: Sequence[Message]) -> int:
    return sum(
        estimate_message_tokens(message)
        for message in messages if message.role == "tool")


def _source_savings(
    before: Sequence[Message], after: Sequence[Message],
) -> Tuple[Tuple[str, int], ...]:
    before_tokens = estimate_history_tokens(list(before))
    after_tokens = estimate_history_tokens(list(after))
    return _source_savings_from_counts(
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        before_tool_tokens=_tool_result_tokens(before),
        after_tool_tokens=_tool_result_tokens(after),
    )


def _source_savings_from_counts(
    *,
    before_tokens: int,
    after_tokens: int,
    before_tool_tokens: int,
    after_tool_tokens: int,
) -> Tuple[Tuple[str, int], ...]:
    tool_saved = max(0, before_tool_tokens - after_tool_tokens)
    conversation_before = max(0, before_tokens - before_tool_tokens)
    conversation_after = max(0, after_tokens - after_tool_tokens)
    conversation_saved = max(0, conversation_before - conversation_after)
    return (
        ("tool_results", tool_saved),
        ("conversation", conversation_saved),
        ("total", max(0, before_tokens - after_tokens)),
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
            calls = ", ".join(
                f"{tc.name}({short_json(tc.arguments)})" for tc in m.tool_calls)
            parts.append(f"调用工具: {calls}")
        lines.append(" ".join(parts))
    return "\n".join(lines)
