"""Structured compact handoff schema and deterministic fallbacks.

Step 90 replaces free-form summaries with a small, machine verifiable
handoff record.  The record survives repeated compactions because every new
summary is merged with the previous one instead of replacing it blindly.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple, cast

from krelcode.core.messages import Message, ToolCall
from krelcode.agent.context.tokens import estimate_text_tokens
from krelcode.agent.value_utils import merge_by_key

COMPACT_SUMMARY_VERSION = 2
SUMMARY_MARKER_START = "{"
SUMMARY_MARKER_END = "}"
SUMMARY_PREFIX = "[上下文摘要] "
SUMMARY_V2_PREFIX = SUMMARY_PREFIX + "v2\n"

_MAX_GOAL_CHARS = 4_000
_MAX_ITEM_CHARS = 1_200
_MAX_ITEMS = 32
_TEST_COMMAND_PARTS = (
    "pytest", "unittest", "npm test", "pnpm test", "yarn test",
    "vitest", "jest", "cargo test", "go test", "ruff", "mypy",
    "check_syntax",
)


class CompactSummaryError(ValueError):
    """A model-produced summary does not satisfy the v2 schema."""


COMPACT_SUMMARY_JSON_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://krelcode.dev/schemas/compact-summary-v2.json",
    "title": "Krelcode Structured Compact Summary v2",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "goal", "completed", "decisions", "files", "tests", "open_issues",
        "next_steps", "critical_constraints",
    ],
    "properties": {
        "goal": {"type": "string", "maxLength": _MAX_GOAL_CHARS},
        "completed": {
            "type": "array", "maxItems": _MAX_ITEMS,
            "items": {"type": "string", "maxLength": _MAX_ITEM_CHARS},
        },
        "decisions": {
            "type": "array", "maxItems": _MAX_ITEMS,
            "items": {"type": "string", "maxLength": _MAX_ITEM_CHARS},
        },
        "files": {
            "type": "array", "maxItems": _MAX_ITEMS,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["path", "status", "note"],
                "properties": {
                    "path": {"type": "string", "minLength": 1,
                             "maxLength": _MAX_ITEM_CHARS},
                    "status": {
                        "type": "string",
                        "enum": ["created", "modified", "read", "touched"],
                    },
                    "note": {"type": "string", "maxLength": _MAX_ITEM_CHARS},
                },
            },
        },
        "tests": {
            "type": "array", "maxItems": _MAX_ITEMS,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["name", "status", "detail"],
                "properties": {
                    "name": {"type": "string", "minLength": 1,
                             "maxLength": _MAX_ITEM_CHARS},
                    "status": {
                        "type": "string",
                        "enum": ["passed", "failed", "skipped", "unknown"],
                    },
                    "detail": {"type": "string", "maxLength": _MAX_ITEM_CHARS},
                },
            },
        },
        "open_issues": {
            "type": "array", "maxItems": _MAX_ITEMS,
            "items": {"type": "string", "maxLength": _MAX_ITEM_CHARS},
        },
        "next_steps": {
            "type": "array", "maxItems": _MAX_ITEMS,
            "items": {"type": "string", "maxLength": _MAX_ITEM_CHARS},
        },
        "critical_constraints": {
            "type": "array", "maxItems": _MAX_ITEMS,
            "items": {"type": "string", "maxLength": _MAX_ITEM_CHARS},
        },
    },
}


@dataclass(frozen=True)
class CompactFile:
    path: str
    status: str = "touched"
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "status": self.status, "note": self.note}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CompactFile":
        if not isinstance(raw, Mapping):
            raise CompactSummaryError("files[] 必须是 object")
        path = _required_string(raw.get("path"), "files[].path")
        status = _bounded(raw.get("status", "touched"), 80).lower()
        if status not in {"created", "modified", "read", "touched"}:
            raise CompactSummaryError(f"files[].status 非法: {status}")
        return cls(
            path=path,
            status=status,
            note=_bounded(raw.get("note", ""), _MAX_ITEM_CHARS),
        )


@dataclass(frozen=True)
class CompactTestStatus:
    name: str
    status: str = "unknown"
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "status": self.status, "detail": self.detail}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CompactTestStatus":
        if not isinstance(raw, Mapping):
            raise CompactSummaryError("tests[] 必须是 object")
        name = _required_string(raw.get("name"), "tests[].name")
        status = _bounded(raw.get("status", "unknown"), 80).lower()
        if status not in {"passed", "failed", "skipped", "unknown"}:
            raise CompactSummaryError(f"tests[].status 非法: {status}")
        return cls(
            name=name,
            status=status,
            detail=_bounded(raw.get("detail", ""), _MAX_ITEM_CHARS),
        )


@dataclass(frozen=True)
class CompactSummary:
    """Bounded handoff state stored as JSON inside one summary message."""

    version: int = COMPACT_SUMMARY_VERSION
    goal: str = ""
    completed: Tuple[str, ...] = ()
    decisions: Tuple[str, ...] = ()
    files: Tuple[CompactFile, ...] = ()
    # Retained as internal/audit fields for backward compatibility and
    # deterministic fallbacks. They are not part of the model-facing v2 schema.
    commands: Tuple[str, ...] = ()
    tests: Tuple[CompactTestStatus, ...] = ()
    errors: Tuple[str, ...] = ()
    open_issues: Tuple[str, ...] = ()
    next_steps: Tuple[str, ...] = ()
    critical_constraints: Tuple[str, ...] = ()
    handoff_notes: Tuple[str, ...] = ()
    source: str = "model"
    generation: int = 1
    compacted_messages: int = 0
    created_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "version", int(self.version))
        object.__setattr__(self, "goal", _bounded(self.goal, _MAX_GOAL_CHARS))
        object.__setattr__(self, "source", _bounded(self.source or "model", 80))
        object.__setattr__(self, "generation", max(1, int(self.generation or 1)))
        object.__setattr__(
            self, "compacted_messages", max(0, int(self.compacted_messages or 0)))
        object.__setattr__(
            self, "created_at", float(self.created_at or 0.0))

    @property
    def token_count(self) -> int:
        return estimate_text_tokens(self.render())

    @property
    def digest(self) -> str:
        payload = json.dumps(
            self._content_dict(), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict[str, object]:
        """Return the persisted/audit representation.

        This includes deterministic metadata that must never be sent back to
        the summary model. Use :meth:`as_contract_dict` for the model-facing
        contract and :meth:`render` for the compact history wire format.
        """
        return {
            **self._content_dict(),
            "source": self.source,
            "generation": self.generation,
            "compacted_messages": self.compacted_messages,
            "created_at": self.created_at,
        }

    def as_contract_dict(self) -> dict[str, object]:
        """Return exactly the machine-verifiable model-facing v2 schema."""
        return {
            "goal": self.goal,
            "completed": list(self.completed),
            "decisions": list(self.decisions),
            "files": [item.as_dict() for item in self.files],
            "tests": [item.as_dict() for item in self.tests],
            "open_issues": list(self.open_issues),
            "next_steps": list(self.next_steps),
            "critical_constraints": list(self.critical_constraints),
        }

    def _content_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "goal": self.goal,
            "completed": list(self.completed),
            "decisions": list(self.decisions),
            "files": [item.as_dict() for item in self.files],
            "commands": list(self.commands),
            "tests": [item.as_dict() for item in self.tests],
            "errors": list(self.errors),
            "open_issues": list(self.open_issues),
            "next_steps": list(self.next_steps),
            "critical_constraints": list(self.critical_constraints),
            "handoff_notes": list(self.handoff_notes),
        }

    def render(self) -> str:
        payload_data: dict[str, object] = {}
        if self.goal:
            payload_data["g"] = self.goal
        for key, alias in (
            ("completed", "q"), ("decisions", "d"), ("files", "f"),
            ("commands", "c"), ("tests", "t"), ("errors", "e"),
            ("open_issues", "o"), ("next_steps", "x"),
            ("critical_constraints", "r"), ("handoff_notes", "n"),
        ):
            value = getattr(self, key)
            if value:
                payload_data[alias] = [
                    item.as_dict() if hasattr(item, "as_dict") else item
                    for item in value
                ]
        if self.generation != 1:
            payload_data["i"] = self.generation
        payload = json.dumps(
            payload_data, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"))
        return SUMMARY_V2_PREFIX + payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CompactSummary":
        if not isinstance(raw, Mapping):
            raise CompactSummaryError("摘要必须是 JSON object")
        version = raw.get("version", raw.get("v", COMPACT_SUMMARY_VERSION))
        if not isinstance(version, int) or version != COMPACT_SUMMARY_VERSION:
            raise CompactSummaryError(f"不支持的摘要版本: {version!r}")
        files_raw = raw.get("files", raw.get("f", []))
        tests_raw = raw.get("tests", raw.get("t", []))
        if not isinstance(files_raw, list):
            raise CompactSummaryError("files 必须是数组")
        if not isinstance(tests_raw, list):
            raise CompactSummaryError("tests 必须是数组")
        if len(files_raw) > _MAX_ITEMS:
            raise CompactSummaryError(f"files 超过 {_MAX_ITEMS} 项")
        if len(tests_raw) > _MAX_ITEMS:
            raise CompactSummaryError(f"tests 超过 {_MAX_ITEMS} 项")
        summary = cls(
            version=version,
            goal=_bounded(raw.get("goal", raw.get("g", "")), _MAX_GOAL_CHARS),
            completed=_string_tuple(
                raw.get("completed", raw.get("q", [])), "completed"),
            decisions=_string_tuple(raw.get("decisions", raw.get("d", [])), "decisions"),
            files=tuple(CompactFile.from_dict(item) for item in files_raw),
            commands=_string_tuple(raw.get("commands", raw.get("c", [])), "commands"),
            tests=tuple(
                CompactTestStatus.from_dict(item) for item in tests_raw),
            errors=_string_tuple(raw.get("errors", raw.get("e", [])), "errors"),
            open_issues=_string_tuple(
                raw.get("open_issues", raw.get("o", [])), "open_issues"),
            next_steps=_string_tuple(
                raw.get("next_steps", raw.get("x", [])), "next_steps"),
            critical_constraints=_string_tuple(
                raw.get("critical_constraints", raw.get("r", [])),
                "critical_constraints"),
            handoff_notes=_string_tuple(
                raw.get("handoff_notes", raw.get("n", [])), "handoff_notes"),
            source={
                "h": "history_fallback",
                "w": "working_context",
            }.get(
                str(raw.get("s", "")),
                _bounded(raw.get("source", "model"), 80),
            ),
            generation=max(1, _int_value(
                raw.get("generation", raw.get("i", 1)), "generation")),
            compacted_messages=max(
                0, _int_value(raw.get("compacted_messages", 0), "compacted_messages")),
            created_at=_float_value(raw.get("created_at", 0.0), "created_at"),
        )
        issues = summary.validate()
        if issues:
            raise CompactSummaryError("; ".join(issues))
        return summary

    @classmethod
    def from_json_text(cls, text: str) -> "CompactSummary":
        raw = _extract_json_object(text)
        return cls.from_dict(raw)

    @classmethod
    def from_model_json_text(cls, text: str) -> "CompactSummary":
        """Parse the strict model-facing contract, not compact wire aliases."""
        raw = _extract_json_object(text)
        required = set(cast(Iterable[str], COMPACT_SUMMARY_JSON_SCHEMA["required"]))
        missing = sorted(required - set(raw))
        if missing:
            raise CompactSummaryError(
                "摘要缺少必填字段: " + ", ".join(missing))
        allowed = set(cast(Mapping[str, object], COMPACT_SUMMARY_JSON_SCHEMA["properties"]))
        unexpected = sorted(set(raw) - allowed)
        if unexpected:
            raise CompactSummaryError(
                "摘要包含未声明字段: " + ", ".join(unexpected))
        issues = _model_contract_issues(raw)
        if issues:
            raise CompactSummaryError("; ".join(issues))
        return cls.from_dict(raw)

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.version != COMPACT_SUMMARY_VERSION:
            issues.append("version 必须为 2")
        if len(self.goal) > _MAX_GOAL_CHARS:
            issues.append("goal 超长")
        for name, values in (
            ("completed", self.completed),
            ("decisions", self.decisions),
            ("commands", self.commands),
            ("errors", self.errors),
            ("open_issues", self.open_issues),
            ("next_steps", self.next_steps),
            ("critical_constraints", self.critical_constraints),
            ("handoff_notes", self.handoff_notes),
            ("files", self.files),
            ("tests", self.tests),
        ):
            if len(values) > _MAX_ITEMS:
                issues.append(f"{name} 超过 {_MAX_ITEMS} 项")
        if any(
            item.status not in {"created", "modified", "read", "touched"}
            for item in self.files
        ):
            issues.append("files 存在非法 status")
        if len({item.path for item in self.files}) != len(self.files):
            issues.append("files 存在重复 path")
        if any(
            item.status not in {"passed", "failed", "skipped", "unknown"}
            for item in self.tests
        ):
            issues.append("tests 存在非法 status")
        if len({item.name for item in self.tests}) != len(self.tests):
            issues.append("tests 存在重复 name")
        return issues

    def record(self) -> dict[str, object]:
        """Content-free accounting safe for events and stream-json."""
        return {
            "version": self.version,
            "source": self.source,
            "generation": self.generation,
            "digest": self.digest,
            "tokens": self.token_count,
            "goal_chars": len(self.goal),
            "completed": len(self.completed),
            "files": len(self.files),
            "tests": len(self.tests),
            "errors": len(self.errors),
            "critical_constraints": len(self.critical_constraints),
            "compacted_messages": self.compacted_messages,
        }


def merge_summaries(
    previous: Optional[CompactSummary],
    *candidates: Optional[CompactSummary],
) -> CompactSummary:
    """Merge summaries from oldest to newest with stable key de-duplication."""
    result = previous or CompactSummary(generation=1)
    for candidate in candidates:
        if candidate is None:
            continue
        result = CompactSummary(
            version=COMPACT_SUMMARY_VERSION,
            goal=candidate.goal or result.goal,
            completed=_merge_strings(result.completed, candidate.completed),
            decisions=_merge_strings(result.decisions, candidate.decisions),
            files=merge_by_key(
                result.files, candidate.files, lambda item: item.path, limit=_MAX_ITEMS),
            commands=_merge_strings(result.commands, candidate.commands),
            tests=merge_by_key(
                result.tests, candidate.tests, lambda item: item.name, limit=_MAX_ITEMS),
            errors=_merge_strings(result.errors, candidate.errors),
            open_issues=_merge_strings(
                result.open_issues, candidate.open_issues),
            next_steps=_merge_strings(result.next_steps, candidate.next_steps),
            critical_constraints=_merge_strings(
                result.critical_constraints, candidate.critical_constraints),
            handoff_notes=_merge_strings(
                result.handoff_notes, candidate.handoff_notes),
            source=candidate.source,
            generation=max(result.generation, candidate.generation),
            compacted_messages=max(
                result.compacted_messages, candidate.compacted_messages),
            created_at=max(result.created_at, candidate.created_at),
        )
    return result


def build_working_context_fallback(
    snapshot: Any,
    *,
    previous: Optional[CompactSummary] = None,
    raw_model_text: str = "",
    compacted_messages: int = 0,
) -> CompactSummary:
    """Build a valid handoff from the bounded Working Context snapshot."""
    goal = ""
    completed: list[str] = []
    decisions: list[str] = []
    files: list[CompactFile] = []
    commands: list[str] = []
    tests: list[CompactTestStatus] = []
    errors: list[str] = []
    open_issues: list[str] = []
    next_steps: list[str] = []
    critical_constraints: list[str] = []
    notes: list[str] = []
    for entry in getattr(snapshot, "entries", ()) or ():
        kind = str(getattr(entry, "kind", ""))
        content = str(getattr(entry, "content", "") or "").strip()
        meta = getattr(entry, "metadata", {}) or {}
        if not content:
            continue
        if kind == "goal":
            goal = content
        elif kind == "critical_constraint":
            critical_constraints.append(content)
        elif kind == "decision":
            decisions.append(content)
        elif kind == "file":
            path = str(meta.get("path") or content)
            raw_status = str(meta.get("action") or "").lower()
            if raw_status not in {"created", "modified", "read", "touched"}:
                raw_status = (
                    "created" if "创建" in content
                    else "modified" if "编辑" in content or "修改" in content
                    else "read" if "读取" in content
                    else "touched")
            files.append(CompactFile(
                path=path,
                status=raw_status,
                note="" if path == content else content,
            ))
        elif kind == "command":
            commands.append(content)
            if _looks_like_test(content, meta):
                tests.append(_test_from_metadata(content, meta))
        elif kind == "error":
            errors.append(content)
        elif kind == "open_issue":
            open_issues.append(content)
        elif kind == "plan":
            completed.extend(_completed_plan_steps(content, meta))
            next_steps.extend(_plan_steps(content, meta))
        elif kind == "observation":
            notes.append(content)
    if raw_model_text.strip():
        notes.append(raw_model_text.strip())
    candidate = CompactSummary(
        goal=goal,
        completed=tuple(completed),
        decisions=tuple(decisions),
        files=tuple(files),
        commands=tuple(commands),
        tests=tuple(tests),
        errors=tuple(errors),
        open_issues=tuple(open_issues),
        next_steps=tuple(next_steps),
        critical_constraints=tuple(critical_constraints),
        handoff_notes=tuple(notes),
        source="working_context",
        generation=max(1, (previous.generation if previous else 0) + 1),
        compacted_messages=compacted_messages,
        created_at=time.time(),
    )
    return merge_summaries(previous, candidate)


def build_history_fallback(
    messages: Sequence[Message],
    *,
    previous: Optional[CompactSummary] = None,
    raw_model_text: str = "",
    compacted_messages: int = 0,
) -> CompactSummary:
    """Deterministically recover high-signal facts when the summary model fails."""
    calls: dict[str, ToolCall] = {}
    goal = ""
    completed: list[str] = []
    files: list[CompactFile] = []
    commands: list[str] = []
    tests: list[CompactTestStatus] = []
    errors: list[str] = []
    notes: list[str] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            for call in message.tool_calls:
                calls[call.id] = call
        elif message.role == "user" and not is_compact_summary_message(message.content):
            if message.content:
                goal = str(message.content).strip()
        elif message.role == "tool":
            found_call = calls.get(str(message.tool_call_id or ""))
            meta = dict(getattr(message, "tool_meta", None) or {})
            name = str(
                getattr(message, "tool_name", None)
                or (found_call.name if found_call else "unknown"))
            if found_call is not None:
                for key in ("path", "command", "pattern", "cwd"):
                    if key not in meta and key in found_call.arguments:
                        meta[key] = found_call.arguments.get(key)
            path = str(meta.get("path") or "").strip()
            if path:
                status = (
                    "created" if meta.get("created")
                    else "modified" if name in {"write_file", "edit_file"}
                    else "read" if name == "read_file" else "touched")
                files.append(CompactFile(path=path, status=status))
            command = str(meta.get("command") or "").strip()
            completed.extend(_completed_plan_steps(command, meta))
            if command:
                commands.append(command)
                if _looks_like_test(command, meta):
                    tests.append(_test_from_metadata(command, meta))
            if bool(getattr(message, "tool_is_error", False)):
                first_line = (message.content or "tool error").splitlines()[0]
                errors.append(f"{name}: {first_line}")
    if raw_model_text.strip():
        notes.append(raw_model_text.strip())
    candidate = CompactSummary(
        goal=goal,
        completed=tuple(completed),
        files=tuple(files),
        commands=tuple(commands),
        tests=tuple(tests),
        errors=tuple(errors),
        handoff_notes=tuple(notes[-12:]),
        source="history_fallback",
        generation=max(1, (previous.generation if previous else 0) + 1),
        compacted_messages=compacted_messages,
        created_at=time.time(),
    )
    return merge_summaries(previous, candidate)


def extract_summary(content: Optional[str]) -> Optional[CompactSummary]:
    if not content or not is_compact_summary_message(content):
        return None
    try:
        return CompactSummary.from_json_text(content)
    except (CompactSummaryError, TypeError, ValueError, json.JSONDecodeError):
        return None


def is_compact_summary_message(content: Optional[str]) -> bool:
    text = str(content or "")
    return text.startswith(SUMMARY_PREFIX) and SUMMARY_MARKER_START in text


def summary_from_history(messages: Sequence[Message]) -> Optional[CompactSummary]:
    """Merge every valid compact record in history order."""
    merged: Optional[CompactSummary] = None
    for message in messages:
        summary = extract_summary(message.content)
        if summary is None:
            continue
        meta = message.provider_meta or {}
        summary = replace(
            summary,
            source=str(meta.get("compact_source", summary.source)),
            generation=max(
                1, int(meta.get("compact_generation", summary.generation))),
            compacted_messages=max(
                0, int(meta.get("compact_messages", summary.compacted_messages))),
        )
        merged = merge_summaries(merged, summary)
    return merged


def _model_contract_issues(raw: Mapping[str, Any]) -> list[str]:
    """Validate the model-facing JSON contract before coercing/bounding it."""
    issues: list[str] = []
    goal = raw.get("goal")
    if not isinstance(goal, str) or len(goal) > _MAX_GOAL_CHARS:
        issues.append("goal 必须是长度受限的字符串")

    for field in (
        "completed", "decisions", "open_issues", "next_steps",
        "critical_constraints",
    ):
        values = raw.get(field)
        if not isinstance(values, list):
            issues.append(f"{field} 必须是数组")
            continue
        if len(values) > _MAX_ITEMS:
            issues.append(f"{field} 超过 {_MAX_ITEMS} 项")
        if any(
            not isinstance(item, str) or len(item) > _MAX_ITEM_CHARS
            for item in values
        ):
            issues.append(f"{field}[] 必须是长度受限的字符串")

    files = raw.get("files")
    if not isinstance(files, list):
        issues.append("files 必须是数组")
    else:
        if len(files) > _MAX_ITEMS:
            issues.append(f"files 超过 {_MAX_ITEMS} 项")
        for index, item in enumerate(files):
            if not isinstance(item, Mapping):
                issues.append(f"files[{index}] 必须是 object")
                continue
            unexpected = sorted(set(item) - {"path", "status", "note"})
            if unexpected:
                issues.append(
                    f"files[{index}] 包含未声明字段: {', '.join(unexpected)}")
            path = item.get("path")
            status = item.get("status")
            note = item.get("note")
            if not isinstance(path, str) or not path.strip() or len(path) > _MAX_ITEM_CHARS:
                issues.append(f"files[{index}].path 必须是非空且长度受限的字符串")
            if status not in {"created", "modified", "read", "touched"}:
                issues.append(f"files[{index}].status 非法")
            if not isinstance(note, str) or len(note) > _MAX_ITEM_CHARS:
                issues.append(f"files[{index}].note 必须是长度受限的字符串")

    tests = raw.get("tests")
    if not isinstance(tests, list):
        issues.append("tests 必须是数组")
    else:
        if len(tests) > _MAX_ITEMS:
            issues.append(f"tests 超过 {_MAX_ITEMS} 项")
        for index, item in enumerate(tests):
            if not isinstance(item, Mapping):
                issues.append(f"tests[{index}] 必须是 object")
                continue
            unexpected = sorted(set(item) - {"name", "status", "detail"})
            if unexpected:
                issues.append(
                    f"tests[{index}] 包含未声明字段: {', '.join(unexpected)}")
            name = item.get("name")
            status = item.get("status")
            detail = item.get("detail")
            if not isinstance(name, str) or not name.strip() or len(name) > _MAX_ITEM_CHARS:
                issues.append(f"tests[{index}].name 必须是非空且长度受限的字符串")
            if status not in {"passed", "failed", "skipped", "unknown"}:
                issues.append(f"tests[{index}].status 非法")
            if not isinstance(detail, str) or len(detail) > _MAX_ITEM_CHARS:
                issues.append(f"tests[{index}].detail 必须是长度受限的字符串")
    return issues


def _extract_json_object(text: str) -> Mapping[str, Any]:
    value = str(text or "").strip()
    if SUMMARY_MARKER_START in value:
        start = value.find(SUMMARY_MARKER_START)
        end = value.rfind(SUMMARY_MARKER_END)
        if end < start:
            raise CompactSummaryError("摘要缺少 JSON 结束标记")
        value = value[start:end + len(SUMMARY_MARKER_END)]
    elif value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end < start:
        raise CompactSummaryError("摘要模型未返回 JSON object")
    try:
        raw = json.loads(value[start:end + 1])
    except json.JSONDecodeError as exc:
        raise CompactSummaryError(f"摘要 JSON 无法解析: {exc.msg}") from exc
    if not isinstance(raw, Mapping):
        raise CompactSummaryError("摘要 JSON 根节点必须是 object")
    if isinstance(raw.get("summary"), Mapping):
        raw = raw["summary"]
    return cast(Mapping[str, Any], raw)


def _string_tuple(value: Any, name: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise CompactSummaryError(f"{name} 必须是数组")
    values = list(values)
    if len(values) > _MAX_ITEMS:
        raise CompactSummaryError(f"{name} 超过 {_MAX_ITEMS} 项")
    result = []
    seen = set()
    for item in values:
        if not isinstance(item, (str, int, float)):
            raise CompactSummaryError(f"{name}[] 必须是字符串")
        text = _bounded(item, _MAX_ITEM_CHARS)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return tuple(result)


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompactSummaryError(f"{field} 必须是非空字符串")
    return _bounded(value, _MAX_ITEM_CHARS)


def _bounded(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _int_value(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise CompactSummaryError(f"{field} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CompactSummaryError(f"{field} 必须是整数") from exc


def _float_value(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise CompactSummaryError(f"{field} 必须是数字") from exc


def _merge_strings(*groups: Sequence[str]) -> Tuple[str, ...]:
    result: list[str] = []
    seen = set()
    for group in groups:
        for value in group:
            text = _bounded(value, _MAX_ITEM_CHARS)
            if text and text not in seen:
                result.append(text)
                seen.add(text)
    return tuple(result[-_MAX_ITEMS:])


def _looks_like_test(command: str, meta: Mapping[str, Any]) -> bool:
    lowered = command.lower()
    return any(part in lowered for part in _TEST_COMMAND_PARTS) or (
        "exit_code" in meta and "check" in lowered)


def _test_from_metadata(command: str, meta: Mapping[str, Any]) -> CompactTestStatus:
    exit_code = meta.get("exit_code")
    if meta.get("timed_out"):
        status = "failed"
        detail = "timeout"
    elif exit_code is None:
        status = "unknown"
        detail = "exit code unavailable"
    else:
        try:
            code = int(exit_code)
        except (TypeError, ValueError):
            status = "unknown"
            detail = "invalid exit code"
        else:
            status = "passed" if code == 0 else "failed"
            detail = f"exit {code}"
    name = command.splitlines()[0][:400]
    return CompactTestStatus(name=name, status=status, detail=detail)


def _plan_steps(content: str, meta: Mapping[str, Any]) -> list[str]:
    items = meta.get("items")
    statuses = meta.get("statuses")
    if isinstance(items, list):
        result = []
        for index, item in enumerate(items[:_MAX_ITEMS]):
            status = (
                str(statuses[index]) if isinstance(statuses, list)
                and index < len(statuses) else "pending")
            if status in {"pending", "in_progress"}:
                result.append(f"[{status}] {item}")
        return result
    return [line.strip("- ○▶✓ ") for line in content.splitlines() if line.strip()][:8]


def _completed_plan_steps(content: str, meta: Mapping[str, Any]) -> list[str]:
    items = meta.get("items")
    statuses = meta.get("statuses")
    if not isinstance(items, list):
        return []
    result = []
    for index, item in enumerate(items[:_MAX_ITEMS]):
        status = (
            str(statuses[index]).lower()
            if isinstance(statuses, list) and index < len(statuses)
            else "pending"
        )
        if status == "completed":
            result.append(_bounded(item, _MAX_ITEM_CHARS))
    return result


__all__ = [
    "COMPACT_SUMMARY_JSON_SCHEMA",
    "COMPACT_SUMMARY_VERSION",
    "CompactFile",
    "CompactSummary",
    "CompactSummaryError",
    "CompactTestStatus",
    "SUMMARY_MARKER_END",
    "SUMMARY_MARKER_START",
    "SUMMARY_PREFIX",
    "SUMMARY_V2_PREFIX",
    "build_history_fallback",
    "build_working_context_fallback",
    "extract_summary",
    "is_compact_summary_message",
    "merge_summaries",
    "summary_from_history",
]
