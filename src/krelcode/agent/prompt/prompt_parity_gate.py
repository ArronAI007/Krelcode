"""Evaluation and rendering for Prompt Parity regression gates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from krelcode.agent.prompt.prompt_parity_baseline import (
    PROMPT_BASELINE_SCHEMA_VERSION,
    PromptParityBaseline,
    _as_float,
    _as_int,
)
from krelcode.agent.prompt.prompt_parity_contract import (
    FROZEN_PROMPT_PARITY_CONTRACT_HASH,
    FROZEN_PROMPT_PARITY_SUITE_HASH,
    PROMPT_PARITY_CONTRACT,
    PROMPT_PARITY_SCHEMA_VERSION,
    PromptParityContract,
    PromptParityStatus,
    _RUNNER_PROFILE_KEYS,
)
from krelcode.agent.prompt.prompt_release_support import BehaviorReportPort


@dataclass(frozen=True)
class ParityGateCheck:
    id: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"id": self.id, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class PromptParityGateResult:
    schema_version: int
    generated_at: str
    status: PromptParityStatus
    passed: bool
    contract_version: str
    contract_hash: str
    baseline_hash: str
    report_suite_hash: str
    checks: tuple[ParityGateCheck, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": "prompt_parity_gate",
            "generated_at": self.generated_at,
            "status": self.status.value,
            "passed": self.passed,
            "contract_version": self.contract_version,
            "contract_hash": self.contract_hash,
            "baseline_hash": self.baseline_hash,
            "report_suite_hash": self.report_suite_hash,
            "summary": {
                "checks": len(self.checks),
                "passed_checks": sum(1 for item in self.checks if item.passed),
                "failed_checks": sum(1 for item in self.checks if not item.passed),
            },
            "checks": [item.as_dict() for item in self.checks],
        }

    def render_text(self) -> str:
        lines = [
            f"Prompt Parity Gate · {self.status.value}",
            (
                f"contract={self.contract_version}/{self.contract_hash[:12]} · "
                f"baseline={self.baseline_hash[:12]} · "
                f"report={self.report_suite_hash[:12]}"
            ),
        ]
        for item in self.checks:
            lines.append(
                f"  {'ok' if item.passed else 'fail':4s} {item.id}: {item.detail}")
        return "\n".join(lines)


def _gate_check(check_id: str, passed: bool, detail: str) -> ParityGateCheck:
    return ParityGateCheck(check_id, bool(passed), detail)


def evaluate_prompt_parity(report: BehaviorReportPort, baseline: PromptParityBaseline,
                           contract: PromptParityContract = PROMPT_PARITY_CONTRACT) -> PromptParityGateResult:
    """Evaluate a live report against the frozen contract and accepted baseline."""
    checks: list[ParityGateCheck] = []
    report_profile = dict(getattr(report, "profile", {}) or {})
    report_suite_hash = str(report_profile.get("suite_hash", "") or "")
    report_release_id = str(
        report_profile.get("prompt_release_id", "") or "")
    report_release_hash = str(
        report_profile.get("prompt_release_hash", "") or "")
    report_profile_name = str(
        report_profile.get("prompt_profile", "") or "")
    report_profile_hash = str(
        report_profile.get("prompt_profile_hash", "") or "")
    report_calibration_status = str(
        report_profile.get("profile_calibration_status", "") or "uncalibrated")
    case_summary = report.case_summary()
    report_cases = set(case_summary)
    required_cases = set(contract.required_case_ids)
    baseline_cases = baseline.case_map

    checks.append(_gate_check(
        "contract-hash",
        contract.hash == FROZEN_PROMPT_PARITY_CONTRACT_HASH,
        "frozen contract hash matches" if contract.hash == FROZEN_PROMPT_PARITY_CONTRACT_HASH
        else "frozen contract hash changed unexpectedly",
    ))
    checks.append(_gate_check(
        "baseline-schema",
        baseline.schema_version == PROMPT_BASELINE_SCHEMA_VERSION,
        f"baseline schema {baseline.schema_version}",
    ))
    checks.append(_gate_check(
        "benchmark-schema",
        baseline.benchmark_schema_version == int(
            getattr(report, "schema_version", 0) or 0),
        (
            f"report schema {getattr(report, 'schema_version', 0)}; "
            f"baseline schema {baseline.benchmark_schema_version}"
        ),
    ))
    checks.append(_gate_check(
        "baseline-contract",
        baseline.contract_version == contract.version
        and baseline.contract_hash == contract.hash,
        "baseline was created for this contract",
    ))
    checks.append(_gate_check(
        "provider-model",
        baseline.provider == str(getattr(report, "provider", "") or "")
        and baseline.model == str(getattr(report, "model", "") or ""),
        (
            f"report {getattr(report, 'provider', '') or '?'}/"
            f"{getattr(report, 'model', '') or '?'}; "
            f"baseline {baseline.provider or '?'}/{baseline.model or '?'}"
        ),
    ))
    baseline_runner = {
        key: baseline.profile.get(key) for key in _RUNNER_PROFILE_KEYS
    }
    report_runner = {
        key: report_profile.get(key) for key in _RUNNER_PROFILE_KEYS
    }
    frozen_runner_keys = tuple(
        key for key, value in baseline_runner.items() if value is not None)
    missing_runner_keys = tuple(
        key for key in frozen_runner_keys
        if report_runner.get(key) is None)
    runner_mismatches = tuple(
        key for key in frozen_runner_keys
        if report_runner.get(key) is not None
        and report_runner.get(key) != baseline_runner.get(key)
    )
    checks.append(_gate_check(
        "runner-profile",
        not missing_runner_keys and not runner_mismatches,
        (
            "frozen runner/provider settings match"
            if frozen_runner_keys
            else "legacy baseline has no frozen runner/provider fingerprint"
        ) if not missing_runner_keys and not runner_mismatches else (
            "missing runner fields: " + ", ".join(missing_runner_keys)
            if missing_runner_keys else
            "runner drift: " + ", ".join(runner_mismatches)
        ),
    ))
    checks.append(_gate_check(
        "suite-hash",
        bool(report_suite_hash)
        and report_suite_hash == baseline.suite_hash
        and report_suite_hash == FROZEN_PROMPT_PARITY_SUITE_HASH,
        "case manifest, frozen suite and baseline match",
    ))
    checks.append(_gate_check(
        "prompt-release",
        bool(report_release_id)
        and report_release_id == baseline.calibration.release_id
        and bool(report_release_hash)
        and report_release_hash == baseline.calibration.release_hash,
        (
            "report and calibration use the same immutable Prompt release"
            if (
                report_release_id == baseline.calibration.release_id
                and report_release_hash == baseline.calibration.release_hash
            ) else "Prompt release identity changed; rerun calibration"
        ),
    ))
    checks.append(_gate_check(
        "prompt-profile",
        bool(report_profile_name)
        and report_profile_name == baseline.calibration.profile_name
        and bool(report_profile_hash)
        and report_profile_hash == baseline.calibration.profile_hash,
        "resolved model Prompt profile matches calibration evidence",
    ))
    checks.append(_gate_check(
        "calibration-identity",
        baseline.calibration.provider
        == str(getattr(report, "provider", "") or "").strip().lower()
        and baseline.calibration.model
        == str(getattr(report, "model", "") or "").strip().lower()
        and baseline.calibration.contract_hash == contract.hash
        and baseline.calibration.contract_version == contract.version
        and baseline.calibration.suite_hash == report_suite_hash,
        "calibration hash, contract and behavior suite match this report",
    ))
    checks.append(_gate_check(
        "calibration-state",
        report_calibration_status in {"calibrated", "uncalibrated"},
        (
            f"runtime calibration state={report_calibration_status}; "
            "stale configured evidence is never accepted"
        ),
    ))
    checks.append(_gate_check(
        "case-manifest",
        report_cases == required_cases and set(baseline_cases) == required_cases,
        (
            f"report={len(report_cases & required_cases)}/{len(required_cases)}; "
            f"baseline={len(set(baseline_cases) & required_cases)}/{len(required_cases)}"
        ),
    ))

    trial_floor = all(
        _as_int(case_summary.get(case_id, {}).get("trials", 0))
        >= contract.minimum_trials
        for case_id in contract.required_case_ids
    )
    checks.append(_gate_check(
        "trial-floor",
        trial_floor,
        f"every case must have at least {contract.minimum_trials} trials",
    ))

    regressions: list[str] = []
    for case_id in contract.required_case_ids:
        current = case_summary.get(case_id)
        accepted = baseline_cases.get(case_id)
        if current is None or accepted is None:
            regressions.append(case_id)
            continue
        current_rate = _as_float(current.get("pass_rate", 0.0))
        if (
            not bool(current.get("stable", False))
            or not bool(current.get("pass_at_k", False))
            or current_rate + 1e-9 < accepted.pass_rate
        ):
            regressions.append(case_id)
    checks.append(_gate_check(
        "case-regressions",
        not regressions,
        (
            "all cases meet baseline stability/pass@k"
            if not regressions else
            "regressed: " + ", ".join(regressions)
        ),
    ))
    checks.append(_gate_check(
        "report-passed",
        bool(getattr(report, "passed", False)),
        "live behavior report passed every trial",
    ))
    error_summary = report.error_summary() if hasattr(report, "error_summary") else {}
    checks.append(_gate_check(
        "no-runtime-errors",
        not contract.require_no_errors or not error_summary,
        (
            "no runtime errors"
            if not error_summary else
            "runtime errors: " + json.dumps(error_summary, sort_keys=True)
        ),
    ))
    checks.append(_gate_check(
        "content-light",
        contract.content_light and not bool(
            baseline.as_dict().get("contains_prompt_content", True)),
        "report/baseline contain metadata only",
    ))

    passed = all(item.passed for item in checks)
    return PromptParityGateResult(
        schema_version=PROMPT_PARITY_SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(),
        status=(PromptParityStatus.ALIGNED if passed else PromptParityStatus.NOT_ALIGNED),
        passed=passed,
        contract_version=contract.version,
        contract_hash=contract.hash,
        baseline_hash=baseline.hash,
        report_suite_hash=report_suite_hash,
        checks=tuple(checks),
    )


__all__ = [
    "ParityGateCheck",
    "PromptParityGateResult",
    "evaluate_prompt_parity",
]
