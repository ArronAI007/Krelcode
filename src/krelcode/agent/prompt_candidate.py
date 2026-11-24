"""Formal candidate Prompt schema and authoring-request adapter.

A candidate is review data, not an active Prompt source.  This module owns the
closed wire shape, validation, deterministic identity, and conversion into the
existing ``PromptAuthoringRequest`` validator.  It never writes a Fragment or
mutates a Registry.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Tuple, cast

from .evidence import InstructionSource, instruction_source_for
from .fragments import (
    LAYER_L6_CATALOG,
    STABILITY_SESSION,
    PromptFragment,
)
from .prompt_authoring import (
    PromptAuthoringItem,
    PromptAuthoringRequest,
    PromptAuthoringValidationCase,
    PromptValidationKind,
)


PROMPT_CANDIDATE_VERSION = "1"
MAX_CANDIDATE_TARGET_CHARS = 160
MAX_CANDIDATE_OBJECTIVE_CHARS = 8_000
MAX_CANDIDATE_PROMPT_CHARS = 40_000
MAX_CANDIDATE_ROLLBACK_CHARS = 8_000
MAX_CANDIDATE_LIST_ITEMS = 50
MAX_CANDIDATE_TEXT_ITEM_CHARS = 4_000
MAX_CANDIDATE_EVALUATION_CASES = 100

_CANDIDATE_KEYS = frozenset({
    "target_fragment",
    "objective",
    "proposed_prompt",
    "constraints",
    "expected_behavior",
    "risk",
    "evaluation_cases",
    "rollback_plan",
})
_EVALUATION_CASE_KEYS = frozenset({
    "id", "kind", "metric", "machine_verifiable",
})
_VALIDATION_KINDS = frozenset(item.value for item in PromptValidationKind)


class PromptCandidateError(ValueError):
    """A structurally or semantically invalid candidate Prompt."""


class PromptCandidateRisk(str, Enum):
    """Review risk assigned to a candidate before any production change."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _required_text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise PromptCandidateError(f"{field} 必须是字符串")
    result = value.strip()
    if not result:
        raise PromptCandidateError(f"{field} 不能为空")
    if len(result) > limit:
        raise PromptCandidateError(f"{field} 超过 {limit} 字符上限")
    return result


def _string_list(
    value: object,
    field: str,
    *,
    limit: int = MAX_CANDIDATE_LIST_ITEMS,
) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise PromptCandidateError(f"{field} 必须是字符串数组")
    if len(value) > limit:
        raise PromptCandidateError(f"{field} 最多允许 {limit} 项")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise PromptCandidateError(f"{field}[{index}] 必须是字符串")
        normalized = item.strip()
        if not normalized:
            raise PromptCandidateError(f"{field}[{index}] 不能为空")
        if len(normalized) > MAX_CANDIDATE_TEXT_ITEM_CHARS:
            raise PromptCandidateError(
                f"{field}[{index}] 超过 {MAX_CANDIDATE_TEXT_ITEM_CHARS} 字符上限")
        result.append(normalized)
    return tuple(result)


def _risk(value: object) -> PromptCandidateRisk:
    if isinstance(value, PromptCandidateRisk):
        return value
    if not isinstance(value, str):
        raise PromptCandidateError("risk 必须是 low/medium/high")
    normalized = value.strip().lower()
    try:
        return PromptCandidateRisk(normalized)
    except ValueError as exc:
        raise PromptCandidateError("risk 必须是 low/medium/high") from exc


def _validation_case(
    value: object,
    *,
    index: int,
) -> PromptAuthoringValidationCase:
    if isinstance(value, PromptAuthoringValidationCase):
        case_id = value.id.strip()
        kind = str(value.kind).strip().lower()
        metric = value.metric.strip()
        machine_verifiable = value.machine_verifiable
    else:
        if not isinstance(value, Mapping):
            raise PromptCandidateError(f"evaluation_cases[{index}] 必须是对象")
        unknown = set(value).difference(_EVALUATION_CASE_KEYS)
        if unknown:
            raise PromptCandidateError(
                f"evaluation_cases[{index}] 含未知字段: "
                + ", ".join(sorted(unknown)))
        case_id = _required_text(
            value.get("id"), f"evaluation_cases[{index}].id", 160)
        kind = _required_text(
            value.get("kind"), f"evaluation_cases[{index}].kind", 40).lower()
        metric = _required_text(
            value.get("metric"), f"evaluation_cases[{index}].metric", 4_000)
        raw_machine_verifiable = value.get("machine_verifiable", True)
        if not isinstance(raw_machine_verifiable, bool):
            raise PromptCandidateError(
                f"evaluation_cases[{index}].machine_verifiable 必须是布尔值")
        machine_verifiable = raw_machine_verifiable
    if not case_id:
        raise PromptCandidateError(f"evaluation_cases[{index}].id 不能为空")
    if len(case_id) > 160:
        raise PromptCandidateError(
            f"evaluation_cases[{index}].id 超过 160 字符上限")
    if not metric:
        raise PromptCandidateError(f"evaluation_cases[{index}].metric 不能为空")
    if len(metric) > 4_000:
        raise PromptCandidateError(
            f"evaluation_cases[{index}].metric 超过 4000 字符上限")
    if not isinstance(machine_verifiable, bool):
        raise PromptCandidateError(
            f"evaluation_cases[{index}].machine_verifiable 必须是布尔值")
    if kind not in _VALIDATION_KINDS:
        raise PromptCandidateError(
            f"evaluation_cases[{index}].kind 必须是 success/regression/security")
    return PromptAuthoringValidationCase(
        id=case_id,
        kind=kind,
        metric=metric,
        machine_verifiable=machine_verifiable,
    )


