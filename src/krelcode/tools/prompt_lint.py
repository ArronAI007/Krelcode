"""Deterministic, content-light lint for model-facing tool Prompts.

The lint layer is intentionally independent from AgentLoop and provider
adapters.  It validates the canonical ``ToolPromptSpec`` contract, including
JSON Schema consistency and model-facing examples, while returning only field
names, issue codes and counts that are safe for logs and slash-command output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from .prompt import ToolPromptSpec
from .schema import validate_tool_arguments


MAX_DESCRIPTION_CHARS = 4_000
MAX_INSTRUCTIONS_CHARS = 20_000
MAX_EXAMPLES = 8
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret"
        r"|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}"),
)


class PromptLintSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class PromptLintIssue:
    """One stable diagnostic without the offending Prompt content."""

    code: str
    severity: PromptLintSeverity
    tool: str
    field: str
    message: str
    hint: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "severity", PromptLintSeverity(self.severity))
        object.__setattr__(self, "code", _safe_label(self.code, 32))
        object.__setattr__(self, "tool", _safe_label(self.tool, 80))
        object.__setattr__(self, "field", _safe_label(self.field, 240))
        object.__setattr__(self, "message", _safe_label(self.message, 500))
        object.__setattr__(self, "hint", _safe_label(self.hint, 300))

    def as_record(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "tool": self.tool,
            "field": self.field,
            "message": self.message,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class ToolPromptLintReport:
    """Sorted lint result suitable for CLI, SDK and content-light logs."""

    issues: Tuple[PromptLintIssue, ...]
    tool_count: int
    error_count: int
    warning_count: int
    info_count: int
    content_light: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", _sort_issues(self.issues))

    @property
    def clean(self) -> bool:
        return not self.error_count and not self.warning_count

    def as_record(self) -> dict[str, object]:
        return {
            "tool_count": self.tool_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "info_count": self.info_count,
            "content_light": self.content_light,
            "issues": [issue.as_record() for issue in self.issues],
        }

    def lines(self, *, limit: int = 100) -> Tuple[str, ...]:
        effective_limit = max(1, limit)
        lines = [
            "  [tool-prompt-lint] "
            f"tools={self.tool_count} · errors={self.error_count}"
            f" · warnings={self.warning_count} · info={self.info_count}"
            " · content-light=true",
        ]
        for issue in self.issues[:effective_limit]:
            hint = f" · hint={issue.hint}" if issue.hint else ""
            lines.append(
                f"  [{issue.severity.value.upper()}] {issue.tool}"
                f" · {issue.field} · {issue.code}: {issue.message}{hint}")
        remaining = len(self.issues) - effective_limit
        if remaining > 0:
            lines.append(f"  …另有 {remaining} 条 lint 问题")
        return tuple(lines)


_SEVERITY_ORDER = {
    PromptLintSeverity.ERROR: 0,
    PromptLintSeverity.WARNING: 1,
    PromptLintSeverity.INFO: 2,
}
_OWNED_SOURCES = frozenset({"builtin", "skill", "subagent", "canonical"})


def _sort_issues(
    issues: Iterable[PromptLintIssue],
) -> Tuple[PromptLintIssue, ...]:
    return tuple(sorted(
        issues,
        key=lambda item: (
            _SEVERITY_ORDER[item.severity],
            item.tool,
            item.field,
            item.code,
            item.message,
        ),
    ))


def lint_tool_prompt_spec(
    spec: ToolPromptSpec,
    *,
    source: Optional[str] = None,
) -> Tuple[PromptLintIssue, ...]:
    """Validate one canonical Prompt spec and return stable diagnostics."""
    if not isinstance(spec, ToolPromptSpec):
        raise TypeError("spec 必须是 ToolPromptSpec")
    resolved_source = str(source or spec.source or "").strip().lower()
    tool = spec.name
    issues: list[PromptLintIssue] = []

    def add(
        code: str,
        severity: PromptLintSeverity,
        field: str,
        message: str,
        hint: str = "",
    ) -> None:
        issues.append(PromptLintIssue(
            code=code,
            severity=severity,
            tool=tool,
            field=field,
            message=message,
            hint=hint,
        ))

    if not _TOOL_NAME.fullmatch(spec.name):
        add(
            "TPL001", PromptLintSeverity.ERROR, "name",
            "工具名必须是 1-64 位小写 snake_case。",
            "Use a stable provider-safe identifier.",
        )
    if not spec.instructions.strip():
        add(
            "TPL002", PromptLintSeverity.ERROR, "instructions",
            "缺少工具操作指令。",
            "Explain operating discipline, safety and verification.",
        )
    if not spec.when_to_use:
        add(
            "TPL003", PromptLintSeverity.ERROR, "when_to_use",
            "缺少适用场景说明。",
        )
    if not spec.when_not_to_use:
        add(
            "TPL004", PromptLintSeverity.ERROR, "when_not_to_use",
            "缺少禁用/改选其他工具的场景。",
        )
    if len(spec.description) > MAX_DESCRIPTION_CHARS:
        add(
            "TPL005", PromptLintSeverity.WARNING, "description",
            f"description 超过 {MAX_DESCRIPTION_CHARS} 字符。",
            "Keep the provider description concise.",
        )
    if len(spec.instructions) > MAX_INSTRUCTIONS_CHARS:
        add(
            "TPL006", PromptLintSeverity.WARNING, "instructions",
            f"instructions 超过 {MAX_INSTRUCTIONS_CHARS} 字符。",
            "Move reusable workflow detail to a discoverable surface.",
        )
    if _normalized_many(spec.when_to_use) == _normalized_many(spec.when_not_to_use):
        add(
            "TPL007", PromptLintSeverity.WARNING, "when_to_use/when_not_to_use",
            "适用与不适用场景完全相同，无法帮助模型分流。",
        )

    schema_issues, properties, required = _lint_schema_shape(spec)
    for issue in schema_issues:
        issues.append(PromptLintIssue(
            code=issue[0], severity=issue[1], tool=tool,
            field=issue[2], message=issue[3], hint=issue[4]))

    if not spec.examples:
        severity = (
            PromptLintSeverity.ERROR
            if resolved_source in _OWNED_SOURCES
            else PromptLintSeverity.WARNING
        )
        add(
            "TPL008", severity, "examples",
            "没有模型可见调用示例。",
            "Add at least one schema-valid normal-path example.",
        )
    elif len(spec.examples) > MAX_EXAMPLES:
        add(
            "TPL009", PromptLintSeverity.INFO, "examples",
            f"示例数量 {len(spec.examples)} 超过建议上限 {MAX_EXAMPLES}。",
            "Keep examples focused on distinct call patterns.",
        )

    seen_inputs: dict[str, int] = {}
    seen_titles: dict[str, int] = {}
    covered_optional: set[str] = set()
    prose_pairs: set[tuple[str, str]] = set()
    for index, example in enumerate(spec.examples):
        field = f"examples[{index}]"
        input_hash = example.input_hash
        if input_hash in seen_inputs:
            add(
                "TPL010", PromptLintSeverity.ERROR, f"{field}.input",
                f"example input 与 examples[{seen_inputs[input_hash]}] 重复。",
            )
        else:
            seen_inputs[input_hash] = index
        normalized_title = " ".join(example.title.casefold().split())
        if normalized_title in seen_titles:
            add(
                "TPL011", PromptLintSeverity.WARNING, f"{field}.title",
                f"example title 与 examples[{seen_titles[normalized_title]}] 重复。",
            )
        else:
            seen_titles[normalized_title] = index
        if not example.description:
            add(
                "TPL012", PromptLintSeverity.WARNING, f"{field}.description",
                "example 缺少 explanation，模型难以理解示例意图。",
            )
        elif len(example.description) > 2_000:
            add(
                "TPL013", PromptLintSeverity.WARNING, f"{field}.description",
                "example description 超过 2000 字符。",
            )

        unknown = _unknown_fields(example.input, properties, spec.parameters)
        for key in unknown:
            add(
                "TPL014", PromptLintSeverity.ERROR, f"{field}.input.{key}",
                "example 包含 schema 未声明的字段。",
                "Remove the field or declare it in properties.",
            )
        for error in validate_tool_arguments(spec.parameters, dict(example.input)):
            add(
                "TPL015", PromptLintSeverity.ERROR, f"{field}.input{error.path[1:]}",
                "example 不符合参数 Schema 的 required/type/enum/range 约束。",
            )
        if _contains_secret_shape({
            "title": example.title,
            "description": example.description,
            "input": example.input,
        }):
            add(
                "TPL016", PromptLintSeverity.ERROR, f"{field}.input",
                "example 含疑似真实密钥或凭据的字符串。",
                "Use non-secret placeholders and environment-variable indirection.",
            )

        prose_key = (example.title, example.description)
        if prose_key in prose_pairs:
            add(
                "TPL017", PromptLintSeverity.WARNING, f"{field}",
                "多个 example 只变化 input，title/description 未说明差异。",
            )
        prose_pairs.add(prose_key)
        for key in example.input:
            if str(key) not in required and str(key) in properties:
                covered_optional.add(str(key))

    optional = {
        str(name) for name in properties
        if str(name) not in required
    }
    missing_optional = sorted(optional - covered_optional)
    if spec.examples and missing_optional:
        add(
            "TPL018", PromptLintSeverity.INFO, "examples",
            "部分可选参数未出现在示例中: " + ", ".join(missing_optional),
            "Add variants only when they teach a distinct call pattern.",
        )

    missing_required = sorted(
        str(name) for name in required
        if spec.examples and not any(str(name) in item.input for item in spec.examples)
    )
    for name in missing_required:
        add(
            "TPL019", PromptLintSeverity.ERROR, f"examples.input.{name}",
            "必填参数未出现在任何 example 中。",
        )
    return _sort_issues(issues)


def lint_tool_registry(
    registry: object,
    *,
    tool_names: Optional[Iterable[str]] = None,
) -> ToolPromptLintReport:
    """Lint a ToolRegistry-like object with deterministic tool ordering."""
    entries = getattr(registry, "prompt_entries", None)
    if not callable(entries):
        raise TypeError("registry 必须提供 prompt_entries()")
    selected = (
        {str(name).strip() for name in tool_names}
        if tool_names is not None else None
    )
    issues: list[PromptLintIssue] = []
    checked = 0
    for _tool, descriptor, spec in entries():
        if selected is not None and spec.name not in selected:
            continue
        source = getattr(descriptor, "source", "")
        source_value = str(getattr(source, "value", source) or "")
        issues.extend(lint_tool_prompt_spec(spec, source=source_value))
        checked += 1
    counts = {
        severity: sum(issue.severity == severity for issue in issues)
        for severity in PromptLintSeverity
    }
    return ToolPromptLintReport(
        issues=tuple(issues),
        tool_count=checked,
        error_count=counts[PromptLintSeverity.ERROR],
        warning_count=counts[PromptLintSeverity.WARNING],
        info_count=counts[PromptLintSeverity.INFO],
    )


def _lint_schema_shape(
    spec: ToolPromptSpec,
) -> tuple[
    list[tuple[str, PromptLintSeverity, str, str, str]],
    Mapping[str, Any],
    set[str],
]:
    issues: list[tuple[str, PromptLintSeverity, str, str, str]] = []
    schema = spec.parameters
    if schema.get("type") != "object":
        issues.append((
            "TPL020", PromptLintSeverity.ERROR, "parameters.type",
            "工具参数 schema 顶层 type 必须为 object。",
            "Provider tool arguments are always an object.",
        ))
    raw_properties = schema.get("properties", {})
    if raw_properties is None:
        raw_properties = {}
    if not isinstance(raw_properties, Mapping):
        issues.append((
            "TPL021", PromptLintSeverity.ERROR, "parameters.properties",
            "parameters.properties 必须是对象。",
            "",
        ))
        properties: Mapping[str, Any] = {}
    else:
        properties = raw_properties
        invalid = sorted(
            str(name) for name, child in properties.items()
            if not isinstance(child, (Mapping, bool))
        )
        if invalid:
            issues.append((
                "TPL022", PromptLintSeverity.ERROR, "parameters.properties",
                "以下字段的 Schema 不是对象/布尔值: " + ", ".join(invalid),
                "",
            ))

    raw_required = schema.get("required", [])
    if not isinstance(raw_required, (list, tuple)):
        issues.append((
            "TPL023", PromptLintSeverity.ERROR, "parameters.required",
            "parameters.required 必须是字符串数组。",
            "",
        ))
        required: set[str] = set()
    else:
        invalid_required = sorted(
            str(item) for item in raw_required if not isinstance(item, str))
        if invalid_required:
            issues.append((
                "TPL024", PromptLintSeverity.ERROR, "parameters.required",
                "required 只能包含字符串字段名。",
                "",
            ))
        required_names = [str(item) for item in raw_required if isinstance(item, str)]
        duplicates = sorted({
            name for name in required_names if required_names.count(name) > 1
        })
        if duplicates:
            issues.append((
                "TPL025", PromptLintSeverity.ERROR, "parameters.required",
                "required 包含重复字段: " + ", ".join(duplicates),
                "",
            ))
        required = set(required_names)
        unknown_required = sorted(required - {str(name) for name in properties})
        if unknown_required:
            issues.append((
                "TPL026", PromptLintSeverity.ERROR, "parameters.required",
                "required 引用了未声明字段: " + ", ".join(unknown_required),
                "Declare each required field in properties.",
            ))
    return issues, properties, required


def _unknown_fields(
    values: Mapping[str, object],
    properties: Mapping[str, Any],
    schema: Mapping[str, object],
) -> list[str]:
    known = {str(name) for name in properties}
    patterns = schema.get("patternProperties")
    patterns = patterns if isinstance(patterns, Mapping) else {}
    unknown: list[str] = []
    for raw_name in values:
        name = str(raw_name)
        if name in known:
            continue
        if any(_pattern_matches(pattern, name) for pattern in patterns):
            continue
        unknown.append(name)
    return sorted(unknown)


def _pattern_matches(pattern: object, value: str) -> bool:
    try:
        return re.search(str(pattern), value) is not None
    except re.error:
        return False


def _contains_secret_shape(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_secret_shape(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_shape(item) for item in value)
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in _SECRET_SHAPES)


def _normalized_many(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        " ".join(str(value).casefold().split())
        for value in values
        if str(value).strip()
    )


def _safe_label(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


__all__ = [
    "PromptLintIssue",
    "PromptLintSeverity",
    "ToolPromptLintReport",
    "lint_tool_prompt_spec",
    "lint_tool_registry",
]
