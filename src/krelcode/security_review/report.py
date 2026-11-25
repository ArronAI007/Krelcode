"""Structured, machine-verifiable Security Review report contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from ..agent.prompt.prompt_surface_contract import (
    PromptSurfaceContract,
    PromptSurfaceSchemaError,
    validate_prompt_surface_schema,
)


SECURITY_REVIEW_SCHEMA_VERSION = "security-review-report-v1"
SECURITY_REVIEW_SEVERITIES = ("critical", "high", "medium", "low", "info")
SECURITY_REVIEW_CONFIDENCES = ("high", "medium", "low")
SECURITY_REVIEW_VERDICTS = ("pass", "needs_review", "block")
SECURITY_REVIEW_CATEGORIES = (
    "authentication",
    "authorization",
    "injection",
    "secrets",
    "path_traversal",
    "unsafe_execution",
    "deserialization",
    "ssrf",
    "cryptography",
    "data_exposure",
    "supply_chain",
    "configuration",
    "other",
)

_SEVERITY_RANK = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
_FAIL_ON_RANK = {
    "never": None,
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
_FINDING_ID_RE = re.compile(r"^SEC-[0-9]{3}$")
_CWE_RE = re.compile(r"^CWE-[0-9]{1,5}$", re.IGNORECASE)

SECURITY_REVIEW_REPORT_SCHEMA: dict[str, object] = {
    "$id": "https://krelcode.dev/schemas/security-review-report-v1.json",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "summary",
        "overall_severity",
        "verdict",
        "findings",
        "coverage",
        "limitations",
    ],
    "properties": {
        "schema_version": {
            "type": "string",
            "enum": [SECURITY_REVIEW_SCHEMA_VERSION],
        },
        "summary": {"type": "string", "minLength": 1, "maxLength": 8_000},
        "overall_severity": {
            "type": "string", "enum": list(SECURITY_REVIEW_SEVERITIES),
        },
        "verdict": {
            "type": "string", "enum": list(SECURITY_REVIEW_VERDICTS),
        },
        "findings": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "title",
                    "severity",
                    "confidence",
                    "category",
                    "file",
                    "description",
                    "impact",
                    "evidence",
                    "remediation",
                ],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 32},
                    "title": {"type": "string", "minLength": 1, "maxLength": 300},
                    "severity": {
                        "type": "string", "enum": list(SECURITY_REVIEW_SEVERITIES),
                    },
                    "confidence": {
                        "type": "string", "enum": list(SECURITY_REVIEW_CONFIDENCES),
                    },
                    "category": {
                        "type": "string", "enum": list(SECURITY_REVIEW_CATEGORIES),
                    },
                    "cwe": {"type": "string", "maxLength": 80},
                    "file": {"type": "string", "minLength": 1, "maxLength": 1_000},
                    "line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "description": {
                        "type": "string", "minLength": 1, "maxLength": 8_000,
                    },
                    "impact": {
                        "type": "string", "minLength": 1, "maxLength": 4_000,
                    },
                    "evidence": {
                        "type": "string", "minLength": 1, "maxLength": 8_000,
                    },
                    "remediation": {
                        "type": "string", "minLength": 1, "maxLength": 8_000,
                    },
                    "references": {
                        "type": "array",
                        "maxItems": 10,
                        "items": {"type": "string", "maxLength": 1_000},
                    },
                },
            },
        },
        "coverage": {
            "type": "object",
            "additionalProperties": False,
            "required": ["reviewed_files", "reviewed_areas", "notes"],
            "properties": {
                "reviewed_files": {
                    "type": "array",
                    "maxItems": 200,
                    "items": {"type": "string", "maxLength": 1_000},
                },
                "reviewed_areas": {
                    "type": "array",
                    "maxItems": 50,
                    "items": {
                        "type": "string",
                        "enum": list(SECURITY_REVIEW_CATEGORIES),
                    },
                },
                "notes": {"type": "string", "maxLength": 8_000},
            },
        },
        "limitations": {
            "type": "array",
            "maxItems": 50,
            "items": {"type": "string", "maxLength": 2_000},
        },
    },
}

SECURITY_REVIEW_SURFACE_CONTRACT = PromptSurfaceContract(
    input_schema={
        "$id": "https://krelcode.dev/schemas/security-review-input-v1.json",
        "type": "string",
        "minLength": 1,
        "maxLength": 160_000,
    },
    output_schema=SECURITY_REVIEW_REPORT_SCHEMA,
    timeout_seconds=300.0,
    max_input_chars=160_000,
    max_output_chars=128_000,
    output_mode="json",
    limit_strategy="error",
)


class SecurityReviewReportError(ValueError):
    """The model returned valid JSON that violates a report invariant."""


@dataclass(frozen=True)
class SecurityFinding:
    id: str
    title: str
    severity: str
    confidence: str
    category: str
    file: str
    description: str
    impact: str
    evidence: str
    remediation: str
    cwe: str = ""
    line: int | None = None
    end_line: int | None = None
    references: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "title": self.title,
            "severity": self.severity,
            "confidence": self.confidence,
            "category": self.category,
            "file": self.file,
            "description": self.description,
            "impact": self.impact,
            "evidence": self.evidence,
            "remediation": self.remediation,
            "references": list(self.references),
        }
        if self.cwe:
            payload["cwe"] = self.cwe
        if self.line is not None:
            payload["line"] = self.line
        if self.end_line is not None:
            payload["end_line"] = self.end_line
        return payload

    @property
    def location(self) -> str:
        if self.line is None:
            return self.file
        end = f"-{self.end_line}" if self.end_line and self.end_line != self.line else ""
        return f"{self.file}:{self.line}{end}"


@dataclass(frozen=True)
class SecurityCoverage:
    reviewed_files: tuple[str, ...] = ()
    reviewed_areas: tuple[str, ...] = ()
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "reviewed_files": list(self.reviewed_files),
            "reviewed_areas": list(self.reviewed_areas),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class SecurityReviewReport:
    summary: str
    overall_severity: str
    verdict: str
    findings: tuple[SecurityFinding, ...]
    coverage: SecurityCoverage
    limitations: tuple[str, ...] = ()
    schema_version: str = SECURITY_REVIEW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SECURITY_REVIEW_SCHEMA_VERSION:
            raise ValueError("未知 Security Review Schema 版本")
        if self.overall_severity not in SECURITY_REVIEW_SEVERITIES:
            raise ValueError("未知 Security Review severity")
        if self.verdict not in SECURITY_REVIEW_VERDICTS:
            raise ValueError("未知 Security Review verdict")
        ids = [finding.id for finding in self.findings]
        expected_ids = [f"SEC-{index:03d}" for index in range(1, len(ids) + 1)]
        if ids != expected_ids:
            raise SecurityReviewReportError(
                "Security Review finding id 必须从 SEC-001 开始按顺序连续编号")
        if self.findings:
            highest = max(
                (finding.severity for finding in self.findings),
                key=lambda severity: _SEVERITY_RANK[severity],
            )
            if _SEVERITY_RANK[self.overall_severity] != _SEVERITY_RANK[highest]:
                raise SecurityReviewReportError(
                    "overall_severity 必须等于最高 finding severity")
            if highest in {"critical", "high"} and self.verdict != "block":
                raise SecurityReviewReportError(
                    "high/critical finding 必须给出 block 结论")
            if highest in {"medium", "low"} and self.verdict == "pass":
                raise SecurityReviewReportError(
                    "medium/low finding 不能给出 pass 结论")

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "SecurityReviewReport":
        try:
            validate_prompt_surface_schema(
                raw, SECURITY_REVIEW_REPORT_SCHEMA, label="security report")
        except PromptSurfaceSchemaError as exc:
            raise SecurityReviewReportError(str(exc)) from exc
        findings_raw = raw.get("findings", ())
        findings = tuple(
            _finding_from_dict(item)
            for item in (
                findings_raw
                if isinstance(findings_raw, Sequence)
                and not isinstance(findings_raw, (str, bytes))
                else ()
            )
            if isinstance(item, Mapping)
        )
        coverage_raw = raw.get("coverage")
        coverage = coverage_raw if isinstance(coverage_raw, Mapping) else {}
        reviewed_files = _unique_texts(
            coverage.get("reviewed_files", ()), limit=200)
        for file_name in reviewed_files:
            _validate_report_path(file_name)
        report = cls(
            schema_version=str(raw.get("schema_version", "")),
            summary=str(raw.get("summary", "")).strip(),
            overall_severity=str(raw.get("overall_severity", "")),
            verdict=str(raw.get("verdict", "")),
            findings=findings,
            coverage=SecurityCoverage(
                reviewed_files=reviewed_files,
                reviewed_areas=_unique_texts(
                    coverage.get("reviewed_areas", ()), limit=50),
                notes=str(coverage.get("notes", "") or ""),
            ),
            limitations=_unique_texts(
                raw.get("limitations", ()), limit=50),
        )
        return report

    @classmethod
    def from_value(cls, value: object) -> "SecurityReviewReport":
        if not isinstance(value, Mapping):
            raise SecurityReviewReportError(
                "Security Review 输出必须是 JSON object")
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "summary": self.summary,
            "overall_severity": self.overall_severity,
            "verdict": self.verdict,
            "findings": [finding.to_dict() for finding in self.findings],
            "coverage": self.coverage.to_dict(),
            "limitations": list(self.limitations),
        }

    def counts_by_severity(self) -> dict[str, int]:
        return {
            severity: sum(
                1 for finding in self.findings
                if finding.severity == severity)
            for severity in SECURITY_REVIEW_SEVERITIES
        }

    def gate_failed(self, fail_on: str = "high") -> bool:
        normalized = str(fail_on or "high").strip().lower()
        if normalized not in _FAIL_ON_RANK:
            raise ValueError(f"未知 fail-on 阈值: {fail_on!r}")
        threshold = _FAIL_ON_RANK[normalized]
        if threshold is None:
            return False
        highest = max(
            [_SEVERITY_RANK[self.overall_severity], *(
                _SEVERITY_RANK[finding.severity]
                for finding in self.findings
            )],
        )
        return highest >= threshold

    def render_text(self) -> str:
        counts = self.counts_by_severity()
        lines = [
            "Security Review 报告",
            f"结论: {_verdict_label(self.verdict)}",
            f"总体风险: {self.overall_severity.upper()}",
            (
                "发现: "
                f"critical={counts['critical']} high={counts['high']} "
                f"medium={counts['medium']} low={counts['low']} "
                f"info={counts['info']}"
            ),
            "",
            "摘要:",
            self.summary,
        ]
        if not self.findings:
            lines.extend(("", "未发现需要报告的安全问题。"))
        else:
            lines.extend(("", "发现项:"))
            ordered = sorted(
                self.findings,
                key=lambda item: (-_SEVERITY_RANK[item.severity], item.id),
            )
            for finding in ordered:
                labels = [finding.severity.upper(), finding.category]
                if finding.cwe:
                    labels.append(finding.cwe)
                lines.extend((
                    "",
                    f"[{finding.id}] {finding.title}",
                    (
                        f"  {' · '.join(labels)} · confidence={finding.confidence}"
                        f" · {finding.location}"
                    ),
                    f"  问题: {finding.description}",
                    f"  影响: {finding.impact}",
                    f"  证据: {finding.evidence}",
                    f"  修复: {finding.remediation}",
                ))
                if finding.references:
                    lines.append(
                        "  参考: " + ", ".join(finding.references))
        lines.extend(("", "覆盖范围:"))
        if self.coverage.reviewed_files:
            lines.append(
                "  文件: " + ", ".join(self.coverage.reviewed_files))
        if self.coverage.reviewed_areas:
            lines.append(
                "  领域: " + ", ".join(self.coverage.reviewed_areas))
        if self.coverage.notes:
            lines.append(f"  说明: {self.coverage.notes}")
        if self.limitations:
            lines.extend(("", "限制:"))
            lines.extend(f"  - {item}" for item in self.limitations)
        return "\n".join(lines)


def _finding_from_dict(raw: Mapping[str, object]) -> SecurityFinding:
    finding_id = str(raw.get("id", "") or "").strip()
    if not _FINDING_ID_RE.fullmatch(finding_id):
        raise SecurityReviewReportError(
            f"非法 Security finding id: {finding_id!r}；应为 SEC-001 格式")
    cwe = str(raw.get("cwe", "") or "").strip()
    if cwe and not _CWE_RE.fullmatch(cwe):
        raise SecurityReviewReportError(
            f"{finding_id} 的 cwe 必须是 CWE-NNN 格式")
    file_name = str(raw.get("file", "") or "").strip()
    try:
        _validate_report_path(file_name)
    except SecurityReviewReportError as exc:
        raise SecurityReviewReportError(f"{finding_id}: {exc}") from exc
    line = _optional_int(raw.get("line"))
    end_line = _optional_int(raw.get("end_line"))
    if line is not None and end_line is not None and end_line < line:
        raise SecurityReviewReportError(
            f"{finding_id} 的 end_line 不能早于 line")
    return SecurityFinding(
        id=finding_id,
        title=str(raw.get("title", "") or "").strip(),
        severity=str(raw.get("severity", "") or ""),
        confidence=str(raw.get("confidence", "") or ""),
        category=str(raw.get("category", "") or ""),
        cwe=cwe,
        file=file_name,
        line=line,
        end_line=end_line,
        description=str(raw.get("description", "") or "").strip(),
        impact=str(raw.get("impact", "") or "").strip(),
        evidence=str(raw.get("evidence", "") or "").strip(),
        remediation=str(raw.get("remediation", "") or "").strip(),
        references=_unique_texts(raw.get("references", ()), limit=10),
    )


def _validate_report_path(path: str) -> None:
    value = str(path or "").strip()
    if not value:
        raise SecurityReviewReportError("报告 file 不能为空")
    if value.startswith(("/", "\\")):
        raise SecurityReviewReportError("报告 file 必须是工作区相对路径")
    if any(part == ".." for part in value.replace("\\", "/").split("/")):
        raise SecurityReviewReportError("报告 file 不能包含路径穿越")


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SecurityReviewReportError("finding line 必须是正整数")
    if value < 1:
        raise SecurityReviewReportError("finding line 必须是正整数")
    return value


def _unique_texts(values: object, *, limit: int) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    normalized = (
        str(value).strip() for value in values if str(value).strip())
    return tuple(dict.fromkeys(normalized))[:limit]


def _verdict_label(verdict: str) -> str:
    return {
        "pass": "通过",
        "needs_review": "需要人工复核",
        "block": "阻断合入",
    }.get(verdict, verdict)


def security_report_json_template() -> str:
    """Compact prompt-side reminder of the enforced output shape."""
    sample = {
        "schema_version": SECURITY_REVIEW_SCHEMA_VERSION,
        "summary": "One-paragraph risk summary.",
        "overall_severity": "high",
        "verdict": "block",
        "findings": [{
            "id": "SEC-001",
            "title": "Short finding title",
            "severity": "high",
            "confidence": "high",
            "category": "injection",
            "cwe": "CWE-78",
            "file": "relative/path.py",
            "line": 42,
            "end_line": 44,
            "description": "What is wrong and the triggering condition.",
            "impact": "Concrete security impact.",
            "evidence": "Specific inspected code evidence.",
            "remediation": "Actionable fix and verification.",
            "references": [],
        }],
        "coverage": {
            "reviewed_files": ["relative/path.py"],
            "reviewed_areas": ["injection"],
            "notes": "What was and was not inspected.",
        },
        "limitations": [],
    }
    return json.dumps(sample, ensure_ascii=False, indent=2)


def normalize_security_report(value: object) -> SecurityReviewReport:
    report = SecurityReviewReport.from_value(value)
    ordered = tuple(sorted(
        report.findings,
        key=lambda item: (-_SEVERITY_RANK[item.severity], item.id),
    ))
    return replace(report, findings=ordered)


__all__ = [
    "SECURITY_REVIEW_CATEGORIES",
    "SECURITY_REVIEW_CONFIDENCES",
    "SECURITY_REVIEW_REPORT_SCHEMA",
    "SECURITY_REVIEW_SCHEMA_VERSION",
    "SECURITY_REVIEW_SEVERITIES",
    "SECURITY_REVIEW_SURFACE_CONTRACT",
    "SECURITY_REVIEW_VERDICTS",
    "SecurityCoverage",
    "SecurityFinding",
    "SecurityReviewReport",
    "SecurityReviewReportError",
    "normalize_security_report",
    "security_report_json_template",
]