def _case_record(case: PromptAuthoringValidationCase) -> dict[str, object]:
    return {
        "id": case.id,
        "kind": str(case.kind),
        "metric": case.metric,
        "machine_verifiable": case.machine_verifiable,
    }


@dataclass(frozen=True)
class PromptCandidate:
    """Closed, reviewable candidate Prompt contract.

    Arrays may be empty structurally.  Semantic evaluation still requires
    machine-verifiable success/regression/security cases through
    :class:`PromptAuthoringValidator`.
    """

    target_fragment: str
    objective: str
    proposed_prompt: str
    constraints: Tuple[str, ...] = ()
    expected_behavior: Tuple[str, ...] = ()
    risk: PromptCandidateRisk | str = PromptCandidateRisk.LOW
    evaluation_cases: Tuple[PromptAuthoringValidationCase, ...] = ()
    rollback_plan: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_fragment",
            _required_text(
                self.target_fragment,
                "target_fragment",
                MAX_CANDIDATE_TARGET_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "objective",
            _required_text(
                self.objective,
                "objective",
                MAX_CANDIDATE_OBJECTIVE_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "proposed_prompt",
            _required_text(
                self.proposed_prompt,
                "proposed_prompt",
                MAX_CANDIDATE_PROMPT_CHARS,
            ),
        )
        object.__setattr__(
            self, "constraints", _string_list(self.constraints, "constraints"))
        object.__setattr__(
            self,
            "expected_behavior",
            _string_list(self.expected_behavior, "expected_behavior"),
        )
        object.__setattr__(self, "risk", _risk(self.risk))
        cases = tuple(self.evaluation_cases or ())
        if len(cases) > MAX_CANDIDATE_EVALUATION_CASES:
            raise PromptCandidateError(
                "evaluation_cases 最多允许 "
                f"{MAX_CANDIDATE_EVALUATION_CASES} 项")
        object.__setattr__(
            self,
            "evaluation_cases",
            tuple(
                _validation_case(case, index=index)
                for index, case in enumerate(cases)
            ),
        )
        object.__setattr__(
            self,
            "rollback_plan",
            _required_text(
                self.rollback_plan,
                "rollback_plan",
                MAX_CANDIDATE_ROLLBACK_CHARS,
            ),
        )

    @classmethod
    def from_mapping(cls, value: object) -> "PromptCandidate":
        if not isinstance(value, Mapping):
            raise PromptCandidateError("candidate 必须是对象")
        unknown = set(value).difference(_CANDIDATE_KEYS)
        if unknown:
            raise PromptCandidateError(
                "candidate 含未知字段: " + ", ".join(sorted(unknown)))
        missing = sorted(_CANDIDATE_KEYS.difference(value))
        if missing:
            raise PromptCandidateError(
                "candidate 缺少字段: " + ", ".join(missing))
        raw_cases = value.get("evaluation_cases")
        if not isinstance(raw_cases, (list, tuple)):
            raise PromptCandidateError("evaluation_cases 必须是数组")
        return cls(
            target_fragment=cast(str, value.get("target_fragment")),
            objective=cast(str, value.get("objective")),
            proposed_prompt=cast(str, value.get("proposed_prompt")),
            constraints=cast(Tuple[str, ...], value.get("constraints")),
            expected_behavior=cast(
                Tuple[str, ...], value.get("expected_behavior")),
            risk=cast(PromptCandidateRisk | str, value.get("risk")),
            evaluation_cases=tuple(
                _validation_case(case, index=index)
                for index, case in enumerate(raw_cases)
            ),
            rollback_plan=cast(str, value.get("rollback_plan")),
        )

    @property
    def risk_value(self) -> str:
        return _risk(self.risk).value

    @property
    def candidate_hash(self) -> str:
        return _hash({
            "candidate_version": PROMPT_CANDIDATE_VERSION,
            "candidate": self.as_record(),
        })

    def as_record(self) -> dict[str, object]:
        """Return the canonical, content-bearing review schema."""
        return {
            "target_fragment": self.target_fragment,
            "objective": self.objective,
            "proposed_prompt": self.proposed_prompt,
            "constraints": list(self.constraints),
            "expected_behavior": list(self.expected_behavior),
            "risk": self.risk_value,
            "evaluation_cases": [
                _case_record(case) for case in self.evaluation_cases
            ],
            "rollback_plan": self.rollback_plan,
        }

    def content_light_record(self) -> dict[str, object]:
        """Return review metadata without Prompt or metric bodies."""
        return {
            "candidate_version": PROMPT_CANDIDATE_VERSION,
            "target_fragment": self.target_fragment,
            "objective_hash": _hash(self.objective),
            "proposed_prompt_hash": _hash(self.proposed_prompt),
            "constraint_count": len(self.constraints),
            "expected_behavior_count": len(self.expected_behavior),
            "risk": self.risk_value,
            "evaluation_case_count": len(self.evaluation_cases),
            "evaluation_cases": tuple({
                "id": case.id,
                "kind": str(case.kind),
                "metric_hash": _hash(case.metric),
                "machine_verifiable": case.machine_verifiable,
            } for case in self.evaluation_cases),
            "rollback_plan_hash": _hash(self.rollback_plan),
            "contains_prompt_content": False,
        }

    def to_authoring_request(
        self,
        target_fragment: PromptFragment | None = None,
    ) -> PromptAuthoringRequest:
        """Adapt the candidate to the existing deterministic validator.

        Existing target Fragments are the source of truth for layer, scope,
        stability, cacheability, and instruction authority.  New targets use a
        conservative project-scoped L6/session default.
        """
        if target_fragment is not None:
            if target_fragment.id != self.target_fragment:
                raise PromptCandidateError(
                    "target_fragment 与已解析 Fragment ID 不一致")
            layer = target_fragment.layer
            scope = target_fragment.scope
            stability = target_fragment.stability
            cacheable = target_fragment.cacheable
            source = instruction_source_for(
                target_fragment.trust,
                source=target_fragment.source,
                origin=target_fragment.scope,
            )
            if source is None:
                raise PromptCandidateError(
                    "target_fragment 不是可承载 Instruction 的受信来源")
        else:
            layer = LAYER_L6_CATALOG
            scope = "project"
            stability = STABILITY_SESSION
            cacheable = True
            source = InstructionSource.PROJECT

        success_metrics = tuple(
            case.metric
            for case in self.evaluation_cases
            if str(case.kind) == PromptValidationKind.SUCCESS.value
        )
        item = PromptAuthoringItem(
            id=self.target_fragment,
            role="instruction",
            source=source,
            content=self.proposed_prompt,
            scope=scope,
            layer=layer,
            stability=stability,
            cacheable=cacheable,
            requires_validation=False,
        )
        return PromptAuthoringRequest(
            objective=self.objective,
            target_fragment_id=self.target_fragment,
            target_layer=layer,
            items=(item,),
            permission_mode="default",
            tool_scope="none",
            sandbox_policy="",
            approval_policy="",
            validation_cases=self.evaluation_cases,
            success_metrics=success_metrics,
            non_goals=self.constraints,
        )


PROMPT_CANDIDATE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "target_fragment": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CANDIDATE_TARGET_CHARS,
            "description": "Exact active or proposed Prompt Fragment ID.",
        },
        "objective": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CANDIDATE_OBJECTIVE_CHARS,
            "description": "Observable behavior the candidate should change.",
        },
        "proposed_prompt": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CANDIDATE_PROMPT_CHARS,
            "description": "Reviewable Prompt text; never applied automatically.",
        },
        "constraints": {
            "type": "array",
            "maxItems": MAX_CANDIDATE_LIST_ITEMS,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_CANDIDATE_TEXT_ITEM_CHARS,
            },
        },
        "expected_behavior": {
            "type": "array",
            "maxItems": MAX_CANDIDATE_LIST_ITEMS,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_CANDIDATE_TEXT_ITEM_CHARS,
            },
        },
        "risk": {
            "type": "string",
            "enum": [item.value for item in PromptCandidateRisk],
        },
        "evaluation_cases": {
            "type": "array",
            "maxItems": MAX_CANDIDATE_EVALUATION_CASES,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1, "maxLength": 160},
                    "kind": {
                        "type": "string",
                        "enum": [item.value for item in PromptValidationKind],
                    },
                    "metric": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4_000,
                    },
                    "machine_verifiable": {"type": "boolean"},
                },
                "required": ["id", "kind", "metric"],
                "additionalProperties": False,
            },
        },
        "rollback_plan": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_CANDIDATE_ROLLBACK_CHARS,
        },
    },
    "required": sorted(_CANDIDATE_KEYS),
    "additionalProperties": False,
}


__all__ = [
    "MAX_CANDIDATE_EVALUATION_CASES",
    "MAX_CANDIDATE_LIST_ITEMS",
    "MAX_CANDIDATE_OBJECTIVE_CHARS",
    "MAX_CANDIDATE_PROMPT_CHARS",
    "MAX_CANDIDATE_ROLLBACK_CHARS",
    "MAX_CANDIDATE_TARGET_CHARS",
    "MAX_CANDIDATE_TEXT_ITEM_CHARS",
    "PROMPT_CANDIDATE_SCHEMA",
    "PROMPT_CANDIDATE_VERSION",
    "PromptCandidate",
    "PromptCandidateError",
    "PromptCandidateRisk",
]
