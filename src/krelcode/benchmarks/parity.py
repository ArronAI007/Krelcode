"""Frozen Prompt Parity contract and content-light benchmark gates.

Claude Code's Prompt text is not public.  Krelcode therefore defines parity as
an explicit contract over observable architecture and behavior rather than as
string equality with another implementation.  The contract freezes:

* the production Prompt/assembly invariants;
* the live benchmark case manifest and minimum trials;
* the baseline format used for regression gates; and
* the rule that baselines stay content-light.

This module intentionally has no dependency on the live runner.  It can be used
by CI, release tooling and the CLI without importing providers or reading a
workspace.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Mapping, Sequence

from ..agent.prompt_release import ModelProfileCalibration


PROMPT_PARITY_SCHEMA_VERSION = 1
PROMPT_BASELINE_SCHEMA_VERSION = 2
PROMPT_PARITY_CONTRACT_VERSION = "1"
PROMPT_PARITY_MINIMUM_TRIALS = 3


class ParityDimension(str, Enum):
    ARCHITECTURE = "architecture"
    PERMISSIONS = "permissions"
    EVIDENCE = "evidence"
    CACHE = "cache"
    SUBAGENTS = "subagents"
    COMPACT = "compact"
    RECOVERY = "recovery"
    LEAKAGE = "leakage"


class PromptParityStatus(str, Enum):
    ALIGNED = "aligned"
    NOT_ALIGNED = "not_aligned"


@dataclass(frozen=True)
class ParityRequirement:
    """One frozen, testable parity principle."""

    id: str
    dimension: ParityDimension
    title: str
    constraint: str
    case_ids: tuple[str, ...] = ()
    metric_names: tuple[str, ...] = ()
    test_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "dimension", ParityDimension(self.dimension))
        object.__setattr__(self, "case_ids", tuple(dict.fromkeys(
            str(item).strip() for item in self.case_ids if str(item).strip())))
        object.__setattr__(self, "metric_names", tuple(dict.fromkeys(
            str(item).strip() for item in self.metric_names if str(item).strip())))
        object.__setattr__(self, "test_refs", tuple(dict.fromkeys(
            str(item).strip() for item in self.test_refs if str(item).strip())))
        if not self.id or not self.title or not self.constraint:
            raise ValueError("parity requirement 的 id/title/constraint 不能为空")
        if not self.case_ids and not self.test_refs:
            raise ValueError(f"parity requirement 缺少证据: {self.id}")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "dimension": self.dimension.value,
            "title": self.title,
            "constraint": self.constraint,
            "cases": list(self.case_ids),
            "metrics": list(self.metric_names),
            "tests": list(self.test_refs),
        }


_PROMPT_PARITY_CASES: tuple[str, ...] = (
    "read-scope",
    "edit-minimal",
    "edit-verify",
    "review-no-edit",
    "instruction-precedence",
    "evidence-injection",
    "permission-denied",
    "plan-mode-gate",
    "tool-search-deferred",
    "mcp-prompt-injection",
    "failed-test-recovery",
    "bounded-refactor",
    "compact-continuation",
    "resume-workspace-refresh",
    "fork-memory-isolation",
    "second-turn-cache-hit",
)

_PROMPT_PARITY_REQUIREMENTS: tuple[ParityRequirement, ...] = (
    ParityRequirement(
        id="single-prompt-assembly",
        dimension=ParityDimension.ARCHITECTURE,
        title="Single Prompt assembly path",
        constraint=(
            "Main, headless, stream-json, SDK, subagent, resume and compact paths "
            "must use PromptAssembler -> PromptPlan -> ProviderMessages without a "
            "second production assembly implementation."
        ),
        case_ids=(
            "read-scope", "edit-verify", "compact-continuation",
            "resume-workspace-refresh", "second-turn-cache-hit",
        ),
        metric_names=("task_success", "trajectory_scope"),
        test_refs=(
            "tests/test_prompt_assembler.py",
            "tests/test_subagent_prompt_assembly.py",
            "tests/test_step88_request_assembly.py",
        ),
    ),
    ParityRequirement(
        id="layered-instruction-authority",
        dimension=ParityDimension.ARCHITECTURE,
        title="Layered instruction authority",
        constraint=(
            "Managed, Base, User, Project, Directory, persona, mode, catalogue, "
            "Memory Evidence, environment, working context and turn content retain "
            "their fixed source order and trust labels."
        ),
        case_ids=("instruction-precedence", "plan-mode-gate"),
        metric_names=("instruction_precedence_followed", "task_success"),
        test_refs=(
            "tests/test_prompt_fragments.py",
            "tests/test_prompt_observability.py",
        ),
    ),
    ParityRequirement(
        id="permission-before-execution",
        dimension=ParityDimension.PERMISSIONS,
        title="Permission filtering precedes execution",
        constraint=(
            "TurnToolPool and the approval policy filter the same capability boundary. "
            "Denied, plan-only and unadvertised tools cannot execute or mutate the workspace."
        ),
        case_ids=("permission-denied", "plan-mode-gate", "tool-search-deferred"),
        metric_names=("safe_execution", "workspace_protected", "trajectory_scope"),
        test_refs=(
            "tests/test_tool_pool.py",
            "tests/test_safety_policy.py",
            "tests/test_tool_use_context.py",
        ),
    ),
    ParityRequirement(
        id="instruction-evidence-separation",
        dimension=ParityDimension.EVIDENCE,
        title="Instruction and Evidence remain separate",
        constraint=(
            "Memory, web, MCP, retrieved documents and tool results are bounded evidence. "
            "They never become system instructions and require validation against workspace facts."
        ),
        case_ids=("evidence-injection", "mcp-prompt-injection"),
        metric_names=("evidence_resisted", "task_success"),
        test_refs=(
            "tests/test_prompt_assembler.py",
            "tests/test_subagent_prompt_assembly.py",
        ),
    ),
    ParityRequirement(
        id="stable-cache-boundary",
        dimension=ParityDimension.CACHE,
        title="Stable prefix and wire cache boundary",
        constraint=(
            "Dynamic turns, Evidence and attachments cannot enter the stable prefix. "
            "Provider cache negotiation, the observed usage and the Cache Ledger must agree."
        ),
        case_ids=("second-turn-cache-hit",),
        metric_names=("task_success",),
        test_refs=(
            "tests/test_provider_cache_negotiation.py",
            "tests/test_step88_request_assembly.py",
            "tests/test_context_observability.py",
        ),
    ),
    ParityRequirement(
        id="subagent-isolation",
        dimension=ParityDimension.SUBAGENTS,
        title="Subagent context isolation",
        constraint=(
            "A subagent receives instruction layers and selected Evidence, but not the "
            "parent's full history. Sidechain history is not automatically injected into main."
        ),
        test_refs=(
            "tests/test_subagent_prompt_assembly.py",
            "tests/test_fork_context.py",
            "tests/test_subagent_interaction.py",
        ),
    ),
    ParityRequirement(
        id="structured-compact-continuity",
        dimension=ParityDimension.COMPACT,
        title="Structured compact continuity",
        constraint=(
            "A validated structured handoff preserves goal, completed work, decisions, "
            "files, test state, constraints and next steps across compaction."
        ),
        case_ids=("compact-continuation",),
        metric_names=("compact_continuity", "task_success"),
        test_refs=(
            "tests/test_context_compression.py",
            "tests/test_step90_compact_v2.py",
        ),
    ),
    ParityRequirement(
        id="resume-fork-rewind-recovery",
        dimension=ParityDimension.RECOVERY,
        title="Complete context recovery",
        constraint=(
            "Resume refreshes live Git/tree/rules and invalidates stale file facts. Fork "
            "copies context references while isolating session memory. A failed test can be "
            "diagnosed, repaired and verified without unbounded writes."
        ),
        case_ids=(
            "failed-test-recovery", "resume-workspace-refresh",
            "fork-memory-isolation", "bounded-refactor",
        ),
        metric_names=("tests_verified", "workspace_protected", "task_success"),
        test_refs=(
            "tests/test_step107_prompt_parity.py",
            "tests/test_step92_context_resume.py",
            "tests/test_fork_context.py",
        ),
    ),
    ParityRequirement(
        id="content-light-non-leakage",
        dimension=ParityDimension.LEAKAGE,
        title="Content-light reports and no secret leakage",
        constraint=(
            "Benchmark reports and baselines never persist Prompt bodies, final answers, "
            "tool argument bodies, credentials, workspace files or session history."
        ),
        case_ids=tuple(_PROMPT_PARITY_CASES),
        metric_names=("safe_execution",),
        test_refs=(
            "tests/test_step107_prompt_parity.py",
            "tests/test_live_behavior_benchmark.py",
        ),
    ),
)


@dataclass(frozen=True)
class PromptParityContract:
    """Frozen parity policy consumed by benchmarks and release gates."""

    schema_version: int
    version: str
    required_case_ids: tuple[str, ...]
    requirements: tuple[ParityRequirement, ...]
    minimum_trials: int = PROMPT_PARITY_MINIMUM_TRIALS
    require_all_cases: bool = True
    require_no_errors: bool = True
    content_light: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "required_case_ids", tuple(self.required_case_ids))
        object.__setattr__(self, "requirements", tuple(self.requirements))
        known = set(self.required_case_ids)
        if len(known) != len(self.required_case_ids):
            raise ValueError("Prompt Parity benchmark case id 不能重复")
        requirement_ids = [item.id for item in self.requirements]
        if len(set(requirement_ids)) != len(requirement_ids):
            raise ValueError("Prompt Parity requirement id 不能重复")
        if {item.dimension for item in self.requirements} != set(ParityDimension):
            raise ValueError("Prompt Parity contract 必须覆盖全部维度")
        covered_cases: set[str] = set()
        for item in self.requirements:
            unknown = set(item.case_ids) - known
            if unknown:
                raise ValueError(
                    f"parity requirement {item.id} 引用了未知 case: "
                    + ", ".join(sorted(unknown)))
            covered_cases.update(item.case_ids)
        missing = known - covered_cases
        if missing:
            raise ValueError(
                "Prompt Parity contract 存在未覆盖 case: "
                + ", ".join(sorted(missing)))
        if self.minimum_trials < 1:
            raise ValueError("Prompt Parity minimum_trials 必须 >= 1")

    @property
    def hash(self) -> str:
        encoded = json.dumps(
            self._hash_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "required_case_ids": list(self.required_case_ids),
            "requirements": [item.as_dict() for item in self.requirements],
            "minimum_trials": self.minimum_trials,
            "require_all_cases": self.require_all_cases,
            "require_no_errors": self.require_no_errors,
            "content_light": self.content_light,
        }

    def requirement_for_case(self, case_id: str) -> tuple[str, ...]:
        return tuple(
            item.id for item in self.requirements if case_id in item.case_ids)

    def as_dict(self) -> dict[str, object]:
        payload = self._hash_payload()
        payload["contract_hash"] = self.hash
        return payload


PROMPT_PARITY_CONTRACT = PromptParityContract(
    schema_version=PROMPT_PARITY_SCHEMA_VERSION,
    version=PROMPT_PARITY_CONTRACT_VERSION,
    required_case_ids=_PROMPT_PARITY_CASES,
    requirements=_PROMPT_PARITY_REQUIREMENTS,
)

# This value is deliberately pinned.  Any semantic contract change must be an
# explicit review event with a contract-version/hash update in the same commit.
FROZEN_PROMPT_PARITY_CONTRACT_HASH = "8d9c1a9f68a24113ce7a3f42595bf13005a4286ae0f641547775d84e5495262a"

# Full-suite identity consumed as runtime calibration context. Any case
# description/catalog change must deliberately update this reviewed value.
FROZEN_PROMPT_PARITY_SUITE_HASH = (
    "642a4a24d0192f9597caa0eb419c23b6661b62c480c7a13e126065cb0eb6706b"
)


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
        report,
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
            if int(value.get("trials", 0) or 0) < contract.minimum_trials
        )
        if under_sampled:
            raise ValueError(
                f"baseline 每个 case 至少需要 {contract.minimum_trials} trials: "
                + ", ".join(under_sampled)
            )
        cases = tuple(BaselineCaseResult(
            case_id=case_id,
            trials=int(summary[case_id].get("trials", 0) or 0),
            passed=int(summary[case_id].get("passed", 0) or 0),
            failed=int(summary[case_id].get("failed", 0) or 0),
            pass_rate=float(summary[case_id].get("pass_rate", 0.0) or 0.0),
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


def _gate_check(check_id: str, passed: bool, detail: str) -> ParityGateCheck:
    return ParityGateCheck(check_id, bool(passed), detail)


def evaluate_prompt_parity(
    report,
    baseline: PromptParityBaseline,
    contract: PromptParityContract = PROMPT_PARITY_CONTRACT,
) -> PromptParityGateResult:
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
        int(case_summary.get(case_id, {}).get("trials", 0) or 0)
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
        current_rate = float(current.get("pass_rate", 0.0) or 0.0)
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


def contract_summary(contract: PromptParityContract = PROMPT_PARITY_CONTRACT) -> dict[str, object]:
    dimensions: dict[str, list[str]] = {}
    for item in contract.requirements:
        dimensions.setdefault(item.dimension.value, []).append(item.id)
    return {
        "schema_version": contract.schema_version,
        "version": contract.version,
        "contract_hash": contract.hash,
        "minimum_trials": contract.minimum_trials,
        "cases": list(contract.required_case_ids),
        "dimensions": dimensions,
        "requirements": [item.id for item in contract.requirements],
    }


__all__ = [
    "BaselineCaseResult",
    "FROZEN_PROMPT_PARITY_CONTRACT_HASH",
    "FROZEN_PROMPT_PARITY_SUITE_HASH",
    "ParityDimension",
    "ParityGateCheck",
    "ParityRequirement",
    "PROMPT_BASELINE_SCHEMA_VERSION",
    "PROMPT_PARITY_CONTRACT",
    "PROMPT_PARITY_CONTRACT_VERSION",
    "PROMPT_PARITY_MINIMUM_TRIALS",
    "PROMPT_PARITY_SCHEMA_VERSION",
    "PromptParityBaseline",
    "PromptParityContract",
    "PromptParityGateResult",
    "PromptParityStatus",
    "contract_summary",
    "evaluate_prompt_parity",
    "load_prompt_baseline",
    "save_prompt_baseline",
]
