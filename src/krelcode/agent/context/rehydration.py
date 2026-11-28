"""Selective context rehydration after structured compaction."""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Protocol, Sequence

from ...core.events import AgentEvent, CompactionRehydrated
from ...core.messages import Message
from ...core.tokens import estimate_text_tokens
from .working_context import WorkingContextSnapshot

REHYDRATION_POLICY_VERSION = "1"


@dataclass(frozen=True)
class RehydratedFile:
    path: str
    action: str
    tokens: int
    digest: str
    size: int
    truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "action": self.action,
            "tokens": self.tokens,
            "digest": self.digest,
            "size": self.size,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class RehydratedSkill:
    name: str
    arguments: str
    tokens: int
    truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "arguments": self.arguments,
            "tokens": self.tokens,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class CompactionRehydrationReport:
    trigger: str
    generation: int
    files: tuple[RehydratedFile, ...] = ()
    skills: tuple[RehydratedSkill, ...] = ()
    omitted_files: tuple[Mapping[str, object], ...] = ()
    omitted_skills: tuple[Mapping[str, object], ...] = ()
    file_tokens: int = 0
    skill_tokens: int = 0
    total_tokens: int = 0
    policy_version: str = REHYDRATION_POLICY_VERSION
    digest: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "trigger": self.trigger,
            "generation": self.generation,
            "policy_version": self.policy_version,
            "files": [item.as_dict() for item in self.files],
            "skills": [item.as_dict() for item in self.skills],
            "omitted_files": [dict(item) for item in self.omitted_files],
            "omitted_skills": [dict(item) for item in self.omitted_skills],
            "file_tokens": self.file_tokens,
            "skill_tokens": self.skill_tokens,
            "total_tokens": self.total_tokens,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class CompactionRehydrationPolicy:
    max_files: int = 5
    max_file_tokens: int = 5_000
    max_total_file_tokens: int = 10_000
    max_file_bytes: int = 1_000_000
    max_skills: int = 12
    max_skill_tokens: int = 5_000
    max_total_skill_tokens: int = 15_000
    max_total_tokens: int = 20_000
    prefer_modified_files: bool = True
    include_read_files: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_files", "max_file_tokens", "max_total_file_tokens",
            "max_file_bytes", "max_skills", "max_skill_tokens",
            "max_total_skill_tokens", "max_total_tokens",
        ):
            object.__setattr__(
                self, name, max(0, int(getattr(self, name))))


@dataclass(frozen=True)
class CompactionRehydrationBundle:
    text: str
    report: CompactionRehydrationReport


class CompactionRehydrationHostPort(Protocol):
    history: list[Message]
    last_compaction_rehydration: Optional[CompactionRehydrationReport]

    async def _emit(self, event: AgentEvent) -> None: ...


