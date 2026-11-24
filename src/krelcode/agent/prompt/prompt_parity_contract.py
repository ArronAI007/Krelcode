"""Frozen Prompt Parity contract and observable dimensions."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum


PROMPT_PARITY_SCHEMA_VERSION = 1


PROMPT_BASELINE_SCHEMA_VERSION = 2
PROMPT_PARITY_CONTRACT_VERSION = "1"
PROMPT_PARITY_MINIMUM_TRIALS = 3

_RUNNER_PROFILE_KEYS = (
    "sandbox_backend",
    "context_max_tokens",
    "max_steps",
    "trial_timeout_seconds",
    "provider_base_url_hash",
    "provider_generation_hash",
    "provider_timeout_seconds",
    "provider_max_tokens",
)


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
    "FROZEN_PROMPT_PARITY_CONTRACT_HASH",
    "FROZEN_PROMPT_PARITY_SUITE_HASH",
    "PROMPT_BASELINE_SCHEMA_VERSION",
    "PROMPT_PARITY_CONTRACT",
    "PROMPT_PARITY_CONTRACT_VERSION",
    "PROMPT_PARITY_MINIMUM_TRIALS",
    "PROMPT_PARITY_SCHEMA_VERSION",
    "ParityDimension",
    "ParityRequirement",
    "PromptParityContract",
    "PromptParityStatus",
    "contract_summary",
]
