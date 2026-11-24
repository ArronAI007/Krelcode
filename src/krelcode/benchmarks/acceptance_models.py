"""Content-light final-acceptance contract and report data model."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


FINAL_ACCEPTANCE_SCHEMA_VERSION = 1


FINAL_ACCEPTANCE_CONTRACT_VERSION = "1"


FINAL_ACCEPTANCE_LIVE_MAX_AGE_SECONDS = 24 * 60 * 60


class AcceptanceArea(str, Enum):
    ENTRYPOINT = "entrypoint"
    PROVIDER = "provider"
    TOOLS = "tools"
    PROMPT = "prompt"
    CONTEXT = "context"
    RECOVERY = "recovery"
    SUBAGENTS = "subagents"
    EXTENSIONS = "extensions"
    SAFETY = "safety"
    INTERFACES = "interfaces"
    RELEASE = "release"
    LEAKAGE = "leakage"


class AcceptanceEvidenceKind(str, Enum):
    STATIC = "static"
    RUNTIME = "runtime"
    OFFLINE = "offline"
    LIVE = "live"


class AcceptanceCheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    PENDING = "pending"


class FinalAcceptanceStatus(str, Enum):
    ACCEPTED = "accepted"
    FAILED = "failed"
    PENDING_LIVE = "pending_live"


@dataclass(frozen=True)
class AcceptanceRequirement:
    id: str
    area: AcceptanceArea
    title: str
    constraint: str
    evidence_kind: AcceptanceEvidenceKind
    artifacts: tuple[str, ...] = ()
    test_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "area", AcceptanceArea(self.area))
        object.__setattr__(
            self, "evidence_kind", AcceptanceEvidenceKind(self.evidence_kind))
        object.__setattr__(self, "artifacts", tuple(dict.fromkeys(
            str(item).strip() for item in self.artifacts if str(item).strip())))
        object.__setattr__(self, "test_refs", tuple(dict.fromkeys(
            str(item).strip() for item in self.test_refs if str(item).strip())))
        if not self.id or not self.title or not self.constraint:
            raise ValueError("acceptance requirement 缺少 id/title/constraint")
        if not self.artifacts or not self.test_refs:
            raise ValueError(f"acceptance requirement 缺少可核验证据: {self.id}")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "area": self.area.value,
            "title": self.title,
            "constraint": self.constraint,
            "evidence_kind": self.evidence_kind.value,
            "artifacts": list(self.artifacts),
            "tests": list(self.test_refs),
        }


_FINAL_ACCEPTANCE_REQUIREMENTS: tuple[AcceptanceRequirement, ...] = (
    AcceptanceRequirement(
        id="entrypoint-fast-full-dispatch",
        area=AcceptanceArea.ENTRYPOINT,
        title="Fast/full entrypoint dispatch",
        constraint=(
            "--version and lightweight dispatch stay in the process fast path; "
            "all stateful requests cross the complete composition root."
        ),
        evidence_kind=AcceptanceEvidenceKind.RUNTIME,
        artifacts=(
            "src/krelcode/entrypoints/cli.py",
            "src/krelcode/entrypoints/main.py",
        ),
        test_refs=("tests/test_entrypoints.py",),
    ),
    AcceptanceRequirement(
        id="provider-event-stream",
        area=AcceptanceArea.PROVIDER,
        title="Uniform provider event stream",
        constraint=(
            "Providers expose one normalized streaming/tool/usage contract and "
            "wire-specific structures remain inside adapters."
        ),
        evidence_kind=AcceptanceEvidenceKind.RUNTIME,
        artifacts=(
            "src/krelcode/llm/base.py",
            "src/krelcode/llm/registry.py",
            "src/krelcode/llm/providers/mock.py",
        ),
        test_refs=(
            "tests/test_step51_provider_unification.py",
            "tests/test_provider_tool_streaming.py",
        ),
    ),
    AcceptanceRequirement(
        id="tool-contract-dynamic-pool",
        area=AcceptanceArea.TOOLS,
        title="Unified tool contract and dynamic pool",
        constraint=(
            "Execution, model-facing schema, permissions and dynamic exposure "
            "share one tool identity and one per-turn filtered executable pool."
        ),
        evidence_kind=AcceptanceEvidenceKind.RUNTIME,
        artifacts=(
            "src/krelcode/tools/base.py",
            "src/krelcode/tools/prompt.py",
            "src/krelcode/tools/registry.py",
            "src/krelcode/agent/tool_pool.py",
        ),
        test_refs=(
            "tests/test_tool_use_context.py",
            "tests/test_tool_pool.py",
            "tests/test_step109_tool_prompt_spec.py",
        ),
    ),
    AcceptanceRequirement(
        id="single-prompt-assembly",
        area=AcceptanceArea.PROMPT,
        title="Single Prompt assembly path",
        constraint=(
            "Every production surface uses PromptAssembler to obtain a "
            "PromptPlan and provider-neutral instruction/evidence channels."
        ),
        evidence_kind=AcceptanceEvidenceKind.OFFLINE,
        artifacts=("src/krelcode/agent/prompt_assembler.py",),
        test_refs=(
            "tests/test_prompt_assembler.py",
            "tests/test_step88_request_assembly.py",
        ),
    ),
    AcceptanceRequirement(
        id="instruction-evidence-authority",
        area=AcceptanceArea.PROMPT,
        title="Instruction and Evidence separation",
        constraint=(
            "Managed/User/Project/Agent instructions retain authority while "
            "Memory/Web/MCP/Tool/Retrieval remain non-instruction evidence."
        ),
        evidence_kind=AcceptanceEvidenceKind.OFFLINE,
        artifacts=(
            "src/krelcode/agent/evidence.py",
            "src/krelcode/agent/fragments.py",
        ),
        test_refs=(
            "tests/test_prompt_fragments.py",
            "tests/test_step102_memory_evidence.py",
        ),
    ),
    AcceptanceRequirement(
        id="context-cache-request-fidelity",
        area=AcceptanceArea.CONTEXT,
        title="Context budget and provider request fidelity",
        constraint=(
            "Budgeting, cache boundaries, prompt hashes and the final provider "
            "wire are derived from the same assembled request."
        ),
        evidence_kind=AcceptanceEvidenceKind.OFFLINE,
        artifacts=(
            "src/krelcode/agent/context_cache_ledger.py",
            "src/krelcode/llm/cache_planner.py",
        ),
        test_refs=(
            "tests/test_context_cache_ledger.py",
            "tests/test_step108_cache_breakpoint_planner.py",
        ),
    ),
    AcceptanceRequirement(
        id="compact-continuity",
        area=AcceptanceArea.CONTEXT,
        title="Structured compact continuity",
        constraint=(
            "Compaction preserves a machine-verifiable handoff and a recoverable "
            "pre-compact context checkpoint."
        ),
        evidence_kind=AcceptanceEvidenceKind.OFFLINE,
        artifacts=(
            "src/krelcode/agent/compression.py",
            "src/krelcode/session/compact_checkpoint.py",
        ),
        test_refs=(
            "tests/test_step90_structured_compact.py",
            "tests/test_step92_context_resume.py",
        ),
    ),
    AcceptanceRequirement(
        id="session-resume-fork-rewind",
        area=AcceptanceArea.RECOVERY,
        title="Complete session recovery",
        constraint=(
            "Resume refreshes live workspace state; Fork copies references while "
            "isolating session knowledge; Rewind restores history, context and files."
        ),
        evidence_kind=AcceptanceEvidenceKind.OFFLINE,
        artifacts=(
            "src/krelcode/session/restore.py",
            "src/krelcode/session/rewind.py",
            "src/krelcode/session/context_checkpoint.py",
        ),
        test_refs=(
            "tests/test_fork_context.py",
            "tests/test_rewind.py",
            "tests/test_step92_context_resume.py",
        ),
    ),
    AcceptanceRequirement(
        id="subagent-context-isolation",
        area=AcceptanceArea.SUBAGENTS,
        title="Interactive subagent isolation",
        constraint=(
            "Subagents use the same assembly and safety contracts while receiving "
            "only filtered memory/instructions and no unrelated parent history."
        ),
        evidence_kind=AcceptanceEvidenceKind.OFFLINE,
        artifacts=(
            "src/krelcode/subagent/manager.py",
            "src/krelcode/subagent/instructions.py",
            "src/krelcode/subagent/sidechain.py",
        ),
        test_refs=(
            "tests/test_subagent_prompt_assembly.py",
            "tests/test_subagent_interaction.py",
            "tests/test_subagents_advanced.py",
        ),
    ),
    AcceptanceRequirement(
        id="extension-ecosystem",
        area=AcceptanceArea.EXTENSIONS,
        title="Unified extension ecosystem",
        constraint=(
            "Plugin, Tool, Skill, Agent, Command, Hook and MCP components retain "
            "source provenance and participate in deterministic conflict resolution."
        ),
        evidence_kind=AcceptanceEvidenceKind.RUNTIME,
        artifacts=(
            "src/krelcode/extensions.py",
            "src/krelcode/plugins/manager.py",
            "src/krelcode/mcp/supervisor.py",
            "src/krelcode/hooks.py",
        ),
        test_refs=(
            "tests/test_step95_extension_p0.py",
            "tests/test_step96_production_p1.py",
            "tests/test_mcp_streamable.py",
        ),
    ),
    AcceptanceRequirement(
        id="permission-sandbox-enforcement",
        area=AcceptanceArea.SAFETY,
        title="Permission and Sandbox enforcement",
        constraint=(
            "Tool visibility, approval, path policy and command sandboxing must "
            "agree before any mutating operation executes."
        ),
        evidence_kind=AcceptanceEvidenceKind.RUNTIME,
        artifacts=(
            "src/krelcode/safety/policy.py",
            "src/krelcode/safety/sandbox.py",
            "src/krelcode/safety/contract.py",
        ),
        test_refs=(
            "tests/test_safety_policy.py",
            "tests/test_sandbox.py",
            "tests/test_tool_use_context.py",
        ),
    ),
    AcceptanceRequirement(
        id="secret-path-protection",
        area=AcceptanceArea.SAFETY,
        title="Secret path protection and redaction",
        constraint=(
            "Credential files are denied or structurally redacted across file, "
            "search, command, rendering and persisted-session surfaces."
        ),
        evidence_kind=AcceptanceEvidenceKind.RUNTIME,
        artifacts=(
            "src/krelcode/tools/secret_guard.py",
            "src/krelcode/core/redact.py",
        ),
        test_refs=(
            "tests/test_step49_context_redaction.py",
            "tests/test_step50_subagent_redaction.py",
            "tests/test_step94_context_security.py",
        ),
    ),
    AcceptanceRequirement(
        id="all-execution-surfaces",
        area=AcceptanceArea.INTERFACES,
        title="All execution surfaces",
        constraint=(
            "Main, headless, stream-json, SDK, subagent, resume, fork, rewind and "
            "compact surfaces must execute through production code."
        ),
        evidence_kind=AcceptanceEvidenceKind.OFFLINE,
        artifacts=(
            "src/krelcode/session/runner.py",
            "src/krelcode/session/stream_json_runner.py",
            "src/krelcode/sdk.py",
        ),
        test_refs=(
            "tests/test_prompt_benchmark.py",
            "tests/test_sdk.py",
            "tests/test_stream_json.py",
        ),
    ),
    AcceptanceRequirement(
        id="repl-command-lifecycle",
        area=AcceptanceArea.INTERFACES,
        title="REPL and command lifecycle",
        constraint=(
            "The interactive frontend exposes streaming, steering, permissions, "
            "planning, recovery, context, memory, agents, jobs and extension commands."
        ),
        evidence_kind=AcceptanceEvidenceKind.RUNTIME,
        artifacts=(
            "src/krelcode/ui/repl.py",
            "src/krelcode/session/commands/registry.py",
            "src/krelcode/session/commands/builtin.py",
        ),
        test_refs=(
            "tests/test_repl_event_architecture.py",
            "tests/test_command_registry.py",
        ),
    ),
    AcceptanceRequirement(
        id="prompt-release-calibration",
        area=AcceptanceArea.RELEASE,
        title="Immutable Prompt release and calibration",
        constraint=(
            "The production Prompt catalog has a pinned Release hash and model "
            "profiles distinguish calibrated, uncalibrated and stale evidence."
        ),
        evidence_kind=AcceptanceEvidenceKind.RUNTIME,
        artifacts=(
            "src/krelcode/agent/prompt_release.py",
            "src/krelcode/agent/prompts.py",
        ),
        test_refs=("tests/test_step111_prompt_release.py",),
    ),
    AcceptanceRequirement(
        id="live-model-parity-gate",
        area=AcceptanceArea.RELEASE,
        title="Live model behavior gate",
        constraint=(
            "A fresh full 16-case, at least 3-trial live model run must pass the "
            "frozen Prompt Parity contract and a content-light accepted baseline."
        ),
        evidence_kind=AcceptanceEvidenceKind.LIVE,
        artifacts=(
            "src/krelcode/benchmarks/behavior.py",
            "src/krelcode/benchmarks/parity.py",
        ),
        test_refs=(
            "tests/test_live_behavior_benchmark.py",
            "tests/test_step107_prompt_parity.py",
        ),
    ),
    AcceptanceRequirement(
        id="content-light-final-report",
        area=AcceptanceArea.LEAKAGE,
        title="Content-light final report",
        constraint=(
            "Acceptance output persists only IDs, hashes, counts, statuses and "
            "bounded diagnostics, never Prompt/answer/tool/session/credential content."
        ),
        evidence_kind=AcceptanceEvidenceKind.OFFLINE,
        artifacts=(
            "src/krelcode/benchmarks/acceptance.py",
            "src/krelcode/core/redact.py",
        ),
        test_refs=(
            "tests/test_step107_prompt_parity.py",
            "tests/test_step111_prompt_release.py",
            "tests/test_step112_final_acceptance.py",
        ),
    ),
)


@dataclass(frozen=True)
class FinalAcceptanceContract:
    schema_version: int
    version: str
    requirements: tuple[AcceptanceRequirement, ...]
    content_light: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "requirements", tuple(self.requirements))
        ids = [item.id for item in self.requirements]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("final acceptance requirement 不能为空或重复")
        if {item.area for item in self.requirements} != set(AcceptanceArea):
            raise ValueError("final acceptance 必须覆盖全部 capability area")
        if not self.content_light:
            raise ValueError("final acceptance 必须保持 content-light")

    @property
    def hash(self) -> str:
        return _hash_payload(self._hash_payload())

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "content_light": self.content_light,
            "requirements": [item.as_dict() for item in self.requirements],
        }

    def requirement(self, requirement_id: str) -> AcceptanceRequirement:
        for requirement in self.requirements:
            if requirement.id == requirement_id:
                return requirement
        raise ValueError(f"未知 acceptance requirement: {requirement_id}")


FINAL_ACCEPTANCE_CONTRACT = FinalAcceptanceContract(
    schema_version=FINAL_ACCEPTANCE_SCHEMA_VERSION,
    version=FINAL_ACCEPTANCE_CONTRACT_VERSION,
    requirements=_FINAL_ACCEPTANCE_REQUIREMENTS,
)


FROZEN_FINAL_ACCEPTANCE_CONTRACT_HASH = (
    "0a9f92394b6159cdc61f30d6268c384da30dbf84fd9f699dc046b52be4c3443f"
)


@dataclass(frozen=True)
class AcceptanceCheckResult:
    id: str
    area: AcceptanceArea
    status: AcceptanceCheckStatus
    evidence_kind: AcceptanceEvidenceKind
    required: bool
    detail: str
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "area", AcceptanceArea(self.area))
        object.__setattr__(self, "status", AcceptanceCheckStatus(self.status))
        object.__setattr__(
            self, "evidence_kind", AcceptanceEvidenceKind(self.evidence_kind))
        object.__setattr__(self, "evidence", dict(self.evidence))

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "area": self.area.value,
            "status": self.status.value,
            "evidence_kind": self.evidence_kind.value,
            "required": self.required,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class FinalAcceptanceReport:
    schema_version: int
    generated_at: str
    status: FinalAcceptanceStatus
    contract_version: str
    contract_hash: str
    checks: tuple[AcceptanceCheckResult, ...]
    fingerprints: Mapping[str, object]
    duration_ms: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", FinalAcceptanceStatus(self.status))
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "fingerprints", dict(self.fingerprints))

    @property
    def passed(self) -> bool:
        return self.status is FinalAcceptanceStatus.ACCEPTED

    @property
    def exit_code(self) -> int:
        if self.status is FinalAcceptanceStatus.ACCEPTED:
            return 0
        if self.status is FinalAcceptanceStatus.PENDING_LIVE:
            return 2
        return 1

    def summary(self) -> dict[str, int]:
        return {
            "checks": len(self.checks),
            "passed": sum(
                1 for item in self.checks
                if item.status is AcceptanceCheckStatus.PASSED),
            "failed": sum(
                1 for item in self.checks
                if item.status is AcceptanceCheckStatus.FAILED),
            "pending": sum(
                1 for item in self.checks
                if item.status is AcceptanceCheckStatus.PENDING),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": "final_acceptance",
            "generated_at": self.generated_at,
            "status": self.status.value,
            "passed": self.passed,
            "contract_version": self.contract_version,
            "contract_hash": self.contract_hash,
            "summary": self.summary(),
            "fingerprints": dict(self.fingerprints),
            "checks": [item.as_dict() for item in self.checks],
            "contains_prompt_content": False,
            "duration_ms": self.duration_ms,
        }

    def render_text(self) -> str:
        summary = self.summary()
        lines = [
            f"Krelcode Final Acceptance · {self.status.value}",
            (
                f"contract={self.contract_version}/{self.contract_hash[:12]} · "
                f"passed={summary['passed']} failed={summary['failed']} "
                f"pending={summary['pending']}"
            ),
        ]
        for check in self.checks:
            marker = {
                AcceptanceCheckStatus.PASSED: "ok",
                AcceptanceCheckStatus.FAILED: "FAIL",
                AcceptanceCheckStatus.PENDING: "PEND",
            }[check.status]
            lines.append(
                f"  {marker:4s} {check.id}: {check.detail}")
        return "\n".join(lines)


def _hash_payload(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


_FORBIDDEN_REPORT_KEYS = frozenset({
    "answer",
    "api_key",
    "authorization",
    "content",
    "cookie",
    "history",
    "messages",
    "prompt",
    "prompt_body",
    "prompt_text",
    "raw_output",
    "system_prompt",
    "tool_arguments",
})


_FORBIDDEN_REPORT_MARKERS = (
    "Managed benchmark policy",
    "User benchmark preference",
    "Project benchmark policy",
    "Directory benchmark policy",
    "A second benchmark evidence item",
    "Preserve a recoverable compact benchmark scene",
)


_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", re.IGNORECASE),
    re.compile(r"(?i)\b(?:api[_-]?key|token|password)\s*[=:]\s*[^\s,;]{6,}"),
)


def _content_light_violations(payload: object) -> tuple[str, ...]:
    """Reject structural content fields, long blobs and credential-shaped values."""
    violations: set[str] = set()

    def walk(value: object, path: str, depth: int) -> None:
        if depth > 12:
            violations.add(f"{path}:depth")
            return
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                key = str(raw_key)
                normalized = key.strip().lower()
                if normalized in _FORBIDDEN_REPORT_KEYS:
                    violations.add(f"{path}.{key}:field")
                walk(item, f"{path}.{key}", depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]", depth + 1)
            return
        if isinstance(value, str):
            if len(value) > 800:
                violations.add(f"{path}:oversized")
            if any(marker in value for marker in _FORBIDDEN_REPORT_MARKERS):
                violations.add(f"{path}:prompt-content")
            if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
                violations.add(f"{path}:credential-content")

    walk(payload, "$", 0)
    return tuple(sorted(violations))
