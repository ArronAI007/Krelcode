"""Session-scoped working context and its dynamic ContextSource.

Working Context is intentionally *not* long-term memory.  It is a bounded,
redacted view of the current task (goal, plan, files touched, commands, errors
and important observations) that is rebuilt from the live conversation.  The
store keeps provenance and lifecycle independent from the permanent MemoryStore,
while ``WorkingContextSource`` projects one immutable snapshot into a provider
request.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..core.messages import Message, ToolCall
from .tokens import estimate_text_tokens

WORKING_CONTEXT_KINDS = (
    "goal",
    "plan",
    "decision",
    "file",
    "command",
    "error",
    "observation",
    "open_issue",
)
_KIND_ALIASES = {
    "task": "goal",
    "todo": "plan",
    "files": "file",
    "commands": "command",
    "errors": "error",
    "observations": "observation",
    "issue": "open_issue",
}
_KIND_PRIORITY = {
    "open_issue": 0,
    "error": 1,
    "goal": 2,
    "plan": 3,
    "file": 4,
    "decision": 5,
    "command": 6,
    "observation": 7,
}
_GROUP_TAGS = {
    "goal": "goal",
    "plan": "plan",
    "decision": "decisions",
    "file": "files",
    "command": "commands",
    "error": "errors",
    "observation": "observations",
    "open_issue": "open_issues",
}
_METADATA_KEYS = (
    "path", "action", "status", "turn_id", "tool", "created", "bytes",
    "replacements", "total_lines", "redacted", "count", "files_scanned",
    "truncated", "files", "exit_code", "timed_out", "sandbox_denied",
    "sandbox_backend", "cancelled", "option_index", "command", "duration_ms",
    "workdir", "cwd", "line", "column", "pid", "full_output_path",
    "items", "statuses", "active_index", "explanation", "answer", "prompt",
    "options",
)
_MAX_METADATA_STRING = 400
_SECRETISH_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|authorization)\s*[:=]\s*\S+")

Redactor = Callable[[str], str]


def normalize_kind(kind: str) -> str:
    value = str(kind or "").strip().lower().replace("-", "_")
    value = _KIND_ALIASES.get(value, value)
    if value not in WORKING_CONTEXT_KINDS:
        raise ValueError(f"未知 Working Context 类型: {kind}")
    return value


def _bounded_text(text: object, limit: int, redactor: Optional[Redactor] = None) -> str:
    value = str(text or "").strip()
    if redactor is not None:
        try:
            value = redactor(value)
        except Exception:
            pass
    # Structured stores may be used by embedded SDK clients without a
    # SecretRedactor.  Keep a conservative key/value fallback so API keys and
    # tokens do not become durable working-context facts by accident.
    value = _SECRETISH_PATTERN.sub(
        lambda m: m.group(1) + "=***", value)
    if len(value) > limit:
        value = value[:max(0, limit - 1)] + "…"
    return value


def _safe_metadata_value(value: Any, redactor: Optional[Redactor]) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_text(value, _MAX_METADATA_STRING, redactor)
    if isinstance(value, (list, tuple)):
        return [
            _safe_metadata_value(item, redactor)
            for item in list(value)[:24]
        ]
    if isinstance(value, dict):
        return {
            _bounded_text(key, 80, redactor): _safe_metadata_value(item, redactor)
            for key, item in list(value.items())[:24]
        }
    return _bounded_text(value, _MAX_METADATA_STRING, redactor)


def compact_tool_metadata(
    tool_name: str,
    data: Optional[Mapping[str, Any]] = None,
    *,
    is_error: bool = False,
    turn_id: str = "",
    arguments: Optional[Mapping[str, Any]] = None,
    redactor: Optional[Redactor] = None,
) -> Dict[str, Any]:
    """Extract bounded, session-safe tool facts without copying large payloads."""
    result: Dict[str, Any] = {
        "tool": str(tool_name or "unknown")[:120],
        "is_error": bool(is_error),
    }
    if turn_id:
        result["turn_id"] = str(turn_id)[:160]
    source = data or {}
    for key in _METADATA_KEYS:
        if key not in source:
            continue
        value = source.get(key)
        if key == "full_output_path" and isinstance(value, str):
            value = value[:1_000]
        result[key] = _safe_metadata_value(value, redactor)
    # Arguments are useful only for a few cheap, high-signal fields.
    safe_args = arguments or {}
    for key in ("path", "pattern", "include", "command", "symbol", "cwd", "query"):
        if key in safe_args and key not in result:
            value = _safe_metadata_value(safe_args.get(key), redactor)
            if value not in (None, "", []):
                result[key] = value
    return result


@dataclass(frozen=True)
class WorkingEntry:
    """One bounded task fact with provenance and a stable update key."""

    id: str
    kind: str
    content: str
    source: str = "agent"
    status: str = "active"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    sequence: int = 0
    timestamp: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", normalize_kind(self.kind))
        object.__setattr__(self, "id", str(self.id).strip())
        object.__setattr__(self, "content", str(self.content).strip())
        object.__setattr__(self, "source", str(self.source or "agent")[:120])
        object.__setattr__(self, "status", str(self.status or "active")[:80])
        object.__setattr__(self, "metadata", dict(self.metadata or {}))

    def as_record(self) -> Dict[str, Any]:
        """Content-free representation for recorder/stream-json consumers."""
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "status": self.status,
            "chars": len(self.content),
            "tokens": estimate_text_tokens(self.content),
            "sequence": self.sequence,
        }


# Public compatibility alias: callers often use the more descriptive name.
WorkingContextEntry = WorkingEntry


@dataclass(frozen=True)
class WorkingContextSnapshot:
    """Immutable, bounded projection of the live working context."""

    entries: Tuple[WorkingEntry, ...]
    text: str
    truncated: bool = False
    sequence: int = 0
    dropped_entries: int = 0

    @property
    def count(self) -> int:
        return len(self.entries)

    @property
    def token_count(self) -> int:
        return estimate_text_tokens(self.text)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def counts_by_kind(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for entry in self.entries:
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return counts

    def as_record(self) -> Dict[str, Any]:
        """Metadata safe to expose through events and stream-json."""
        return {
            "entries": self.count,
            "tokens": self.token_count,
            "chars": self.char_count,
            "truncated": self.truncated,
            "dropped_entries": self.dropped_entries,
            "kinds": self.counts_by_kind,
            "sequence": self.sequence,
        }


class WorkingContextStore:
    """Bounded, session-scoped store for current-task facts.

    ``begin_turn`` sets the current goal, ``record_tool_result`` converts a
    structured tool result into a compact fact, and ``snapshot`` performs all
    final bounds so a corrupt or unusually long entry can never inflate a
    provider request without limit.
    """

    def __init__(
        self,
        workspace_root: Optional[Path] = None,
        *,
        redactor: Optional[Redactor] = None,
        max_entries: int = 24,
        max_entry_chars: int = 600,
        max_chars: int = 6_000,
        max_tokens: int = 1_800,
    ) -> None:
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.redactor = redactor
        self.max_entries = max(1, int(max_entries))
        self.max_entry_chars = max(60, int(max_entry_chars))
        self.max_chars = max(500, int(max_chars))
        self.max_tokens = max(120, int(max_tokens))
        self._entries: List[WorkingEntry] = []
        self._sequence = 0
        self._generation = 0
        self._dropped_entries = 0

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._sequence += 1
        self._generation += 1
        self._dropped_entries = 0

    reset = clear

    def upsert(
        self,
        kind: str,
        content: object,
        *,
        id: Optional[str] = None,
        source: str = "agent",
        status: str = "active",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Optional[WorkingEntry]:
        kind = normalize_kind(kind)
        text = _bounded_text(content, self.max_entry_chars, self.redactor)
        if not text:
            return None
        self._sequence += 1
        entry_id = str(id or f"{kind}:{self._sequence}").strip()[:240]
        safe_meta = _safe_metadata_value(dict(metadata or {}), self.redactor)
        entry = WorkingEntry(
            id=entry_id,
            kind=kind,
            content=text,
            source=source,
            status=_bounded_text(status, 80, self.redactor),
            metadata=dict(safe_meta) if isinstance(safe_meta, dict) else {},
            sequence=self._sequence,
            timestamp=self._now(),
        )
        self._entries = [item for item in self._entries if item.id != entry_id]
        self._entries.append(entry)
        self._trim_entry_count()
        return entry

    observe = upsert

    def remove(self, entry_id: str) -> bool:
        before = len(self._entries)
        self._entries = [item for item in self._entries if item.id != entry_id]
        return len(self._entries) != before

    def invalidate_files(
        self,
        paths: Sequence[str],
        *,
        reason: str = "file-changed",
    ) -> int:
        """Drop stale file facts after resume hash verification.

        Re-resumed history remains authoritative, but a file that changed after
        the checkpoint must not keep presenting its old state as a current task
        fact. The invalidation itself is retained as an open issue.
        """
        normalized = tuple(
            dict.fromkeys(str(path).strip() for path in paths if str(path).strip()))
        if not normalized:
            return 0
        wanted = set(normalized)
        removed = 0
        kept = []
        for entry in self._entries:
            metadata_path = str((entry.metadata or {}).get("path", "") or "")
            matches = metadata_path in wanted
            if not matches:
                matches = any(
                    path in entry.content
                    or entry.id.endswith(f":{path}")
                    for path in normalized
                )
            if matches and entry.kind in {"file", "observation"}:
                removed += 1
                continue
            kept.append(entry)
        self._entries = kept
        self.upsert(
            "open_issue",
            "文件在会话检查点后发生变化，旧内容已失效: "
            + ", ".join(normalized),
            id="open_issue:resume-file-integrity",
            source="context-restore",
            status="stale",
            metadata={
                "paths": list(normalized),
                "reason": reason,
                "invalidated_entries": removed,
            },
        )
        return removed

    def restore_snapshot(self, record: Mapping[str, Any]) -> WorkingContextSnapshot:
        """Hydrate the bounded task facts captured in a ContextCheckpoint.

        Rebuilding from history is sufficient for short sessions, but a compact
        turn intentionally removes older facts from the authoritative message
        list. The checkpoint therefore preserves the exact live projection so
        resume/rewind can start from the same handoff state. File-backed facts
        are verified and invalidated by the caller before the next request.
        """
        raw_entries = record.get("entries")
        restored: list[WorkingEntry] = []
        if isinstance(raw_entries, (list, tuple)):
            for raw in raw_entries:
                if not isinstance(raw, Mapping):
                    continue
                try:
                    kind = normalize_kind(str(raw.get("kind", "") or ""))
                except ValueError:
                    continue
                content = _bounded_text(
                    raw.get("content", ""), self.max_entry_chars, self.redactor)
                if not content:
                    continue
                metadata = _safe_metadata_value(
                    raw.get("metadata", {}), self.redactor)
                if not isinstance(metadata, dict):
                    metadata = {}
                try:
                    sequence = max(0, int(raw.get("sequence", 0) or 0))
                    timestamp = float(raw.get("timestamp", 0.0) or 0.0)
                except (TypeError, ValueError):
                    sequence = 0
                    timestamp = 0.0
                restored.append(WorkingEntry(
                    id=str(raw.get("id", "") or f"{kind}:{sequence or len(restored) + 1}"),
                    kind=kind,
                    content=content,
                    source=_bounded_text(
                        raw.get("source", "context-checkpoint"),
                        120,
                        self.redactor,
                    ) or "context-checkpoint",
                    status=_bounded_text(
                        raw.get("status", "active"), 80, self.redactor) or "active",
                    metadata=metadata,
                    sequence=sequence,
                    timestamp=timestamp,
                ))

        self._entries = sorted(
            restored,
            key=lambda item: (item.sequence, item.timestamp, item.id),
        )
        try:
            recorded_sequence = max(0, int(record.get("sequence", 0) or 0))
        except (TypeError, ValueError):
            recorded_sequence = 0
        self._sequence = max(
            recorded_sequence,
            max((item.sequence for item in self._entries), default=0),
        )
        self._generation += 1
        self._dropped_entries = 0
        self._trim_entry_count()
        return self.snapshot()

    def begin_turn(self, turn_id: str, user_input: str) -> Optional[WorkingEntry]:
        """Set the current goal; previous goals are intentionally superseded."""
        return self.upsert(
            "goal",
            user_input,
            id="goal:current",
            source="user",
            metadata={"turn_id": turn_id},
        )

    def record_plan(
        self,
        items: Sequence[str],
        statuses: Sequence[str],
        *,
        active_index: int = -1,
        explanation: str = "",
        turn_id: str = "",
    ) -> Optional[WorkingEntry]:
        item_list = [str(item).strip() for item in items if str(item).strip()]
        status_list = [str(status).strip() for status in statuses]
        if not item_list:
            return None
        rendered = []
        for index, item in enumerate(item_list):
            status = status_list[index] if index < len(status_list) else "pending"
            mark = {
                "completed": "✓",
                "in_progress": "▶",
                "pending": "○",
            }.get(status, "○")
            rendered.append(f"{mark} {item}")
        text = "\n".join(rendered)
        if explanation:
            text += f"\n说明: {explanation}"
        return self.upsert(
            "plan",
            text,
            id="plan:current",
            source="plan",
            status="active",
            metadata={
                "active_index": int(active_index),
                "items": item_list[:24],
                "statuses": status_list[:24],
                "turn_id": turn_id,
            },
        )

    def record_decision(
        self,
        content: str,
        *,
        id: Optional[str] = None,
        source: str = "agent",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Optional[WorkingEntry]:
        return self.upsert(
            "decision", content, id=id, source=source, metadata=metadata)

    def record_error(
        self,
        content: str,
        *,
        source: str = "agent",
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Optional[WorkingEntry]:
        return self.upsert("error", content, source=source, metadata=metadata)

    def record_tool_result(
        self,
        tool_call: ToolCall,
        result: Any,
        *,
        turn_id: str = "",
    ) -> None:
        """Project a ToolResult into one or more bounded task facts."""
        name = str(getattr(tool_call, "name", "") or "unknown")
        data = getattr(result, "data", {}) or {}
        is_error = bool(getattr(result, "is_error", False))
        content = str(getattr(result, "content", "") or "")
        args = getattr(tool_call, "arguments", {}) or {}
        meta = compact_tool_metadata(
            name,
            data,
            is_error=is_error,
            turn_id=turn_id,
            arguments=args,
            redactor=self.redactor,
        )
        path = self._relative_path(meta.get("path"))
        if path:
            meta["path"] = path

        if is_error:
            self.record_error(
                f"{name}: {content}",
                source=f"tool:{name}",
                metadata={
                    **meta,
                    "tool_call_id": getattr(tool_call, "id", ""),
                },
            )
            return

        lower = name.lower()
        if lower in {"write_file", "edit_file"}:
            action = "created" if meta.get("created") else "modified"
            if lower == "write_file":
                detail = f"已写入 {path or '(unknown path)'}"
                if meta.get("bytes") is not None:
                    detail += f" · {meta['bytes']} bytes"
            else:
                detail = f"已编辑 {path or '(unknown path)'}"
                if meta.get("replacements") is not None:
                    detail += f" · {meta['replacements']} 处"
            self.upsert(
                "file",
                detail,
                id=f"file:{path or getattr(tool_call, 'id', 'current')}",
                source=f"tool:{name}",
                status=action,
                metadata=meta,
            )
            return
        if lower == "read_file":
            detail = f"已读取 {path or '(unknown path)'}"
            if meta.get("total_lines") is not None:
                detail += f" · {meta['total_lines']} 行"
            self.upsert(
                "observation",
                detail,
                id=f"read:{path or getattr(tool_call, 'id', 'current')}",
                source=f"tool:{name}",
                metadata=meta,
            )
            return
        if lower == "run_command":
            command = str(meta.get("command") or args.get("command") or "").strip()
            detail = f"$ {command or '(unknown command)'}"
            if meta.get("exit_code") is not None:
                detail += f" · exit {meta['exit_code']}"
            if meta.get("timed_out"):
                detail += " · timeout"
            self.upsert(
                "command",
                detail,
                id=f"command:{getattr(tool_call, 'id', 'current')}",
                source=f"tool:{name}",
                status="failed" if is_error else "succeeded",
                metadata=meta,
            )
            return
        if lower == "update_plan":
            items = meta.get("items") if isinstance(meta.get("items"), list) else []
            statuses = (
                meta.get("statuses") if isinstance(meta.get("statuses"), list)
                else [])
            if items:
                self.record_plan(
                    [str(item) for item in items],
                    [str(status) for status in statuses],
                    active_index=int(meta.get("active_index") or -1),
                    explanation=str(meta.get("explanation") or ""),
                    turn_id=turn_id,
                )
            return
        if lower in {"ask_user", "request_user_input"}:
            answer = str(meta.get("answer") or "").strip()
            if answer:
                self.record_decision(
                    f"用户选择: {answer}",
                    source=f"tool:{name}",
                    metadata=meta,
                )
            return

        summary = self._tool_summary(name, data, args, path)
        self.upsert(
            "observation",
            summary,
            id=f"observation:{getattr(tool_call, 'id', self._sequence + 1)}",
            source=f"tool:{name}",
            status="observed",
            metadata=meta,
        )

    def record_tool_message(self, message: Message, tool_call: Optional[ToolCall] = None) -> None:
        """Project a persisted tool message (used by resume/rewind rebuild)."""
        name = (
            getattr(message, "tool_name", None)
            or (tool_call.name if tool_call is not None else "unknown")
        )
        tool_call = tool_call or ToolCall(
            id=str(getattr(message, "tool_call_id", "") or ""),
            name=str(name or "unknown"),
            arguments={},
        )
        stub = _ToolResultStub(
            content=str(message.content or ""),
            is_error=bool(getattr(message, "tool_is_error", False)),
            data=dict(getattr(message, "tool_meta", None) or {}),
        )
        self.record_tool_result(tool_call, stub)

    def rebuild_from_history(
        self,
        history: Sequence[Message],
        *,
        max_messages: int = 400,
    ) -> WorkingContextSnapshot:
        """Rebuild current facts from the retained authoritative history."""
        self.clear()
        calls: Dict[str, ToolCall] = {}
        recent = list(history)[-max(0, max_messages):] if max_messages else list(history)
        last_goal = ""
        last_goal_turn = ""
        for message in recent:
            if message.role == "assistant" and message.tool_calls:
                for call in message.tool_calls:
                    calls[call.id] = call
            elif message.role == "tool":
                self.record_tool_message(message, calls.get(message.tool_call_id or ""))
            elif message.role == "user" and message.content:
                content = str(message.content)
                if content.startswith("[上下文摘要]"):
                    continue
                if content.startswith("[用户插话]"):
                    self.upsert(
                        "observation",
                        content,
                        source="user_steering",
                    )
                else:
                    last_goal = content
                    last_goal_turn = str(
                        (message.provider_meta or {}).get("turn_id", ""))
        if last_goal:
            self.begin_turn(last_goal_turn, last_goal)
        self._generation += 1
        return self.snapshot()

    def snapshot(self) -> WorkingContextSnapshot:
        selected = list(self._entries)
        dropped = self._dropped_entries
        truncated = self._dropped_entries > 0
        text = self._render(selected)
        while selected and (
            len(selected) > self.max_entries
            or len(text) > self.max_chars
            or estimate_text_tokens(text) > self.max_tokens
        ):
            victim = min(
                selected,
                key=lambda item: (
                    -_KIND_PRIORITY.get(item.kind, 99),
                    item.sequence,
                ),
            )
            selected.remove(victim)
            dropped += 1
            truncated = True
            text = self._render(selected)
        return WorkingContextSnapshot(
            entries=tuple(selected),
            text=text,
            truncated=truncated,
            sequence=self._sequence,
            dropped_entries=dropped,
        )

    def clone(self) -> "WorkingContextStore":
        clone = WorkingContextStore(
            self.workspace_root,
            redactor=self.redactor,
            max_entries=self.max_entries,
            max_entry_chars=self.max_entry_chars,
            max_chars=self.max_chars,
            max_tokens=self.max_tokens,
        )
        clone._entries = [replace(item) for item in self._entries]
        clone._sequence = self._sequence
        clone._generation = self._generation
        clone._dropped_entries = self._dropped_entries
        return clone

    def _trim_entry_count(self) -> None:
        while len(self._entries) > self.max_entries:
            victim = min(
                self._entries,
                key=lambda item: (
                    -_KIND_PRIORITY.get(item.kind, 99),
                    item.sequence,
                ),
            )
            self._entries.remove(victim)
            self._dropped_entries += 1

    def _render(self, entries: Sequence[WorkingEntry]) -> str:
        if not entries:
            return ""
        grouped: Dict[str, List[WorkingEntry]] = {
            kind: [] for kind in WORKING_CONTEXT_KINDS
        }
        for entry in entries:
            grouped[entry.kind].append(entry)
        lines = ["<working_context>"]
        for kind in WORKING_CONTEXT_KINDS:
            items = grouped.get(kind) or []
            if not items:
                continue
            tag = _GROUP_TAGS[kind]
            if kind in {"goal", "plan"}:
                # Plan values may contain newlines; preserve them for readability.
                lines.append(f"<{tag}>")
                lines.extend(item.content for item in items)
                lines.append(f"</{tag}>")
            else:
                lines.append(f"<{tag}>")
                for item in items:
                    lines.append(f"- {item.content}")
                lines.append(f"</{tag}>")
        lines.append("</working_context>")
        return "\n".join(lines)

    def _tool_summary(
        self,
        name: str,
        data: Mapping[str, Any],
        args: Mapping[str, Any],
        path: str,
    ) -> str:
        lower = name.lower()
        if lower in {"grep", "find_refs"}:
            target = data.get("count", 0)
            pattern = args.get("pattern") or args.get("symbol") or ""
            return f"{name} 匹配 {target} 条" + (
                f" · {pattern}" if pattern else "")
        if lower == "glob":
            return f"glob 匹配 {data.get('count', 0)} 个"
        if lower == "repo_map":
            return f"repo_map 覆盖 {data.get('files', 0)} 个文件"
        if lower == "check_syntax":
            return (
                f"check_syntax 检查 {data.get('files', 0)} 个文件"
                f" · {data.get('errors', 0)} 个错误")
        if path:
            return f"{name}: {path}"
        return f"{name} 已完成"

    def _relative_path(self, value: Any) -> str:
        if not isinstance(value, str) or not value:
            return ""
        if self.workspace_root is None:
            return _bounded_text(value, 400, self.redactor)
        try:
            path = Path(value)
            if not path.is_absolute():
                return _bounded_text(value, 400, self.redactor)
            return os.path.relpath(path, self.workspace_root)
        except (OSError, ValueError):
            return _bounded_text(value, 400, self.redactor)

    @staticmethod
    def _now() -> float:
        import time
        return time.time()


@dataclass(frozen=True)
class _ToolResultStub:
    content: str
    is_error: bool = False
    data: Mapping[str, Any] = field(default_factory=dict)


# ``WorkingContext`` is a convenient public name in embedded integrations.
WorkingContext = WorkingContextStore


class WorkingContextSource:
    """Expose a bounded WorkingContextSnapshot as volatile runtime context."""

    name = "working_context"

    def __init__(self, store: Optional[WorkingContextStore] = None) -> None:
        self.store = store
        self.last_snapshot: Optional[WorkingContextSnapshot] = None

    def collect(self, request):
        snapshot = getattr(request, "working_context", None)
        if snapshot is None and self.store is not None:
            snapshot = self.store.snapshot()
        if snapshot is None or not getattr(snapshot, "text", ""):
            self.last_snapshot = snapshot
            return ()
        self.last_snapshot = snapshot
        # Imported lazily to keep the store usable without the assembler stack.
        from .context_source import (
            CATEGORY_RUNTIME,
            ContextLayer,
            ContextTokenContribution,
        )

        return (ContextLayer(
            id="runtime:working-context",
            source=self.name,
            category=CATEGORY_RUNTIME,
            content=snapshot.text,
            priority=40,
            cacheable=False,
            compactable=True,
            metadata={
                "entries": snapshot.count,
                "truncated": snapshot.truncated,
                "dropped_entries": snapshot.dropped_entries,
                "kinds": snapshot.counts_by_kind,
                "sequence": snapshot.sequence,
            },
            token_contributions=(ContextTokenContribution(
                "runtime:working-context",
                CATEGORY_RUNTIME,
                snapshot.token_count,
            ),),
        ),)


__all__ = [
    "WORKING_CONTEXT_KINDS",
    "WorkingContext",
    "WorkingContextEntry",
    "WorkingContextSnapshot",
    "WorkingContextSource",
    "WorkingContextStore",
    "WorkingEntry",
    "compact_tool_metadata",
    "normalize_kind",
]
