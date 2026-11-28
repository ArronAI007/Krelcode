"""Business/application request contract shared by every frontend.

This DTO describes why a task exists, how it was triggered, what shape it has,
how success is accepted, and which risk/budget envelope applies.  It contains
no terminal, provider, tool, or persistence implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence


class TriggerType(str, Enum):
    REPL = "repl"
    HEADLESS = "headless"
    STREAM_JSON = "stream-json"
    SDK = "sdk"
    DAEMON = "daemon"
    SUBAGENT = "subagent"
    WEBUI = "webui"
    IDE = "ide"
    SCHEDULE = "schedule"
    EVENT = "event"


class TaskShape(str, Enum):
    SINGLE_AGENT = "single-agent"
    LONG_HORIZON = "long-horizon"
    MULTI_AGENT = "multi-agent"
    HYBRID = "hybrid"
    WORKFLOW = "workflow"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class AcceptanceContract:
    criteria: tuple[str, ...] = (
        "Return a direct, evidence-backed response to the user's goal.",
    )
    required_evidence: tuple[str, ...] = ("model-or-tool-result",)
    allow_manual_review: bool = True

    def __post_init__(self) -> None:
        criteria = tuple(
            text for item in self.criteria
            if (text := " ".join(str(item).split()))
        )
        evidence = tuple(
            text for item in self.required_evidence
            if (text := " ".join(str(item).split()))
        )
        if not criteria:
            raise ValueError("AcceptanceContract.criteria 不能为空")
        object.__setattr__(self, "criteria", criteria)
        object.__setattr__(self, "required_evidence", evidence)
        object.__setattr__(
            self, "allow_manual_review", bool(self.allow_manual_review))

    def to_dict(self) -> dict[str, object]:
        return {
            "criteria": list(self.criteria),
            "required_evidence": list(self.required_evidence),
            "allow_manual_review": self.allow_manual_review,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "AcceptanceContract":
        criteria = raw.get("criteria", ())
        evidence = raw.get("required_evidence", ())
        return cls(
            criteria=_string_tuple(criteria),
            required_evidence=_string_tuple(evidence),
            allow_manual_review=bool(raw.get("allow_manual_review", True)),
        )


@dataclass(frozen=True)
class RiskProfile:
    level: RiskLevel | str = RiskLevel.LOW
    reasons: tuple[str, ...] = ()
    requires_approval: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "level", RiskLevel(self.level))
        object.__setattr__(
            self, "reasons", _string_tuple(self.reasons))
        object.__setattr__(
            self, "requires_approval", bool(self.requires_approval))

    def to_dict(self) -> dict[str, object]:
        return {
            "level": RiskLevel(self.level).value,
            "reasons": list(self.reasons),
            "requires_approval": self.requires_approval,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "RiskProfile":
        return cls(
            level=str(raw.get("level") or RiskLevel.LOW.value),
            reasons=_string_tuple(raw.get("reasons", ())),
            requires_approval=bool(raw.get("requires_approval", False)),
        )


@dataclass(frozen=True)
class TaskBudget:
    max_steps: Optional[int] = None
    max_tokens: Optional[int] = None
    timeout_seconds: Optional[float] = None
    max_cost_usd: Optional[float] = None
    max_tool_calls: Optional[int] = None
    max_subagents: Optional[int] = None
    max_high_risk_actions: Optional[int] = None
    soft_limit_ratio: float = 0.8
    ask_user_at_soft_limit: bool = True
    allow_budget_extension: bool = True
    budget_extension_ratio: float = 0.5

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "max_steps", _optional_positive_int(self.max_steps))
        object.__setattr__(
            self, "max_tokens", _optional_positive_int(self.max_tokens))
        object.__setattr__(
            self, "timeout_seconds", _optional_positive_float(
                self.timeout_seconds))
        object.__setattr__(
            self, "max_cost_usd", _optional_positive_float(self.max_cost_usd))
        object.__setattr__(
            self,
            "max_tool_calls",
            _optional_positive_int(self.max_tool_calls),
        )
        object.__setattr__(
            self,
            "max_subagents",
            _optional_positive_int(self.max_subagents),
        )
        object.__setattr__(
            self,
            "max_high_risk_actions",
            _optional_positive_int(self.max_high_risk_actions),
        )
        object.__setattr__(
            self,
            "soft_limit_ratio",
            min(1.0, max(0.1, float(self.soft_limit_ratio))),
        )
        object.__setattr__(
            self,
            "ask_user_at_soft_limit",
            bool(self.ask_user_at_soft_limit),
        )
        object.__setattr__(
            self,
            "allow_budget_extension",
            bool(self.allow_budget_extension),
        )
        object.__setattr__(
            self,
            "budget_extension_ratio",
            min(10.0, max(0.1, float(self.budget_extension_ratio))),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "max_steps": self.max_steps,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "max_cost_usd": self.max_cost_usd,
            "max_tool_calls": self.max_tool_calls,
            "max_subagents": self.max_subagents,
            "max_high_risk_actions": self.max_high_risk_actions,
            "soft_limit_ratio": self.soft_limit_ratio,
            "ask_user_at_soft_limit": self.ask_user_at_soft_limit,
            "allow_budget_extension": self.allow_budget_extension,
            "budget_extension_ratio": self.budget_extension_ratio,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "TaskBudget":
        return cls(
            max_steps=_coerce_optional_int(raw.get("max_steps")),
            max_tokens=_coerce_optional_int(raw.get("max_tokens")),
            timeout_seconds=_coerce_optional_float(raw.get("timeout_seconds")),
            max_cost_usd=_coerce_optional_float(raw.get("max_cost_usd")),
            max_tool_calls=_coerce_optional_int(raw.get("max_tool_calls")),
            max_subagents=_coerce_optional_int(raw.get("max_subagents")),
            max_high_risk_actions=_coerce_optional_int(
                raw.get("max_high_risk_actions")),
            soft_limit_ratio=_coerce_optional_float(
                raw.get("soft_limit_ratio")) or 0.8,
            ask_user_at_soft_limit=bool(
                raw.get("ask_user_at_soft_limit", True)),
            allow_budget_extension=bool(
                raw.get("allow_budget_extension", True)),
            budget_extension_ratio=_coerce_optional_float(
                raw.get("budget_extension_ratio")) or 0.5,
        )


@dataclass(frozen=True)
class ApplicationRequest:
    trigger: TriggerType
    goal: str
    task_shape: TaskShape
    acceptance: AcceptanceContract
    risk: RiskProfile
    budget: TaskBudget
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        goal = " ".join(str(self.goal or "").split())
        if not goal:
            raise ValueError("ApplicationRequest.goal 不能为空")
        object.__setattr__(self, "trigger", TriggerType(self.trigger))
        object.__setattr__(self, "goal", goal)
        object.__setattr__(self, "task_shape", TaskShape(self.task_shape))
        if not isinstance(self.acceptance, AcceptanceContract):
            raise TypeError("acceptance 必须是 AcceptanceContract")
        if not isinstance(self.risk, RiskProfile):
            raise TypeError("risk 必须是 RiskProfile")
        if not isinstance(self.budget, TaskBudget):
            raise TypeError("budget 必须是 TaskBudget")
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(
                dict(self.metadata)
                if isinstance(self.metadata, Mapping) else {}),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "trigger": TriggerType(self.trigger).value,
            "goal": self.goal,
            "task_shape": TaskShape(self.task_shape).value,
            "acceptance": self.acceptance.to_dict(),
            "risk": self.risk.to_dict(),
            "budget": self.budget.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "ApplicationRequest":
        acceptance = raw.get("acceptance")
        risk = raw.get("risk")
        budget = raw.get("budget")
        metadata = raw.get("metadata")
        return cls(
            trigger=_coerce_trigger(raw.get("trigger")),
            goal=str(raw.get("goal") or ""),
            task_shape=_coerce_task_shape(raw.get("task_shape")),
            acceptance=(
                AcceptanceContract.from_mapping(acceptance)
                if isinstance(acceptance, Mapping)
                else AcceptanceContract()
            ),
            risk=(
                RiskProfile.from_mapping(risk)
                if isinstance(risk, Mapping)
                else RiskProfile()
            ),
            budget=(
                TaskBudget.from_mapping(budget)
                if isinstance(budget, Mapping)
                else TaskBudget()
            ),
            metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
        )


class ApplicationRequestFactory:
    """Build the same application contract from every frontend."""

    @classmethod
    def build(
        cls,
        *,
        trigger: TriggerType | str,
        goal: str,
        task_shape: TaskShape | str = TaskShape.SINGLE_AGENT,
        acceptance: Optional[AcceptanceContract] = None,
        risk: Optional[RiskProfile] = None,
        budget: Optional[TaskBudget] = None,
        metadata: Optional[Mapping[str, object]] = None,
    ) -> ApplicationRequest:
        return ApplicationRequest(
            trigger=TriggerType(trigger),
            goal=goal,
            task_shape=TaskShape(task_shape),
            acceptance=acceptance or AcceptanceContract(),
            risk=risk or RiskProfile(),
            budget=budget or TaskBudget(),
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_input_source(
        cls,
        source: TriggerType | str,
        *,
        goal: str,
        metadata: Optional[Mapping[str, object]] = None,
        task_shape: TaskShape | str = TaskShape.SINGLE_AGENT,
        acceptance: Optional[AcceptanceContract] = None,
        risk: Optional[RiskProfile] = None,
        budget: Optional[TaskBudget] = None,
    ) -> ApplicationRequest:
        if isinstance(source, TriggerType):
            return cls.build(
                trigger=source,
                goal=goal,
                task_shape=task_shape,
                acceptance=acceptance,
                risk=risk,
                budget=budget,
                metadata={
                    "input_source": source.value,
                    **dict(metadata or {}),
                },
            )
        normalized = str(source or "").strip().lower()
        trigger = {
            "terminal": TriggerType.REPL,
            "repl": TriggerType.REPL,
            "headless": TriggerType.HEADLESS,
            "stream-json": TriggerType.STREAM_JSON,
            "stream_json": TriggerType.STREAM_JSON,
            "sdk": TriggerType.SDK,
            "daemon": TriggerType.DAEMON,
            "subagent": TriggerType.SUBAGENT,
            "webui": TriggerType.WEBUI,
            "web-ui": TriggerType.WEBUI,
            "ide": TriggerType.IDE,
            "schedule": TriggerType.SCHEDULE,
            "event": TriggerType.EVENT,
        }.get(normalized, TriggerType.HEADLESS)
        return cls.build(
            trigger=trigger,
            goal=goal,
            task_shape=task_shape,
            acceptance=acceptance,
            risk=risk,
            budget=budget,
            metadata={"input_source": normalized, **dict(metadata or {})},
        )


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        raw: Sequence[object] = (value,)
    elif isinstance(value, Sequence):
        raw = value
    else:
        raw = ()
    return tuple(
        text for item in raw
        if (text := " ".join(str(item).split()))
    )


def _coerce_optional_int(value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_optional_float(value: object) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _optional_positive_int(value: object) -> Optional[int]:
    parsed = _coerce_optional_int(value)
    return max(1, parsed) if parsed is not None else None


def _optional_positive_float(value: object) -> Optional[float]:
    parsed = _coerce_optional_float(value)
    return max(0.001, parsed) if parsed is not None else None


def _coerce_trigger(value: object) -> TriggerType:
    return value if isinstance(value, TriggerType) else TriggerType(
        str(value or TriggerType.HEADLESS.value))


def _coerce_task_shape(value: object) -> TaskShape:
    return value if isinstance(value, TaskShape) else TaskShape(
        str(value or TaskShape.SINGLE_AGENT.value))


__all__ = [
    "AcceptanceContract",
    "ApplicationRequest",
    "ApplicationRequestFactory",
    "RiskLevel",
    "RiskProfile",
    "TaskBudget",
    "TaskShape",
    "TriggerType",
]
