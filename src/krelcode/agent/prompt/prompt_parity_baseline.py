"""Content-light accepted baselines for Prompt Parity gates."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from krelcode.agent.prompt.prompt_parity_contract import (
    PROMPT_BASELINE_SCHEMA_VERSION,
    PROMPT_PARITY_CONTRACT,
    PromptParityContract,
)
from krelcode.agent.prompt.prompt_release import ModelProfileCalibration
from krelcode.agent.prompt.prompt_release_support import BehaviorReportPort


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, (bool, int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (bool, int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return default


@dataclass(frozen=True)
class BaselineCaseResult:
    case_id: str
    trials: int
    passed: int
    failed: int
    pass_rate: float
    stable: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", str(self.case_id))
        object.__setattr__(self, "trials", max(0, int(self.trials)))
        object.__setattr__(self, "passed", max(0, int(self.passed)))
        object.__setattr__(self, "failed", max(0, int(self.failed)))
        object.__setattr__(self, "pass_rate", max(0.0, min(1.0, float(self.pass_rate))))
        object.__setattr__(self, "stable", bool(self.stable))
        if self.trials != self.passed + self.failed:
            raise ValueError(f"baseline case trial 计数不一致: {self.case_id}")

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "trials": self.trials,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.pass_rate, 6),
            "stable": self.stable,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "BaselineCaseResult":
        allowed = {"case_id", "trials", "passed", "failed", "pass_rate", "stable"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(
                "baseline case 包含不受支持的字段: " + ", ".join(unknown))
        return cls(
            case_id=str(raw.get("case_id", "") or ""),
            trials=_as_int(raw.get("trials", 0)),
            passed=_as_int(raw.get("passed", 0)),
            failed=_as_int(raw.get("failed", 0)),
            pass_rate=_as_float(raw.get("pass_rate", 0.0)),
            stable=bool(raw.get("stable", False)),
        )


@dataclass(frozen=True)
class PromptParityBaseline:
    """A content-light, provider/model-specific accepted benchmark baseline."""

    schema_version: int
    benchmark_schema_version: int
    created_at: str
    contract_version: str
    contract_hash: str
    suite_hash: str
    provider: str
    model: str
    krelcode_version: str
    trials: int
    cases: tuple[BaselineCaseResult, ...]
    calibration: ModelProfileCalibration
    profile: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "cases", tuple(self.cases))
        object.__setattr__(self, "profile", dict(self.profile))
        if len({item.case_id for item in self.cases}) != len(self.cases):
            raise ValueError("baseline case 不能重复")
        if self.calibration.contract_version != self.contract_version:
            raise ValueError("baseline 与 calibration contract version 不一致")
        if self.calibration.contract_hash != self.contract_hash:
            raise ValueError("baseline 与 calibration contract hash 不一致")
        if self.calibration.suite_hash != self.suite_hash:
            raise ValueError("baseline 与 calibration suite hash 不一致")
        if (
            self.calibration.provider != self.provider.strip().lower()
            or self.calibration.model != self.model.strip().lower()
        ):
            raise ValueError("baseline 与 calibration provider/model 不一致")
        if self.calibration.benchmark_schema_version != self.benchmark_schema_version:
            raise ValueError("baseline 与 calibration benchmark schema 不一致")

    @property
    def case_map(self) -> dict[str, BaselineCaseResult]:
        return {item.case_id: item for item in self.cases}

    @property
    def hash(self) -> str:
        payload = self._hash_payload()
        return hashlib.sha256(json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "benchmark_schema_version": self.benchmark_schema_version,
            "contract_version": self.contract_version,
            "contract_hash": self.contract_hash,
            "suite_hash": self.suite_hash,
            "provider": self.provider,
            "model": self.model,
            "krelcode_version": self.krelcode_version,
            "trials": self.trials,
            "cases": [item.as_dict() for item in self.cases],
            "calibration": self.calibration.as_dict(),
            "profile": dict(self.profile),
        }

    def as_dict(self) -> dict[str, object]:
        payload = self._hash_payload()
        payload.update({
            "kind": "prompt_parity_baseline",
            "created_at": self.created_at,
            "baseline_hash": self.hash,
            "contains_prompt_content": False,
        })
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "PromptParityBaseline":
        allowed_top_level = {
            "kind", "schema_version", "benchmark_schema_version",
            "created_at", "contract_version",
            "contract_hash", "suite_hash", "provider", "model",
            "krelcode_version", "trials", "cases", "calibration", "profile",
            "baseline_hash", "contains_prompt_content",
        }
        unknown = sorted(set(raw) - allowed_top_level)
        if unknown:
            raise ValueError(
                "baseline 包含不受支持的字段: " + ", ".join(unknown))
        if str(raw.get("kind", "")) != "prompt_parity_baseline":
            raise ValueError("不是 Prompt Parity baseline")
        if raw.get("contains_prompt_content") is not False:
            raise ValueError("baseline 不允许包含 Prompt/答案/工具正文")
        cases_raw = raw.get("cases")
        if not isinstance(cases_raw, list):
            raise ValueError("baseline cases 必须是数组")
        cases = []
        for item in cases_raw:
            if not isinstance(item, Mapping):
                raise ValueError("baseline case 必须是 object")
            cases.append(BaselineCaseResult.from_dict(item))
        calibration_raw = raw.get("calibration")
        if not isinstance(calibration_raw, Mapping):
            raise ValueError(
                "baseline schema 2 必须包含 model profile calibration；"
                "旧 baseline 请重新运行 --save-baseline")
        calibration = ModelProfileCalibration.from_dict(calibration_raw)
        profile = raw.get("profile")
        if profile is not None and not isinstance(profile, Mapping):
            raise ValueError("baseline profile 必须是 object")
        allowed_profile = {
            "suite_hash", "sandbox_backend", "context_max_tokens",
            "max_steps", "trial_timeout_seconds", "krelcode_version",
            "python_version", "platform", "parity_contract_version",
            "parity_contract_hash", "cases", "trials",
            "prompt_release_id", "prompt_release_hash",
            "prompt_profile", "prompt_profile_hash",
            "profile_calibration_id", "profile_calibration_hash",
            "profile_calibration_status", "profile_calibration_reason",
            "provider_base_url_hash", "provider_generation_hash",
            "provider_timeout_seconds", "provider_max_tokens",
        }
        profile_unknown = sorted(set(profile or ()) - allowed_profile)
        if profile_unknown:
            raise ValueError(
                "baseline profile 包含不受支持的字段: "
                + ", ".join(profile_unknown))
        baseline = cls(
            schema_version=_as_int(raw.get("schema_version", 0)),
            benchmark_schema_version=_as_int(
                raw.get("benchmark_schema_version", 0)),
            created_at=str(raw.get("created_at", "") or ""),
            contract_version=str(raw.get("contract_version", "") or ""),
            contract_hash=str(raw.get("contract_hash", "") or ""),
            suite_hash=str(raw.get("suite_hash", "") or ""),
            provider=str(raw.get("provider", "") or ""),
            model=str(raw.get("model", "") or ""),
            krelcode_version=str(raw.get("krelcode_version", "") or ""),
            trials=_as_int(raw.get("trials", 0)),
            cases=tuple(cases),
            calibration=calibration,
            profile=dict(profile) if isinstance(profile, Mapping) else {},
        )
        stored_hash = str(raw.get("baseline_hash", "") or "")
        if stored_hash and stored_hash != baseline.hash:
            raise ValueError("baseline_hash 与内容不一致")
        return baseline

    @classmethod
    def from_report(
        cls,
        report: BehaviorReportPort,
        contract: PromptParityContract = PROMPT_PARITY_CONTRACT,
    ) -> "PromptParityBaseline":
        if not bool(getattr(report, "passed", False)):
            raise ValueError("只有完整通过的 live report 可以冻结为 baseline")
        summary = report.case_summary()
        required = set(contract.required_case_ids)
        if set(summary) != required:
            missing = sorted(required - set(summary))
            extra = sorted(set(summary) - required)
            detail = []
            if missing:
                detail.append("missing=" + ",".join(missing))
            if extra:
                detail.append("extra=" + ",".join(extra))
            raise ValueError("baseline 必须来自完整 case manifest: " + " ".join(detail))
        under_sampled = sorted(
            case_id for case_id, value in summary.items()
            if _as_int(value.get("trials", 0)) < contract.minimum_trials
        )
        if under_sampled:
            raise ValueError(
                f"baseline 每个 case 至少需要 {contract.minimum_trials} trials: "
                + ", ".join(under_sampled)
            )
        cases = tuple(BaselineCaseResult(
            case_id=case_id,
            trials=_as_int(summary[case_id].get("trials", 0)),
            passed=_as_int(summary[case_id].get("passed", 0)),
            failed=_as_int(summary[case_id].get("failed", 0)),
            pass_rate=_as_float(summary[case_id].get("pass_rate", 0.0)),
            stable=bool(summary[case_id].get("stable", False)),
        ) for case_id in contract.required_case_ids)
        profile = dict(getattr(report, "profile", {}) or {})
        release_id = str(profile.get("prompt_release_id", "") or "")
        release_hash = str(profile.get("prompt_release_hash", "") or "")
        profile_name = str(profile.get("prompt_profile", "") or "")
        profile_hash = str(profile.get("prompt_profile_hash", "") or "")
        suite_hash = str(profile.get("suite_hash", "") or "")
        missing_identity = [
            name for name, value in (
                ("prompt_release_id", release_id),
                ("prompt_release_hash", release_hash),
                ("prompt_profile", profile_name),
                ("prompt_profile_hash", profile_hash),
                ("suite_hash", suite_hash),
            )
            if not value
        ]
        if missing_identity:
            raise ValueError(
                "baseline report 缺少 Prompt release/profile identity: "
                + ", ".join(missing_identity))
        report_contract_version = str(
            profile.get("parity_contract_version", "") or "")
        report_contract_hash = str(
            profile.get("parity_contract_hash", "") or "")
        if (
            report_contract_version != contract.version
            or report_contract_hash != contract.hash
        ):
            raise ValueError("baseline report 不是由当前 Parity contract 生成")
        trials = max((item.trials for item in cases), default=0)
        calibration = ModelProfileCalibration.certify(
            profile_name=profile_name,
            profile_hash=profile_hash,
            release_id=release_id,
            release_hash=release_hash,
            contract_version=contract.version,
            contract_hash=contract.hash,
            benchmark_schema_version=int(
                getattr(report, "schema_version", 0) or 0),
            suite_hash=suite_hash,
            provider=str(getattr(report, "provider", "") or ""),
            model=str(getattr(report, "model", "") or ""),
            verified_at=(
                str(getattr(report, "generated_at", "") or "")
                or datetime.now(timezone.utc).isoformat()),
            trials=trials,
        )
        return cls(
            schema_version=PROMPT_BASELINE_SCHEMA_VERSION,
            benchmark_schema_version=int(
                getattr(report, "schema_version", 0) or 0),
            created_at=datetime.now(timezone.utc).isoformat(),
            contract_version=contract.version,
            contract_hash=contract.hash,
            suite_hash=suite_hash,
            provider=str(getattr(report, "provider", "") or ""),
            model=str(getattr(report, "model", "") or ""),
            krelcode_version=str(profile.get("krelcode_version", "") or ""),
            trials=trials,
            cases=cases,
            calibration=calibration,
            profile=profile,
        )


def save_prompt_baseline(path: Path, baseline: PromptParityBaseline) -> None:
    """Atomically persist a content-light baseline."""
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(baseline.as_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def load_prompt_baseline(path: Path) -> PromptParityBaseline:
    source = Path(path).expanduser()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取 baseline: {source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"baseline JSON 无效: {source}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("baseline 根节点必须是 object")
    return PromptParityBaseline.from_dict(raw)


__all__ = [
    "BaselineCaseResult",
    "PROMPT_BASELINE_SCHEMA_VERSION",
    "PromptParityBaseline",
    "load_prompt_baseline",
    "save_prompt_baseline",
]
