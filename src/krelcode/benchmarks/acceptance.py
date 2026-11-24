"""Step 112 final, fail-closed acceptance for Krelcode.

The final gate is intentionally stricter than a test summary.  It freezes a
capability contract, checks that every required evidence artifact exists, runs
runtime probes over production objects, executes the offline multi-surface
Prompt benchmark, and finally requires a fresh live-model Prompt Parity Gate.
Without the live gate the best possible status is ``pending_live``.

Reports are content-light: they contain IDs, hashes, statuses, counts and
bounded diagnostics only.  Prompt bodies, answers, tool arguments, session
history, workspace files and credentials never enter this module's output.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Sequence


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

# Pinned after all requirement/evidence metadata has been reviewed.
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


def _check(
    requirement: AcceptanceRequirement,
    passed: bool,
    detail: str,
    evidence: Mapping[str, object] | None = None,
    *,
    pending: bool = False,
) -> AcceptanceCheckResult:
    status = (
        AcceptanceCheckStatus.PENDING if pending else
        AcceptanceCheckStatus.PASSED if passed else
        AcceptanceCheckStatus.FAILED
    )
    return AcceptanceCheckResult(
        id=requirement.id,
        area=requirement.area,
        status=status,
        evidence_kind=requirement.evidence_kind,
        required=True,
        detail=detail,
        evidence=evidence or {},
    )


def _safe_error(error: BaseException) -> str:
    """Return type-only diagnostics; exception text may contain user content."""
    return type(error).__name__


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


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_is_fresh(
    value: datetime | None,
    *,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    if value is None:
        return False
    age = (now - value).total_seconds()
    # Permit a small future clock skew but never an unbounded future timestamp.
    return -300.0 <= age <= max_age_seconds


def _report_metric(report, metric_name: str) -> Mapping[str, object]:
    metrics = report.metric_summary()
    value = metrics.get(metric_name, {})
    return value if isinstance(value, Mapping) else {}


def _report_mode(report, mode_name: str) -> Mapping[str, object]:
    modes = report.mode_summary()
    value = modes.get(mode_name, {})
    return value if isinstance(value, Mapping) else {}


class FinalAcceptanceRunner:
    """Run the deterministic final matrix and optionally consume a live gate."""

    def __init__(
        self,
        contract: FinalAcceptanceContract = FINAL_ACCEPTANCE_CONTRACT,
        *,
        offline_report=None,
        workspace: Path | None = None,
        entrypoint_probe: Callable[[], tuple[bool, str, Mapping[str, object]]] | None = None,
    ) -> None:
        self.contract = contract
        self.offline_report = offline_report
        self.workspace = Path(workspace or Path.cwd()).expanduser().resolve()
        self.entrypoint_probe = entrypoint_probe

    def run(
        self,
        *,
        include_sdk: bool = True,
        sdk_timeout: float = 30.0,
        live_gate=None,
        live_report=None,
        live_max_age_seconds: float = FINAL_ACCEPTANCE_LIVE_MAX_AGE_SECONDS,
    ) -> FinalAcceptanceReport:
        started = time.monotonic()
        offline_error = ""
        if self.offline_report is None:
            try:
                self.offline_report = self._run_offline(
                    include_sdk=include_sdk, sdk_timeout=sdk_timeout)
            except Exception as exc:
                offline_error = _safe_error(exc)
        checks = [self._contract_check()]
        probes = self._runtime_probe_map()
        for requirement in self.contract.requirements:
            checks.append(self._evaluate(
                requirement,
                probes=probes,
                offline_error=offline_error,
                live_gate=live_gate,
                live_report=live_report,
                live_max_age_seconds=live_max_age_seconds,
            ))
        fingerprints = self._fingerprints(live_gate, live_report)
        report = self._build_report(checks, fingerprints, started)

        # The report cannot certify itself without also checking its own
        # serialized structure.  A probe failure is never allowed to smuggle
        # exception text, Prompt content or credential-shaped values into JSON.
        violations = _content_light_violations(report.as_dict())
        if violations:
            revised: list[AcceptanceCheckResult] = []
            for check in checks:
                check_violations = _content_light_violations(check.as_dict())
                if check_violations:
                    check = replace(
                        check,
                        status=(
                            AcceptanceCheckStatus.FAILED
                            if check.id == "content-light-final-report"
                            else check.status
                        ),
                        detail="check evidence redacted by final content policy",
                        evidence={
                            "content_light_violations": len(check_violations),
                        },
                    )
                if check.id == "content-light-final-report":
                    check = replace(
                        check,
                        status=AcceptanceCheckStatus.FAILED,
                        detail=(
                            "final report content-light violations: "
                            f"{len(violations)}"
                        ),
                        evidence={
                            **dict(check.evidence),
                            "content_light_violations": len(violations),
                        },
                    )
                revised.append(check)
            report = self._build_report(revised, fingerprints, started)
        return report

    def _build_report(
        self,
        checks: Sequence[AcceptanceCheckResult],
        fingerprints: Mapping[str, object],
        started: float,
    ) -> FinalAcceptanceReport:
        return FinalAcceptanceReport(
            schema_version=FINAL_ACCEPTANCE_SCHEMA_VERSION,
            generated_at=datetime.now(timezone.utc).isoformat(),
            status=self._status(checks),
            contract_version=self.contract.version,
            contract_hash=self.contract.hash,
            checks=tuple(checks),
            fingerprints=dict(fingerprints),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )

    def _run_offline(self, *, include_sdk: bool, sdk_timeout: float):
        from .prompt import (
            BENCHMARK_CASES,
            PromptBenchmarkMode,
            PromptBenchmarkRunner,
        )

        cases = tuple(
            case for case in BENCHMARK_CASES
            if include_sdk or case.mode is not PromptBenchmarkMode.SDK
        )
        return PromptBenchmarkRunner(
            cases=cases, sdk_timeout=sdk_timeout).run()

    def _contract_check(self) -> AcceptanceCheckResult:
        frozen_ok = (
            bool(FROZEN_FINAL_ACCEPTANCE_CONTRACT_HASH)
            and self.contract.hash == FROZEN_FINAL_ACCEPTANCE_CONTRACT_HASH
        )
        missing: list[str] = []
        for requirement in self.contract.requirements:
            for relative in (*requirement.artifacts, *requirement.test_refs):
                if not (self.workspace / relative).is_file():
                    missing.append(relative)
        passed = frozen_ok and not missing
        detail = (
            "contract hash and required evidence files match"
            if passed else
            "contract hash changed" if not frozen_ok else
            f"missing evidence files: {len(missing)}"
        )
        return AcceptanceCheckResult(
            id="frozen-acceptance-contract",
            area=AcceptanceArea.LEAKAGE,
            status=(
                AcceptanceCheckStatus.PASSED if passed
                else AcceptanceCheckStatus.FAILED),
            evidence_kind=AcceptanceEvidenceKind.STATIC,
            required=True,
            detail=detail,
            evidence={
                "requirements": len(self.contract.requirements),
                "areas": len({item.area for item in self.contract.requirements}),
                "missing_files": tuple(sorted(set(missing))),
                "frozen_hash_matches": frozen_ok,
            },
        )

    def _runtime_probe_map(self) -> dict[str, tuple[bool, str, Mapping[str, object]]]:
        probes: dict[str, tuple[bool, str, Mapping[str, object]]] = {}
        handlers: dict[
            str,
            Callable[[], tuple[bool, str, Mapping[str, object]]],
        ] = {
            "entrypoint-fast-full-dispatch": self._probe_entrypoints,
            "provider-event-stream": self._probe_provider,
            "tool-contract-dynamic-pool": self._probe_tool_pool,
            "extension-ecosystem": self._probe_extensions,
            "permission-sandbox-enforcement": self._probe_safety,
            "secret-path-protection": self._probe_secret_paths,
            "repl-command-lifecycle": self._probe_commands,
            "prompt-release-calibration": self._probe_prompt_release,
        }
        for requirement in self.contract.requirements:
            handler = handlers.get(requirement.id)
            if handler is None:
                continue
            try:
                probes[requirement.id] = handler()
            except Exception as exc:
                probes[requirement.id] = (
                    False,
                    f"runtime probe failed: {_safe_error(exc)}",
                    {},
                )
        return probes

    def _evaluate(
        self,
        requirement: AcceptanceRequirement,
        *,
        probes: Mapping[str, tuple[bool, str, Mapping[str, object]]],
        offline_error: str,
        live_gate,
        live_report,
        live_max_age_seconds: float,
    ) -> AcceptanceCheckResult:
        if requirement.id in probes:
            passed, detail, evidence = probes[requirement.id]
            return _check(requirement, passed, detail, evidence)

        report = self.offline_report
        if requirement.evidence_kind is AcceptanceEvidenceKind.OFFLINE:
            if report is None:
                return _check(
                    requirement,
                    False,
                    "offline benchmark failed: " + (offline_error or "not run"),
                )
            return self._offline_check(requirement, report)

        if requirement.evidence_kind is AcceptanceEvidenceKind.LIVE:
            return self._live_check(
                requirement,
                live_gate=live_gate,
                live_report=live_report,
                max_age_seconds=live_max_age_seconds,
            )

        return _check(
            requirement,
            False,
            "no acceptance evaluator registered for requirement",
        )

    def _offline_check(
        self,
        requirement: AcceptanceRequirement,
        report,
    ) -> AcceptanceCheckResult:
        passed = bool(getattr(report, "passed", False))
        evidence: dict[str, object] = {
            "benchmark_passed": passed,
            "modes": len(report.mode_summary()),
            "metrics": len(report.metric_summary()),
        }
        detail = "offline production-surface matrix passed"

        if requirement.id == "single-prompt-assembly":
            modes = report.mode_summary()
            passed = passed and all(
                bool(value.get("covered"))
                for value in modes.values()
            )
            detail = "all production surfaces assembled and passed"
            evidence["surfaces_covered"] = sum(
                1 for value in modes.values() if value.get("covered"))
        elif requirement.id == "instruction-evidence-authority":
            names = (
                "memory_not_instruction",
                "rule_precedence_stable",
            )
            selected = [_report_metric(report, name) for name in names]
            passed = passed and all(bool(item.get("passed")) for item in selected)
            detail = "instruction precedence and Evidence boundary passed"
        elif requirement.id == "context-cache-request-fidelity":
            names = ("stable_prefix_isolated", "provider_hash_exact")
            selected = [_report_metric(report, name) for name in names]
            passed = passed and all(bool(item.get("passed")) for item in selected)
            detail = "cache prefix and provider request hashes matched"
        elif requirement.id == "compact-continuity":
            metric = _report_metric(report, "compact_recoverable")
            passed = passed and bool(metric.get("passed"))
            detail = "compact checkpoint and handoff recovery passed"
        elif requirement.id == "session-resume-fork-rewind":
            modes = (
                _report_mode(report, "resume"),
                _report_mode(report, "fork"),
                _report_mode(report, "rewind"),
            )
            passed = passed and all(
                bool(item.get("covered")) and int(item.get("failed", 0) or 0) == 0
                for item in modes
            )
            detail = "resume, fork and rewind production surfaces passed"
        elif requirement.id == "subagent-context-isolation":
            names = (
                "subagent_history_isolated",
                "subagent_memory_evidence",
            )
            selected = [_report_metric(report, name) for name in names]
            passed = passed and all(bool(item.get("passed")) for item in selected)
            detail = "subagent history and memory boundaries passed"
        elif requirement.id == "all-execution-surfaces":
            modes = report.mode_summary()
            passed = passed and len(modes) == 9 and all(
                bool(item.get("covered")) and int(item.get("failed", 0) or 0) == 0
                for item in modes.values()
            )
            detail = "all nine execution surfaces passed"
        elif requirement.id == "content-light-final-report":
            violations = _content_light_violations(report.as_dict())
            passed = passed and not violations
            detail = (
                "offline report is content-light"
                if not violations else
                f"offline report content-light violations: {len(violations)}"
            )
            evidence["content_light_violations"] = len(violations)
        return _check(requirement, passed, detail, evidence)

    def _live_check(
        self,
        requirement: AcceptanceRequirement,
        *,
        live_gate,
        live_report,
        max_age_seconds: float,
    ) -> AcceptanceCheckResult:
        if live_gate is None:
            return _check(
                requirement,
                False,
                "live model gate not provided; final acceptance remains pending_live",
                {"gate_present": False},
                pending=True,
            )
        if live_report is None:
            return _check(
                requirement,
                False,
                "live gate has no source report; fresh evidence cannot be verified",
                {"gate_present": True, "report_present": False},
            )

        from .parity import (
            FROZEN_PROMPT_PARITY_CONTRACT_HASH,
            FROZEN_PROMPT_PARITY_SUITE_HASH,
            PROMPT_PARITY_CONTRACT,
            PROMPT_PARITY_SCHEMA_VERSION,
        )

        expected_gate_checks = {
            "contract-hash",
            "baseline-schema",
            "benchmark-schema",
            "baseline-contract",
            "provider-model",
            "suite-hash",
            "prompt-release",
            "prompt-profile",
            "calibration-identity",
            "calibration-state",
            "case-manifest",
            "trial-floor",
            "case-regressions",
            "report-passed",
            "no-runtime-errors",
            "content-light",
        }
        issues: list[str] = []
        now = datetime.now(timezone.utc)
        max_age = max(1.0, float(max_age_seconds))

        gate_status = str(getattr(
            getattr(live_gate, "status", ""), "value",
            getattr(live_gate, "status", "")) or "")
        gate_checks = tuple(getattr(live_gate, "checks", ()) or ())
        gate_check_map = {
            str(getattr(check, "id", "") or ""): bool(
                getattr(check, "passed", False))
            for check in gate_checks
        }
        gate_contract_hash = str(
            getattr(live_gate, "contract_hash", "") or "")
        gate_suite_hash = str(
            getattr(live_gate, "report_suite_hash", "") or "")
        baseline_hash = str(getattr(live_gate, "baseline_hash", "") or "")

        if (
            not bool(getattr(live_gate, "passed", False))
            or gate_status != "aligned"
            or not gate_check_map
            or not all(gate_check_map.values())
        ):
            issues.append("gate-not-aligned")
        if int(getattr(live_gate, "schema_version", 0) or 0) != (
            PROMPT_PARITY_SCHEMA_VERSION
        ):
            issues.append("gate-schema")
        if (
            str(getattr(live_gate, "contract_version", "") or "")
            != PROMPT_PARITY_CONTRACT.version
            or gate_contract_hash != FROZEN_PROMPT_PARITY_CONTRACT_HASH
        ):
            issues.append("gate-contract")
        if gate_suite_hash != FROZEN_PROMPT_PARITY_SUITE_HASH:
            issues.append("gate-suite")
        if not re.fullmatch(r"[0-9a-f]{64}", baseline_hash):
            issues.append("baseline-hash")
        missing_checks = expected_gate_checks - set(gate_check_map)
        if missing_checks:
            issues.append("gate-check-manifest")

        report_profile = dict(getattr(live_report, "profile", {}) or {})
        report_suite_hash = str(
            report_profile.get("suite_hash", "") or "")
        report_contract_hash = str(
            report_profile.get("parity_contract_hash", "") or "")
        report_contract_version = str(
            report_profile.get("parity_contract_version", "") or "")
        calibration_status = str(
            report_profile.get("profile_calibration_status", "") or "")
        provider = str(getattr(live_report, "provider", "") or "").strip().lower()
        model = str(getattr(live_report, "model", "") or "").strip().lower()

        if not bool(getattr(live_report, "passed", False)):
            issues.append("report-not-passed")
        if provider in {"", "mock", "scripted"} or not model:
            issues.append("live-provider")
        if (
            report_suite_hash != FROZEN_PROMPT_PARITY_SUITE_HASH
            or report_suite_hash != gate_suite_hash
        ):
            issues.append("report-suite")
        if (
            report_contract_version != PROMPT_PARITY_CONTRACT.version
            or report_contract_hash != FROZEN_PROMPT_PARITY_CONTRACT_HASH
        ):
            issues.append("report-contract")
        if calibration_status not in {"calibrated", "uncalibrated"}:
            issues.append("calibration-state")

        case_summary = live_report.case_summary()
        required_cases = set(PROMPT_PARITY_CONTRACT.required_case_ids)
        if set(case_summary) != required_cases:
            issues.append("case-manifest")
        for case_id in PROMPT_PARITY_CONTRACT.required_case_ids:
            value = case_summary.get(case_id, {})
            trials = int(value.get("trials", 0) or 0)
            if (
                trials < PROMPT_PARITY_CONTRACT.minimum_trials
                or int(value.get("failed", 0) or 0) != 0
                or not bool(value.get("stable", False))
                or not bool(value.get("pass_at_k", False))
            ):
                issues.append("case-quality")
                break
        if live_report.error_summary():
            issues.append("runtime-errors")

        report_generated = _parse_utc_timestamp(
            getattr(live_report, "generated_at", ""))
        gate_generated = _parse_utc_timestamp(
            getattr(live_gate, "generated_at", ""))
        report_fresh = _timestamp_is_fresh(
            report_generated, now=now, max_age_seconds=max_age)
        gate_fresh = _timestamp_is_fresh(
            gate_generated, now=now, max_age_seconds=max_age)
        if not report_fresh or not gate_fresh:
            issues.append("stale-live-evidence")
        if report_generated and gate_generated:
            gate_delay = (gate_generated - report_generated).total_seconds()
            if gate_delay < -300 or gate_delay > 300:
                issues.append("gate-report-time")

        unique_issues = tuple(dict.fromkeys(issues))
        passed = not unique_issues
        detail = (
            "fresh live-model Parity Gate and full 16-case/3-trial report verified"
            if passed else
            "fresh live acceptance failed: " + ", ".join(unique_issues[:6])
        )
        return _check(
            requirement,
            passed,
            detail,
            {
                "gate_present": True,
                "report_present": True,
                "gate_status": gate_status,
                "gate_passed": bool(getattr(live_gate, "passed", False)),
                "gate_checks": len(gate_checks),
                "gate_contract_hash": gate_contract_hash,
                "baseline_hash_present": bool(baseline_hash),
                "report_suite_hash": report_suite_hash,
                "provider": provider,
                "model": model,
                "cases": len(case_summary),
                "minimum_trials": PROMPT_PARITY_CONTRACT.minimum_trials,
                "report_fresh": report_fresh,
                "gate_fresh": gate_fresh,
                "issues": unique_issues,
            },
        )

    def _status(
        self,
        checks: Sequence[AcceptanceCheckResult],
    ) -> FinalAcceptanceStatus:
        required = [item for item in checks if item.required]
        if any(item.status is AcceptanceCheckStatus.FAILED for item in required):
            return FinalAcceptanceStatus.FAILED
        if any(item.status is AcceptanceCheckStatus.PENDING for item in required):
            return FinalAcceptanceStatus.PENDING_LIVE
        return FinalAcceptanceStatus.ACCEPTED

    def _fingerprints(self, live_gate, live_report=None) -> dict[str, object]:
        from ..agent.prompts import (
            CURRENT_PROMPT_RELEASE,
            FROZEN_PROMPT_RELEASE_HASH,
        )
        from .parity import (
            FROZEN_PROMPT_PARITY_CONTRACT_HASH,
            FROZEN_PROMPT_PARITY_SUITE_HASH,
        )

        offline_hash = ""
        if self.offline_report is not None:
            offline_hash = _hash_payload(self.offline_report.as_dict())
        live_report_hash = ""
        live_report_suite_hash = ""
        if live_report is not None:
            live_payload = live_report.as_dict(include_content=False)
            live_report_hash = _hash_payload(live_payload)
            live_report_suite_hash = str(
                dict(getattr(live_report, "profile", {}) or {}).get(
                    "suite_hash", "") or "")
        live_gate_hash = ""
        if live_gate is not None and callable(
            getattr(live_gate, "as_dict", None)
        ):
            live_gate_hash = _hash_payload(live_gate.as_dict())
        return {
            "prompt_release_id": CURRENT_PROMPT_RELEASE.release_id,
            "prompt_release_hash": CURRENT_PROMPT_RELEASE.release_hash,
            "frozen_prompt_release_hash": FROZEN_PROMPT_RELEASE_HASH,
            "prompt_parity_contract_hash": FROZEN_PROMPT_PARITY_CONTRACT_HASH,
            "prompt_parity_suite_hash": FROZEN_PROMPT_PARITY_SUITE_HASH,
            "offline_benchmark_hash": offline_hash,
            "live_report_hash": live_report_hash,
            "live_report_suite_hash": live_report_suite_hash,
            "live_gate_hash": live_gate_hash,
            "live_gate_present": live_gate is not None,
            "live_gate_passed": bool(getattr(live_gate, "passed", False)),
        }

    # -- runtime probes ---------------------------------------------------------

    def _probe_entrypoints(self):
        if self.entrypoint_probe is not None:
            return self.entrypoint_probe()
        # Entrypoints are the process composition boundary above benchmarks.
        # Runtime discovery keeps the static dependency graph acyclic while the
        # acceptance probe still exercises the real callables.
        cli = importlib.import_module("krelcode.entrypoints.cli")
        main = importlib.import_module("krelcode.entrypoints.main")
        passed = callable(cli.main) and callable(main.main)
        return passed, "fast and full entrypoints are callable", {
            "fast_path_callable": callable(cli.main),
            "full_path_callable": callable(main.main),
        }

    def _probe_provider(self):
        from ..core.messages import Message
        from ..llm.base import StreamFinished, TextDelta
        from ..llm.providers.mock import MockProvider

        async def collect_events():
            provider = MockProvider(scripted=["acceptance"], chunk_size=2)
            return [
                event async for event in provider.stream(
                    [Message.user("final acceptance probe")])
            ]

        events = asyncio.run(collect_events())
        deltas = sum(isinstance(event, TextDelta) for event in events)
        finished = sum(isinstance(event, StreamFinished) for event in events)
        passed = deltas >= 2 and finished == 1
        return passed, "provider emitted normalized deltas and finish event", {
            "events": len(events),
            "text_deltas": deltas,
            "stream_finished": finished,
        }

    def _probe_tool_pool(self):
        from ..agent.tool_pool import TurnToolPool
        from ..contracts.tool import PermissionMode, RuntimeSnapshot
        from ..tools.builtin.read_file import ReadFileTool
        from ..tools.builtin.write_file import WriteFileTool
        from ..tools.registry import ToolRegistry

        registry = ToolRegistry([ReadFileTool(), WriteFileTool()])
        pool = TurnToolPool(
            registry,
            runtime=RuntimeSnapshot(permission_mode=PermissionMode.READ_ONLY),
        )
        passed = (
            pool.names() == ["read_file"]
            and pool.get("write_file") is None
            and pool.excluded_reason("write_file") is not None
            and all(
                spec.name == name for name, spec in (
                    (entry.name, entry.prompt_spec)
                    for entry in pool.catalog_entries()
                )
            )
        )
        return passed, "dynamic tool pool filtered mutation and preserved identity", {
            "eligible": len(pool.names()),
            "excluded": len(pool.excluded_items()),
            "read_only": bool(pool.descriptor("read_file").read_only),
        }

    def _probe_extensions(self):
        from ..extensions import ExtensionKind, ExtensionRegistry
        from ..session.commands.types import CommandSource

        extension_kinds = {item.value for item in ExtensionKind}
        required_extensions = {
            "plugin", "tool", "skill", "agent", "command", "hook", "mcp",
        }
        command_sources = {item.value for item in CommandSource}
        required_sources = {"builtin", "custom", "skill", "plugin", "mcp"}
        registry = ExtensionRegistry()
        passed = (
            extension_kinds == required_extensions
            and command_sources == required_sources
            and registry is not None
        )
        return passed, "extension kinds and command sources are complete", {
            "extension_kinds": len(extension_kinds),
            "command_sources": len(command_sources),
        }

    def _probe_safety(self):
        from ..agent.tool_pool import TurnToolPool
        from ..safety.policy import classify_protected_path
        from ..contracts.tool import PermissionMode, RuntimeSnapshot
        from ..tools.builtin.write_file import WriteFileTool
        from ..tools.registry import ToolRegistry

        protected = classify_protected_path(
            ".env", self.workspace)
        pool = TurnToolPool(
            ToolRegistry([WriteFileTool()]),
            runtime=RuntimeSnapshot(permission_mode=PermissionMode.PLAN),
        )
        passed = bool(protected) and pool.get("write_file") is None
        return passed, "protected paths and Plan mode block mutation", {
            "protected_path_classified": bool(protected),
            "plan_mutation_excluded": pool.get("write_file") is None,
        }

    def _probe_secret_paths(self):
        from ..core.secret_guard import classify_secret_path

        secret_paths = (".env", "credentials.json", ".ssh/id_rsa", "token.json")
        classified = sum(
            1 for path in secret_paths
            if classify_secret_path(path, self.workspace) is not None
        )
        safe_path = classify_secret_path("src/main.py", self.workspace) is None
        passed = classified == len(secret_paths) and safe_path
        return passed, "secret paths classified and normal source allowed", {
            "classified_secret_paths": classified,
            "required_secret_paths": len(secret_paths),
            "normal_source_allowed": safe_path,
        }

    def _probe_commands(self):
        from ..session.commands.builtin import builtin_command_registry

        registry = builtin_command_registry()
        names = {item.name for item in registry.list()}
        required = {
            "help", "plan", "permissions", "mcp", "agents", "jobs", "skills",
            "plugins", "context", "memory", "compact", "model", "resume",
            "fork", "rewind", "exit",
        }
        missing = sorted(required - names)
        passed = not missing
        return passed, "REPL command lifecycle is complete", {
            "commands": len(names),
            "required_commands": len(required),
            "missing": tuple(missing),
        }

    def _probe_prompt_release(self):
        from ..agent.prompts import (
            CURRENT_PROMPT_RELEASE,
            FROZEN_PROMPT_RELEASE_HASH,
            resolve_prompt_profile,
        )

        resolution = resolve_prompt_profile(
            "deepseek", "deepseek-chat", configured="deepseek")
        release_ok = (
            CURRENT_PROMPT_RELEASE.release_hash == FROZEN_PROMPT_RELEASE_HASH
            and resolution.release_hash == FROZEN_PROMPT_RELEASE_HASH
        )
        status_ok = resolution.calibration_status in {
            "calibrated", "uncalibrated", "stale",
        }
        passed = release_ok and status_ok and bool(resolution.profile_hash)
        return passed, "Prompt release identity and calibration states are valid", {
            "release_id": resolution.release_id,
            "profiles": len(CURRENT_PROMPT_RELEASE.profiles),
            "calibration_status": resolution.calibration_status,
            "release_hash_matches": release_ok,
        }


def run_offline_acceptance(
    *,
    json_output: bool = False,
    include_sdk: bool = True,
    sdk_timeout: float = 30.0,
    workspace: Path | None = None,
    live_gate=None,
    live_report=None,
    live_max_age_seconds: float = FINAL_ACCEPTANCE_LIVE_MAX_AGE_SECONDS,
) -> int:
    report = FinalAcceptanceRunner(workspace=workspace).run(
        include_sdk=include_sdk,
        sdk_timeout=sdk_timeout,
        live_gate=live_gate,
        live_report=live_report,
        live_max_age_seconds=live_max_age_seconds,
    )
    if json_output:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render_text())
    return report.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m krelcode.benchmarks.acceptance",
        description=(
            "Run deterministic final acceptance. A live Prompt Parity Gate is "
            "still required for accepted status."
        ),
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-sdk", action="store_true")
    parser.add_argument("--sdk-timeout", type=float, default=30.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    return run_offline_acceptance(
        json_output=args.json,
        include_sdk=not args.no_sdk,
        sdk_timeout=args.sdk_timeout,
    )


__all__ = [
    "AcceptanceArea",
    "AcceptanceCheckResult",
    "AcceptanceCheckStatus",
    "AcceptanceEvidenceKind",
    "AcceptanceRequirement",
    "FINAL_ACCEPTANCE_CONTRACT",
    "FINAL_ACCEPTANCE_CONTRACT_VERSION",
    "FINAL_ACCEPTANCE_LIVE_MAX_AGE_SECONDS",
    "FINAL_ACCEPTANCE_SCHEMA_VERSION",
    "FROZEN_FINAL_ACCEPTANCE_CONTRACT_HASH",
    "FinalAcceptanceContract",
    "FinalAcceptanceReport",
    "FinalAcceptanceRunner",
    "FinalAcceptanceStatus",
    "run_offline_acceptance",
]


if __name__ == "__main__":
    raise SystemExit(main())
