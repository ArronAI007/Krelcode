"""Stable-turn extraction rules for Auto Memory candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from ..core.messages import Message
from ..core.redact import SecretRedactor
from .memory_candidate_models import (
    MAX_CANDIDATE_CHARS,
    MemoryCandidateDraft,
    MemoryCandidateTrigger,
    MemoryEvidence,
    _meaningful_candidate_text,
    _now,
)
from .memory_models import MemoryType, coerce_memory_type
from .memory_utils import (
    extract_test_command,
    normalize_memory_text,
    sanitize_memory_text,
)


@dataclass(frozen=True)
class MemoryTurnSnapshot:
    user_input: str
    messages: tuple[Message, ...] = ()
    turn_id: str = ""
    session_id: str = ""


_EXPLICIT_REMEMBER_RE = re.compile(
    r"(?is)(?:请|帮我)?记住(?:一下|这一点|这点)?\s*[：:，,\-]?\s*(.+)"
    r"|remember(?:\s+that)?\s*[：:,\-]?\s*(.+)"
)
_CORRECTION_SENTENCE_RE = re.compile(
    r"(?is)(?:不是|并非).{1,120}?(?:而是|而是说)\s*[：:，,\-]?\s*(.+)"
)
_CORRECTION_PREFIX_RE = re.compile(
    r"(?is)^(?:(?:不对|错了|纠正|更正|应该是|应当是|实际上|其实是|"
    r"you\s+are\s+wrong|that'?s\s+wrong|correction|actually)"
    r"\s*[：:，,\-]?\s*)(.+)$"
)
_ARCHITECTURE_TERMS = (
    "架构", "分层", "模块", "解耦", "流水线", "入口层", "执行层",
    "architecture", "layered", "module", "decoupled", "pipeline",
    "entrypoint", "runtime layer",
)
_ARCHITECTURE_ASSERTIONS = (
    "采用", "使用", "分为", "分成", "由", "负责", "位于", "是",
    "uses", "uses a", "consists", "has", "is", "contains",
)
_TEMP_OR_UNVERIFIED = (
    "临时", "暂时", "今天", "这次", "本次", "刚刚", "可能", "也许",
    "待定", "以后再", "todo", "temporary", "for now", "this time",
    "one-off", "one time", "maybe", "might", "not sure",
)
_TASK_FACT_RE = re.compile(
    r"(?i)(测试命令|运行测试|入口文件|入口位于|项目使用|项目采用|"
    r"架构采用|模块负责|配置文件位于|依赖包含|test command|"
    r"entry ?point|project uses|architecture|configured with)"
)
_FORMAL_TEST_COMMAND_RE = re.compile(
    r"(?i)\b(?:pytest|py\.test|tox|nox|npm(?:\s+run)?\s+test|"
    r"pnpm\s+test|yarn\s+test|bun\s+test|cargo\s+test|go\s+test|"
    r"dotnet\s+test|mvn\s+test|gradlew?(?:\s+test)?|make\s+test|"
    r"ruff\s+check)\b"
)
_MEMORY_TYPE_PREFIX_RE = re.compile(
    r"^(user|feedback|project|reference)\s*:\s*", re.IGNORECASE)
_WHY_SECTION_RE = re.compile(
    r"(?is)(?:\*\*)?why(?:\*\*)?\s*:\s*(.*?)"
    r"(?=(?:\*\*)?how\s+to\s+apply(?:\*\*)?\s*:|$)"
)
_HOW_SECTION_RE = re.compile(
    r"(?is)(?:\*\*)?how\s+to\s+apply(?:\*\*)?\s*:\s*(.*)$"
)


class MemoryCandidateExtractor:
    """Extract only bounded, stable and evidence-backed memory candidates."""

    def __init__(
        self,
        *,
        redactor: Optional[SecretRedactor] = None,
        clock: Callable[[], str] = _now,
    ) -> None:
        self.redactor = redactor
        self.clock = clock

    def extract(self, turn: MemoryTurnSnapshot) -> tuple[MemoryCandidateDraft, ...]:
        user_input = str(turn.user_input or "").strip()
        if not user_input:
            return ()
        drafts: list[MemoryCandidateDraft] = []
        explicit_text = self._explicit_remember(user_input)
        if explicit_text:
            drafts.append(self._draft(
                explicit_text,
                MemoryCandidateTrigger.EXPLICIT_REMEMBER,
                0.95,
                turn,
                evidence_kind="explicit_user_request",
                excerpt=user_input,
            ))
        else:
            correction = self._user_correction(user_input)
            if correction:
                drafts.append(self._draft(
                    correction,
                    MemoryCandidateTrigger.USER_CORRECTION,
                    0.85,
                    turn,
                    evidence_kind="user_correction",
                    excerpt=user_input,
                    memory_type=MemoryType.FEEDBACK,
                ))

        architecture = self._architecture_pattern(user_input)
        if architecture:
            drafts.append(self._draft(
                architecture,
                MemoryCandidateTrigger.ARCHITECTURE_PATTERN,
                0.65,
                turn,
                evidence_kind="architecture_confirmation",
                excerpt=user_input,
                topic="architecture",
            ))

        drafts.extend(self._successful_test_commands(turn))
        completion = self._task_completion_fact(turn)
        if completion:
            drafts.append(self._draft(
                completion,
                MemoryCandidateTrigger.TASK_COMPLETION,
                0.65,
                turn,
                evidence_kind="task_completion",
                excerpt=completion,
            ))
        return tuple(item for item in drafts if _meaningful_candidate_text(item.text))

    def _draft(
        self,
        text: str,
        trigger: MemoryCandidateTrigger,
        confidence: float,
        turn: MemoryTurnSnapshot,
        *,
        evidence_kind: str,
        excerpt: str = "",
        tool_name: str = "",
        command: str = "",
        exit_code: Optional[int] = None,
        topic: str = "general",
        memory_type: MemoryType | str = MemoryType.PROJECT,
        why: str = "",
        how_to_apply: str = "",
    ) -> MemoryCandidateDraft:
        clean_text, prefix_type = self._type_prefix(text)
        normalized_type = prefix_type or coerce_memory_type(memory_type)
        without_structure, parsed_why, parsed_how = self._parse_structure(clean_text)
        safe_why = parsed_why or why or self._default_why(trigger)
        safe_how = parsed_how or how_to_apply or self._default_how(trigger)
        if normalized_type not in {MemoryType.FEEDBACK, MemoryType.PROJECT}:
            safe_why = parsed_why or why
            safe_how = parsed_how or how_to_apply
        evidence = MemoryEvidence(
            kind=evidence_kind,
            turn_id=turn.turn_id,
            session_id=turn.session_id,
            excerpt=excerpt,
            tool_name=tool_name,
            command=command,
            exit_code=exit_code,
            created_at=self.clock(),
        )
        return MemoryCandidateDraft(
            text=without_structure,
            trigger=trigger,
            confidence=confidence,
            evidence=(evidence,),
            topic=topic,
            memory_type=normalized_type,
            why=safe_why,
            how_to_apply=safe_how,
        )

    @staticmethod
    def _type_prefix(text: str) -> tuple[str, Optional[MemoryType]]:
        raw = str(text or "").strip()
        match = _MEMORY_TYPE_PREFIX_RE.match(raw)
        if match is None:
            return raw, None
        return raw[match.end():].strip(), MemoryType(match.group(1).lower())

    @staticmethod
    def _parse_structure(text: str) -> tuple[str, str, str]:
        raw = str(text or "").strip()
        why_match = _WHY_SECTION_RE.search(raw)
        how_match = _HOW_SECTION_RE.search(raw)
        if why_match is None and how_match is None:
            return raw, "", ""
        first = min(
            match.start() for match in (why_match, how_match)
            if match is not None)
        body = raw[:first].strip() or raw
        why = " ".join(why_match.group(1).split()) if why_match else ""
        how = " ".join(how_match.group(1).split()) if how_match else ""
        return body, why, how

    @staticmethod
    def _default_why(trigger: MemoryCandidateTrigger) -> str:
        return {
            MemoryCandidateTrigger.EXPLICIT_REMEMBER:
                "用户明确要求将这一稳定事实用于后续工作。",
            MemoryCandidateTrigger.USER_CORRECTION:
                "用户纠正了此前的错误理解，避免后续重复犯错。",
            MemoryCandidateTrigger.SUCCESSFUL_TEST_REUSE:
                "该验证命令已在当前项目中成功执行并可重复使用。",
            MemoryCandidateTrigger.ARCHITECTURE_PATTERN:
                "用户多次确认了这一项目级约束或设计意图。",
            MemoryCandidateTrigger.TASK_COMPLETION:
                "任务完成时已确认该事实具有跨任务复用价值。",
        }[trigger]

    @staticmethod
    def _default_how(trigger: MemoryCandidateTrigger) -> str:
        return {
            MemoryCandidateTrigger.EXPLICIT_REMEMBER:
                "仅在相关任务中作为 Evidence 使用，并结合当前代码与用户请求验证。",
            MemoryCandidateTrigger.USER_CORRECTION:
                "遇到同类决策时先应用该纠正；若工作区事实已变化，以当前事实为准。",
            MemoryCandidateTrigger.SUCCESSFUL_TEST_REUSE:
                "需要同类验证时优先复用该命令，并确认工作目录、依赖和参数仍然匹配。",
            MemoryCandidateTrigger.ARCHITECTURE_PATTERN:
                "设计或修改相关模块时先检查该约束是否仍成立，不得用它替代代码检查。",
            MemoryCandidateTrigger.TASK_COMPLETION:
                "仅在后续任务与该事实直接相关时引用，并重新验证路径、符号和行为。",
        }[trigger]

    @staticmethod
    def _explicit_remember(text: str) -> str:
        match = _EXPLICIT_REMEMBER_RE.search(text)
        if match is None:
            return ""
        candidate = next((group for group in match.groups() if group), "")
        return MemoryCandidateExtractor._stable(candidate)

    @staticmethod
    def _user_correction(text: str) -> str:
        match = _CORRECTION_SENTENCE_RE.search(text)
        if match is not None:
            return MemoryCandidateExtractor._stable(text.strip())
        match = _CORRECTION_PREFIX_RE.match(text.strip())
        if match is None:
            return ""
        return MemoryCandidateExtractor._stable(match.group(1))

    @staticmethod
    def _architecture_pattern(text: str) -> str:
        candidate = MemoryCandidateExtractor._stable(text)
        if not candidate:
            return ""
        lowered = candidate.casefold()
        if not any(term in lowered for term in _ARCHITECTURE_TERMS):
            return ""
        if not any(term in lowered for term in _ARCHITECTURE_ASSERTIONS):
            return ""
        return candidate

    @staticmethod
    def _stable(text: str) -> str:
        value = " ".join(str(text).strip().split())
        if len(value) < 6 or len(value) > MAX_CANDIDATE_CHARS:
            return ""
        if value.rstrip().endswith(("?", "？")):
            return ""
        lowered = value.casefold()
        if any(marker in lowered for marker in _TEMP_OR_UNVERIFIED):
            return ""
        if lowered.startswith(("如果 ", "假如 ", "if ", "what if ")):
            return ""
        return value

    def _successful_test_commands(
        self,
        turn: MemoryTurnSnapshot,
    ) -> list[MemoryCandidateDraft]:
        drafts: list[MemoryCandidateDraft] = []
        seen: set[str] = set()
        for message in turn.messages:
            if message.role != "tool" or message.tool_is_error:
                continue
            metadata = message.tool_meta or {}
            if metadata.get("exit_code") != 0:
                continue
            command = str(metadata.get("command") or "")
            if not command or not _FORMAL_TEST_COMMAND_RE.search(command):
                continue
            canonical = extract_test_command(command) or normalize_memory_text(command)
            if canonical in seen:
                continue
            seen.add(canonical)
            drafts.append(self._draft(
                f"验证命令：{command}",
                MemoryCandidateTrigger.SUCCESSFUL_TEST_REUSE,
                0.70,
                turn,
                evidence_kind="successful_test_command",
                excerpt=f"exit_code=0; command={command}",
                tool_name=message.tool_name or "run_command",
                command=command,
                exit_code=0,
                topic="test_command",
            ))
        return drafts

    @staticmethod
    def _task_completion_fact(turn: MemoryTurnSnapshot) -> str:
        assistant_texts = [
            message.content for message in turn.messages
            if message.role == "assistant" and message.content
        ]
        if not assistant_texts:
            return ""
        final = " ".join(str(assistant_texts[-1]).split())
        if "[krelcode]" in final.casefold():
            return ""
        pieces = re.split(
            r"(?<=[。！？.!?])(?:\s+|(?=[^\s]))|\n+", final)
        for piece in pieces:
            candidate = MemoryCandidateExtractor._stable(piece)
            if not candidate or not _TASK_FACT_RE.search(candidate):
                continue
            if len(candidate) > 240:
                continue
            return candidate
        return ""


__all__ = ["MemoryCandidateExtractor", "MemoryTurnSnapshot"]