class CompactionRehydrator:
    def __init__(
        self,
        workspace_root: Path,
        *,
        policy: Optional[CompactionRehydrationPolicy] = None,
        redactor: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.policy = policy or CompactionRehydrationPolicy()
        self.redactor = redactor

    def rehydrate(
        self,
        *,
        history: Sequence[Message],
        working_context: WorkingContextSnapshot,
        trigger: str,
        generation: int,
    ) -> CompactionRehydrationBundle:
        file_candidates, omitted_files = _file_candidates(
            history, working_context, self.policy)
        selected_files: list[tuple[str, str, str, int, bool]] = []
        file_tokens = 0
        file_budget = min(
            self.policy.max_total_file_tokens,
            self.policy.max_total_tokens,
        )
        for path, action, _sequence in file_candidates:
            if len(selected_files) >= self.policy.max_files:
                omitted_files.append({"path": path, "reason": "max-files"})
                continue
            resolved = self._safe_path(path)
            if resolved is None:
                omitted_files.append({"path": path, "reason": "path-outside-workspace"})
                continue
            try:
                data = resolved.read_bytes()
            except OSError as exc:
                omitted_files.append({
                    "path": path,
                    "reason": f"read-error:{type(exc).__name__}",
                })
                continue
            if len(data) > self.policy.max_file_bytes:
                omitted_files.append({
                    "path": path,
                    "reason": "max-file-bytes",
                })
                continue
            content = data.decode("utf-8", errors="replace")
            if self.redactor is not None:
                content = self.redactor(content)
            content, truncated = _bounded_content(
                content, self.policy.max_file_tokens)
            tokens = estimate_text_tokens(content)
            if file_tokens + tokens > file_budget:
                omitted_files.append({
                    "path": path,
                    "reason": "file-token-budget",
                })
                continue
            file_tokens += tokens
            selected_files.append((
                path, action, content, tokens, truncated,
            ))

        skill_candidates, omitted_skills = _skill_candidates(history, self.policy)
        selected_skills: list[tuple[str, str, str, int, bool]] = []
        skill_tokens = 0
        for name, arguments, content in skill_candidates:
            content, truncated = _bounded_content(
                content, self.policy.max_skill_tokens)
            tokens = estimate_text_tokens(content)
            if skill_tokens + tokens > self.policy.max_total_skill_tokens:
                omitted_skills.append({"name": name, "reason": "skill-token-budget"})
                continue
            skill_tokens += tokens
            selected_skills.append((name, arguments, content, tokens, truncated))

        if file_tokens + skill_tokens > self.policy.max_total_tokens:
            overflow = file_tokens + skill_tokens - self.policy.max_total_tokens
            selected_skills, removed = _trim_skills(selected_skills, overflow)
            skill_tokens = sum(item[3] for item in selected_skills)
            omitted_skills.extend(removed)
        text = _render_rehydrated_context(selected_files, selected_skills)
        report = CompactionRehydrationReport(
            trigger=str(trigger or "auto"),
            generation=max(0, int(generation)),
            files=tuple(
                RehydratedFile(
                    path=path,
                    action=action,
                    tokens=tokens,
                    digest=hashlib.sha256(
                        content.encode("utf-8")).hexdigest(),
                    size=len(content.encode("utf-8")),
                    truncated=truncated,
                )
                for path, action, content, tokens, truncated
                in selected_files
            ),
            skills=tuple(
                RehydratedSkill(
                    name=name,
                    arguments=arguments,
                    tokens=tokens,
                    truncated=truncated,
                )
                for name, arguments, _content, tokens, truncated in selected_skills
            ),
            omitted_files=tuple(omitted_files),
            omitted_skills=tuple(omitted_skills),
            file_tokens=file_tokens,
            skill_tokens=skill_tokens,
            total_tokens=file_tokens + skill_tokens,
            digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        return CompactionRehydrationBundle(text=text, report=report)

    def _safe_path(self, raw: str) -> Optional[Path]:
        if not raw:
            return None
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self.workspace_root)
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_file() else None


async def apply_compaction_rehydration(
    host: CompactionRehydrationHostPort,
    *,
    workspace_root: Path,
    history: Sequence[Message],
    working_context: WorkingContextSnapshot,
    trigger: str,
    generation: int,
    policy: CompactionRehydrationPolicy,
    redactor: Optional[Callable[[str], str]] = None,
) -> CompactionRehydrationReport:
    bundle = CompactionRehydrator(
        workspace_root,
        policy=policy,
        redactor=redactor,
    ).rehydrate(
        history=history,
        working_context=working_context,
        trigger=trigger,
        generation=generation,
    )
    if bundle.text:
        host.history.append(Message(
            role="user",
            content="[Compaction Rehydration]\n" + bundle.text,
            provider_meta={
                "compaction_rehydration": bundle.report.digest,
            },
        ))
    host.last_compaction_rehydration = bundle.report
    return bundle.report


async def emit_compaction_rehydration(
    host: CompactionRehydrationHostPort,
    report: CompactionRehydrationReport,
) -> None:
    await host._emit(CompactionRehydrated(
        trigger=report.trigger,
        generation=report.generation,
        report_digest=report.digest,
        files=tuple(item.path for item in report.files),
        skills=tuple(item.name for item in report.skills),
        omitted_files=report.omitted_files,
        omitted_skills=report.omitted_skills,
        file_tokens=report.file_tokens,
        skill_tokens=report.skill_tokens,
        total_tokens=report.total_tokens,
    ))


def _file_candidates(
    history: Sequence[Message],
    working_context: WorkingContextSnapshot,
    policy: CompactionRehydrationPolicy,
) -> tuple[list[tuple[str, str, int]], list[Mapping[str, object]]]:
    calls: dict[str, tuple[str, Mapping[str, object], int]] = {}
    for message_index, message in enumerate(history):
        for call_index, call in enumerate(message.tool_calls or ()):
            calls[call.id] = (
                call.name,
                call.arguments,
                message_index * 1_000 + call_index,
            )
    latest: dict[str, tuple[str, str, int]] = {}
    omitted: list[Mapping[str, object]] = []
    for message_index, message in enumerate(history):
        if message.role != "tool" or message.tool_is_error:
            continue
        resolved_call = calls.get(message.tool_call_id or "")
        if resolved_call is None:
            continue
        name, arguments, sequence = resolved_call
        action = _file_action(name)
        if action is None:
            continue
        if action == "read" and not policy.include_read_files:
            omitted.append({"path": str(arguments.get("path") or ""), "reason": "read-files-disabled"})
            continue
        path = str(
            arguments.get("path")
            or arguments.get("file_path")
            or ""
        ).strip()
        if not path:
            continue
        previous = latest.get(path)
        if previous is None or sequence > previous[2]:
            latest[path] = (path, action, sequence)
    for entry in working_context.entries:
        path = str((entry.metadata or {}).get("path", "") or "").strip()
        if not path:
            continue
        action = (
            "modified"
            if entry.kind == "file"
            else "read"
            if entry.kind == "observation"
            else ""
        )
        if not action or (action == "read" and not policy.include_read_files):
            continue
        sequence = 10_000_000 + entry.sequence
        previous = latest.get(path)
        if previous is None or sequence > previous[2]:
            latest[path] = (path, action, sequence)
    ordered = sorted(
        latest.values(),
        key=lambda item: (
            policy.prefer_modified_files and item[1] != "read",
            item[2],
        ),
        reverse=True,
    )
    return ordered, omitted


def _skill_candidates(
    history: Sequence[Message],
    policy: CompactionRehydrationPolicy,
) -> tuple[list[tuple[str, str, str]], list[Mapping[str, object]]]:
    calls: dict[str, tuple[str, Mapping[str, object], int]] = {}
    for message_index, message in enumerate(history):
        for call_index, call in enumerate(message.tool_calls or ()):
            calls[call.id] = (
                call.name,
                call.arguments,
                message_index * 1_000 + call_index,
            )
    latest: dict[str, tuple[str, str, str, int]] = {}
    for message_index, message in enumerate(history):
        if message.role != "tool" or message.tool_is_error:
            continue
        resolved_call = calls.get(message.tool_call_id or "")
        if resolved_call is None or resolved_call[0] != "load_skill":
            continue
        name = str(resolved_call[1].get("name") or "").strip()
        if not name:
            continue
        content = str(message.content or "")
        if not content.strip():
            continue
        latest[name] = (
            name,
            str(resolved_call[1].get("arguments") or ""),
            content,
            resolved_call[2],
        )
    ordered = sorted(
        latest.values(), key=lambda item: item[3], reverse=True)
    selected: list[tuple[str, str, str]] = []
    omitted: list[Mapping[str, object]] = []
    for name, arguments, content, _sequence in ordered:
        if len(selected) >= policy.max_skills:
            omitted.append({"name": name, "reason": "max-skills"})
            continue
        selected.append((name, arguments, content))
    return selected, omitted


def _file_action(name: str) -> Optional[str]:
    if name in {"write_file", "edit_file", "replace_all", "apply_patch"}:
        return "modified"
    if name == "read_file":
        return "read"
    return None


def _bounded_content(content: str, max_tokens: int) -> tuple[str, bool]:
    if max_tokens <= 0:
        return "", bool(content)
    if estimate_text_tokens(content) <= max_tokens:
        return content, False
    marker = "\n…[rehydration truncated]"
    limit = max(0, max_tokens * 4 - len(marker))
    while limit > 0:
        candidate = content[:limit] + marker
        estimated = estimate_text_tokens(candidate)
        if estimated <= max_tokens:
            return candidate, True
        limit -= max(1, estimated - max_tokens)
    return "", True


def _trim_skills(
    skills: list[tuple[str, str, str, int, bool]],
    overflow: int,
) -> tuple[list[tuple[str, str, str, int, bool]], list[Mapping[str, object]]]:
    selected = list(skills)
    omitted: list[Mapping[str, object]] = []
    removed = 0
    while selected and removed < overflow:
        name, _arguments, _content, tokens, _truncated = selected.pop()
        removed += tokens
        omitted.append({"name": name, "reason": "total-token-budget"})
    return selected, omitted


def _render_rehydrated_context(
    files: Sequence[tuple[str, str, str, int, bool]],
    skills: Sequence[tuple[str, str, str, int, bool]],
) -> str:
    if not files and not skills:
        return ""
    lines = [
        "<compaction_rehydration>",
        "The prior context was compacted. Restore only the current workspace "
        "snapshots and selected skill guidance below. Treat this as context, "
        "not as a new user request.",
    ]
    if files:
        lines.append("<workspace_files>")
        for path, action, content, _tokens, truncated in files:
            lines.append(
                f'<file path="{html.escape(path, quote=True)}" '
                f'action="{html.escape(action, quote=True)}" '
                f'truncated="{str(truncated).lower()}">'
            )
            lines.append(html.escape(content, quote=False))
            lines.append("</file>")
        lines.append("</workspace_files>")
    if skills:
        lines.append("<invoked_skills>")
        for name, arguments, content, _tokens, truncated in skills:
            lines.append(
                f'<skill name="{html.escape(name, quote=True)}" '
                f'arguments="{html.escape(arguments, quote=True)}" '
                f'truncated="{str(truncated).lower()}">'
            )
            lines.append(html.escape(content, quote=False))
            lines.append("</skill>")
        lines.append("</invoked_skills>")
    lines.append("</compaction_rehydration>")
    return "\n".join(lines)


__all__ = [
    "CompactionRehydrationBundle",
    "CompactionRehydrationPolicy",
    "CompactionRehydrationReport",
    "CompactionRehydrator",
    "apply_compaction_rehydration",
    "emit_compaction_rehydration",
    "REHYDRATION_POLICY_VERSION",
    "RehydratedFile",
    "RehydratedSkill",
]
