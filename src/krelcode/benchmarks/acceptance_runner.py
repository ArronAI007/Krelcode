"""Final acceptance probes and fail-closed report construction."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .acceptance_checks import (
    _check,
    _parse_utc_timestamp,
    _report_metric,
    _report_mode,
    _safe_error,
    _timestamp_is_fresh,
)
from .acceptance_models import (
    FINAL_ACCEPTANCE_CONTRACT,
    FINAL_ACCEPTANCE_LIVE_MAX_AGE_SECONDS,
    FINAL_ACCEPTANCE_SCHEMA_VERSION,
    FROZEN_FINAL_ACCEPTANCE_CONTRACT_HASH,
    AcceptanceArea,
    AcceptanceCheckResult,
    AcceptanceCheckStatus,
    AcceptanceEvidenceKind,
    AcceptanceRequirement,
    FinalAcceptanceContract,
    FinalAcceptanceReport,
    FinalAcceptanceStatus,
    _content_light_violations,
    _hash_payload,
)


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
        from .prompt_cases import BENCHMARK_CASES
        from .prompt_models import PromptBenchmarkMode
        from .prompt_runner import PromptBenchmarkRunner

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

        from ..agent.prompt.prompt_parity import (
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
        from ..agent.prompt.prompt_parity import (
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
        # Exercise the real process boundary without importing it from the
        # benchmark package. Subprocess probes preserve one-way dependencies.
        env = dict(os.environ)
        env.pop("PYTHONHOME", None)
        source_root = str((self.workspace / "src").resolve())
        existing_path = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            item for item in (source_root, existing_path) if item)
        fast = subprocess.run(
            [sys.executable, "-m", "krelcode", "--version"],
            cwd=self.workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        full = subprocess.run(
            [sys.executable, "-m", "krelcode", "--help"],
            cwd=self.workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        fast_ok = fast.returncode == 0 and fast.stdout.startswith("krelcode ")
        full_ok = full.returncode == 0 and "usage: krelcode" in full.stdout
        return fast_ok and full_ok, "fast and full entrypoints are callable", {
            "fast_path_callable": fast_ok,
            "full_path_callable": full_ok,
            "fast_path_exit_code": fast.returncode,
            "full_path_exit_code": full.returncode,
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
        from ..agent.turn.tool_pool import TurnToolPool
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
        from ..agent.turn.tool_pool import TurnToolPool
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
