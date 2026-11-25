"""Deterministic, provider-agnostic microcompaction of old tool results.

Tool output is usually the largest and least cacheable part of a coding-agent
history.  Before paying for an LLM summary, this module replaces old, large
tool results with a bounded structured summary or a one-line recovery
reference.  It never removes messages, so assistant-tool-call/tool-result
pairing stays intact, and it never changes the most recent or safety-critical
results.

Design constraints:
- zero LLM calls and zero I/O;
- no raw output larger than ``summary_chars`` is retained;
- errors, successful writes/edits, plan/user-input results are protected;
- full-output file references are preserved verbatim (bounded);
- repeated runs are idempotent.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from krelcode.core.messages import (
    ImageBlock,
    Message,
    ResourceBlock,
    StructuredBlock,
    TextBlock,
    ToolCall,
)
from krelcode.core.tokens import estimate_history_tokens

DEFAULT_KEEP_RECENT_TOOLS = 6
DEFAULT_MIN_COMPACT_CHARS = 200
DEFAULT_SUMMARY_CHARS = 240
COMPACTED_PREFIX = "[旧工具输出已驱逐"
RECOVERY_HINT = "如需细节请重新执行工具或 read_file"
PROTECTED_TOOLS = frozenset({
    "write_file", "edit_file", "ask_user", "request_user_input",
    "update_plan", "exit_plan_mode",
})

_PATH_REF_PATTERN = re.compile(
    r"(?:全文已存盘|full output|output file)\s*[:：]\s*([^\n\]]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ToolMicrocompactEntry:
    """One safe, content-free audit record for a compacted tool message."""

    message_index: int
    tool_call_id: str
    tool_name: str
    tier: str
    original_chars: int
    compacted_chars: int
    digest: str

    def as_record(self) -> Dict[str, Any]:
        return {
            "index": self.message_index,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tier": self.tier,
            "original_chars": self.original_chars,
            "compacted_chars": self.compacted_chars,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class MicrocompactReport:
    """Result of one microcompaction pass.

    ``before_tokens``/``after_tokens`` use the shared heuristic history
    estimator.  The event layer can safely expose all fields: entries contain
    only tool names, sizes and digests, never output text.
    """

    trigger: str = "manual"
    before_tokens: int = 0
    after_tokens: int = 0
    compacted_count: int = 0
    preserved_count: int = 0
    protected_count: int = 0
    small_result_count: int = 0
    recent_count: int = 0
    summarized_count: int = 0
    referenced_count: int = 0
    entries: Tuple[ToolMicrocompactEntry, ...] = ()

    @property
    def changed(self) -> bool:
        return self.compacted_count > 0

    @property
    def saved_tokens(self) -> int:
        return max(0, self.before_tokens - self.after_tokens)

    def as_record(self) -> Dict[str, Any]:
        return {
            "trigger": self.trigger,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "saved_tokens": self.saved_tokens,
            "compacted_count": self.compacted_count,
            "preserved_count": self.preserved_count,
            "protected_count": self.protected_count,
            "small_result_count": self.small_result_count,
            "recent_count": self.recent_count,
            "summarized_count": self.summarized_count,
            "referenced_count": self.referenced_count,
            "entries": [entry.as_record() for entry in self.entries],
        }


# Naming aliases for integrations using either term.
ToolMicrocompactResult = MicrocompactReport
ToolCompactReport = MicrocompactReport


class ToolMicrocompactor:
    """Replace old large tool results with bounded summaries/references."""

    def __init__(
        self,
        *,
        keep_recent: int = DEFAULT_KEEP_RECENT_TOOLS,
        min_compact_chars: int = DEFAULT_MIN_COMPACT_CHARS,
        summary_chars: int = DEFAULT_SUMMARY_CHARS,
        protect_current_turn: bool = True,
    ) -> None:
        self.keep_recent = max(1, int(keep_recent))
        self.min_compact_chars = max(1, int(min_compact_chars))
        self.summary_chars = max(120, int(summary_chars))
        self.protect_current_turn = bool(protect_current_turn)
        self.last_report: Optional[MicrocompactReport] = None

    def compact(
        self,
        history: List[Message],
        *,
        trigger: str = "manual",
        current_turn_id: str = "",
    ) -> MicrocompactReport:
        """Microcompact ``history`` in place and return a safe report."""
        before = estimate_history_tokens(history)
        call_map = _tool_call_map(history)
        tool_indices = [
            index for index, message in enumerate(history)
            if message.role == "tool"
        ]
        recent = set(tool_indices[-self.keep_recent:])
        eligible = [index for index in tool_indices if index not in recent]
        # The oldest half of eligible results become one-line references; the
        # newer half keeps a small structured preview.  With a tiny eligible
        # set, prefer the more informative summary tier.
        reference_cut = max(0, len(eligible) // 2)
        reference_indices = set(eligible[:reference_cut])
        entries: List[ToolMicrocompactEntry] = []
        preserved = protected = small = 0

        for index in eligible:
            message = history[index]
            call = call_map.get(str(message.tool_call_id or ""))
            tool_name = str(
                getattr(message, "tool_name", None)
                or (call.name if call is not None else "unknown")
            )
            meta = _message_tool_meta(message, call)
            content = str(message.content or "")
            rich_chars = _rich_content_chars(message.tool_blocks)
            effective_chars = max(len(content), rich_chars)

            if content.startswith(COMPACTED_PREFIX):
                preserved += 1
                continue
            if self._is_protected(
                message, meta, tool_name, current_turn_id=current_turn_id
            ):
                protected += 1
                continue
            if effective_chars <= self.min_compact_chars:
                small += 1
                continue

            recovery = _recovery_reference(content, meta)
            details = _details(meta, tool_name, recovery)
            tier = "reference" if index in reference_indices else "summary"
            if tier == "reference":
                compacted = (
                    f"[旧工具输出已驱逐：原 {len(content) or rich_chars} 字符；"
                    f"{details}；{RECOVERY_HINT}]"
                )
            else:
                preview = _bounded_preview(content, self.summary_chars)
                compacted = (
                    f"[旧工具输出已驱逐：原 {len(content) or rich_chars} 字符；"
                    f"{details}]\n摘要: {preview}\n"
                    f"[{RECOVERY_HINT}]"
                )

            if len(compacted) >= effective_chars:
                preserved += 1
                continue
            message.content = compacted
            # Rich blocks duplicate or exceed textual output.  Old compacted
            # results are represented by their recovery reference instead.
            message.tool_blocks = None
            entries.append(ToolMicrocompactEntry(
                message_index=index,
                tool_call_id=str(message.tool_call_id or ""),
                tool_name=tool_name,
                tier=tier,
                original_chars=effective_chars,
                compacted_chars=len(compacted),
                digest=hashlib.sha256(
                    (str(message.tool_call_id or "") + tool_name + str(effective_chars))
                    .encode("utf-8")
                ).hexdigest()[:16],
            ))

        after = estimate_history_tokens(history)
        report = MicrocompactReport(
            trigger=str(trigger or "manual"),
            before_tokens=before,
            after_tokens=after,
            compacted_count=len(entries),
            preserved_count=preserved,
            protected_count=protected,
            small_result_count=small,
            recent_count=len(recent),
            summarized_count=sum(item.tier == "summary" for item in entries),
            referenced_count=sum(item.tier == "reference" for item in entries),
            entries=tuple(entries),
        )
        self.last_report = report
        return report

    # Compatibility names used by early integrations and ContextManager.
    microcompact = compact
    evict_old_tool_results = compact

    def _is_protected(
        self,
        message: Message,
        meta: Mapping[str, Any],
        tool_name: str,
        *,
        current_turn_id: str,
    ) -> bool:
        if bool(getattr(message, "tool_is_error", False)) or bool(
            meta.get("is_error")
        ):
            return True
        if str(tool_name or "").lower() in PROTECTED_TOOLS:
            return True
        if self.protect_current_turn and current_turn_id:
            if str(meta.get("turn_id") or "") == str(current_turn_id):
                return True
        return False


def _tool_call_map(history: Sequence[Message]) -> Dict[str, ToolCall]:
    calls: Dict[str, ToolCall] = {}
    for message in history:
        for call in message.tool_calls or ():
            if call.id:
                calls[call.id] = call
    return calls


def _message_tool_meta(
    message: Message,
    call: Optional[ToolCall],
) -> Dict[str, Any]:
    raw = getattr(message, "tool_meta", None)
    meta: Dict[str, Any] = dict(raw) if isinstance(raw, Mapping) else {}
    if call is not None:
        for key in ("path", "pattern", "include", "command", "symbol", "cwd"):
            if key not in meta and key in call.arguments:
                meta[key] = call.arguments.get(key)
    if getattr(message, "tool_name", None) and "tool" not in meta:
        meta["tool"] = message.tool_name
    return meta


def _rich_content_chars(blocks: Optional[Sequence[Any]]) -> int:
    if not blocks:
        return 0
    total = 0
    for block in blocks:
        if isinstance(block, TextBlock):
            total += len(block.text)
        elif isinstance(block, ImageBlock):
            total += len(block.data_b64)
        elif isinstance(block, ResourceBlock):
            total += len(block.uri) + len(block.text or "") + len(block.blob or "")
        elif isinstance(block, StructuredBlock):
            try:
                total += len(json.dumps(block.data, ensure_ascii=False))
            except (TypeError, ValueError):
                total += len(repr(block.data))
    return total


def _recovery_reference(content: str, meta: Mapping[str, Any]) -> str:
    explicit = str(meta.get("full_output_path") or "").strip()
    if explicit:
        return explicit[:1_000]
    match = _PATH_REF_PATTERN.search(content)
    if match:
        return match.group(1).strip()[:1_000]
    return ""


def _details(
    meta: Mapping[str, Any],
    tool_name: str,
    recovery: str,
) -> str:
    parts = [f"tool={tool_name or 'unknown'}"]
    keys = (
        ("path", "path"), ("command", "command"), ("exit_code", "exit"),
        ("timed_out", "timeout"), ("count", "count"),
        ("files_scanned", "scanned"), ("files", "files"),
        ("bytes", "bytes"), ("replacements", "replacements"),
        ("truncated", "truncated"),
    )
    for key, label in keys:
        if key not in meta:
            continue
        value = meta.get(key)
        if value in (None, "", False, []):
            continue
        parts.append(f"{label}={_bounded_detail_value(value)}")
    if recovery:
        parts.append(f"全文已存盘: {recovery}")
    return "；".join(parts)


def _bounded_detail_value(value: Any, limit: int = 240) -> str:
    """Bound metadata copied into a replacement summary.

    Persisted metadata is normally already compacted, but sessions created by
    older versions may contain arbitrary lists or objects.  A summary must not
    silently reintroduce a large payload while trying to remove one.
    """
    if isinstance(value, (list, tuple)):
        visible = [str(item).replace("\n", " ") for item in list(value)[:8]]
        text = ", ".join(visible)
        if len(value) > len(visible):
            text += f", …(+{len(value) - len(visible)})"
    elif isinstance(value, dict):
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = repr(value)
    else:
        text = str(value)
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _bounded_preview(content: str, limit: int) -> str:
    text = content.strip()
    if not text:
        return "（无文本内容）"
    # Keep line structure for read_file/grep, but flatten pathological whitespace.
    text = "\n".join(
        line.rstrip() for line in text.splitlines() if line.strip())
    if len(text) > limit:
        text = text[:max(0, limit - 1)] + "…"
    return text


__all__ = [
    "COMPACTED_PREFIX",
    "DEFAULT_KEEP_RECENT_TOOLS",
    "DEFAULT_MIN_COMPACT_CHARS",
    "DEFAULT_SUMMARY_CHARS",
    "MicrocompactReport",
    "PROTECTED_TOOLS",
    "ToolCompactReport",
    "ToolMicrocompactEntry",
    "ToolMicrocompactResult",
    "ToolMicrocompactor",
]
