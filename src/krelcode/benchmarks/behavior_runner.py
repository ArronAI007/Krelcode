"""Live-model benchmark runner over the production Krelcode agent loop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from unittest.mock import patch

from .. import __version__
from ..agent.prompts import PromptProfileResolution, resolve_prompt_profile
from ..core.errors import ConfigError, LLMError, LLMHTTPError
from ..core.redact import SecretRedactor
from ..llm.base import LLMProvider
from ..safety.sandbox import SandboxError
from .behavior_cases import BEHAVIOR_CASES
from .behavior_helpers import _check, _elapsed_ms
from .behavior_models import (
    BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
    BehaviorCase,
    BehaviorErrorKind,
    BehaviorMetric,
    BehaviorReport,
    BehaviorTrialResult,
)
from .behavior_session import BehaviorSession
from .parity import PROMPT_PARITY_CONTRACT
from .suite import BENCHMARK_SUITES


def _safe_error(
    error: BaseException,
    redactor: SecretRedactor | None = None,
) -> str:
    """Keep diagnostics useful without copying provider response bodies."""
    if isinstance(error, LLMHTTPError):
        provider = f"[{error.provider}] " if error.provider else ""
        return f"LLMHTTPError: {provider}HTTP {error.status}"
    message = " ".join(str(error).split())
    message = (redactor or SecretRedactor()).redact(message)
    limit = 500 if isinstance(error, ConfigError) else 240
    return f"{type(error).__name__}: {message[:limit]}"


def _error_kind(error: BaseException) -> str:
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return BehaviorErrorKind.TIMEOUT.value
    if isinstance(error, LLMError):
        return BehaviorErrorKind.PROVIDER.value
    if isinstance(error, SandboxError):
        return BehaviorErrorKind.SANDBOX.value
    if isinstance(error, ConfigError):
        return BehaviorErrorKind.CONFIGURATION.value
    return BehaviorErrorKind.INTERNAL.value


class BehaviorBenchmarkRunner:
    """Run live scenarios against a caller-supplied provider factory."""

    def __init__(
        self,
        provider_factory: Callable[[], LLMProvider],
        cases: Sequence[BehaviorCase] = (),
        *,
        trials: int = 1,
        max_steps: int = 14,
        context_max_tokens: int = 30_000,
        sandbox_backend: str = "auto",
        trial_timeout_seconds: float = 300.0,
        provider_name: str = "",
        model_name: str = "",
        prompt_resolution: PromptProfileResolution | None = None,
        provider_profile: Mapping[str, object] | None = None,
        suite_name: str = "custom",
        shard: str = "",
    ) -> None:
        self.provider_factory = provider_factory
        self.cases = tuple(cases or BEHAVIOR_CASES)
        self.trials = max(1, min(int(trials), 50))
        self.max_steps = max(1, int(max_steps))
        self.context_max_tokens = max(4_000, int(context_max_tokens))
        self.sandbox_backend = sandbox_backend
        self.trial_timeout_seconds = max(0.01, float(trial_timeout_seconds))
        self.provider_name = provider_name
        self.model_name = model_name
        self.prompt_resolution = prompt_resolution
        self.provider_profile = dict(provider_profile or {})
        self.suite_name = str(suite_name or "custom")
        self.shard = str(shard or "")

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(case.id for case in self.cases)

    def run(self, case_ids: Sequence[str] | None = None) -> BehaviorReport:
        return asyncio.run(self.run_async(case_ids))

    async def run_async(
        self,
        case_ids: Sequence[str] | None = None,
    ) -> BehaviorReport:
        requested = tuple(case_ids or self.case_ids)
        selected = [
            case for case in self.cases if not case_ids or case.id in requested
        ]
        unknown = sorted(set(requested) - {case.id for case in self.cases})
        if unknown:
            raise ValueError("未知 live benchmark case: " + ", ".join(unknown))
        started = time.monotonic()
        results: list[BehaviorTrialResult] = []
        for case in selected:
            for trial in range(1, self.trials + 1):
                results.append(await self._run_trial_with_timeout(case, trial))
        case_summary = BehaviorReport(
            schema_version=BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
            generated_at="",
            provider=self.provider_name,
            model=self.model_name,
            passed=False,
            results=tuple(results),
            duration_ms=0,
        ).case_summary()
        passed = bool(results) and all(
            bool(item.get("stable")) for item in case_summary.values())
        return BehaviorReport(
            schema_version=BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
            generated_at=datetime.now(timezone.utc).isoformat(),
            provider=self.provider_name,
            model=self.model_name,
            passed=passed,
            results=tuple(results),
            duration_ms=_elapsed_ms(started),
            profile=self._profile(selected),
        )

    def _profile(self, selected: Sequence[BehaviorCase]) -> dict[str, object]:
        suite_payload = json.dumps(
            [case.as_dict() for case in selected],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        resolution = self.prompt_resolution or resolve_prompt_profile(
            self.provider_name,
            self.model_name,
        )
        suite = BENCHMARK_SUITES.get(self.suite_name)
        suite_ids = set(
            suite.select(item.id for item in BEHAVIOR_CASES)
        ) if suite is not None else {item.id for item in selected}
        suite_cases = tuple(
            case for case in BEHAVIOR_CASES if case.id in suite_ids)
        if suite is None:
            suite_cases = tuple(selected)
        profile = {
            "krelcode_version": __version__,
            "python_version": platform.python_version(),
            "platform": platform.system().lower() or sys.platform,
            "suite_hash": hashlib.sha256(suite_payload).hexdigest(),
            "parity_contract_version": PROMPT_PARITY_CONTRACT.version,
            "parity_contract_hash": PROMPT_PARITY_CONTRACT.hash,
            "cases": [case.id for case in selected],
            "suite_name": self.suite_name,
            "suite_size": len(suite_cases),
            "selected_cases": len(selected),
            "shard": self.shard,
            "trials": self.trials,
            "max_steps": self.max_steps,
            "context_max_tokens": self.context_max_tokens,
            "sandbox_backend": self.sandbox_backend,
            "trial_timeout_seconds": self.trial_timeout_seconds,
            "prompt_release_id": resolution.release_id,
            "prompt_release_hash": resolution.release_hash,
            "prompt_profile": resolution.name,
            "prompt_profile_hash": resolution.profile_hash,
            "profile_calibration_id": resolution.calibration_id,
            "profile_calibration_hash": resolution.calibration_hash,
            "profile_calibration_status": resolution.calibration_status,
            "profile_calibration_reason": resolution.calibration_reason,
        }
        profile.update(self.provider_profile)
        return profile

    async def _run_trial_with_timeout(
        self, case: BehaviorCase, trial: int,
    ) -> BehaviorTrialResult:
        started = time.monotonic()
        try:
            return await asyncio.wait_for(
                self._run_trial(case, trial),
                timeout=self.trial_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return BehaviorTrialResult(
                case_id=case.id,
                trial=trial,
                passed=False,
                checks=(),
                duration_ms=_elapsed_ms(started),
                error=(
                    "TimeoutError: trial exceeded "
                    f"{self.trial_timeout_seconds:g}s"),
                error_kind=BehaviorErrorKind.TIMEOUT.value,
            )

    async def _run_trial(
        self, case: BehaviorCase, trial: int,
    ) -> BehaviorTrialResult:
        started = time.monotonic()
        with tempfile.TemporaryDirectory(
            prefix=f"krelcode-live-{case.id}-{trial}-"
        ) as raw_root:
            root = Path(raw_root)
            workspace = root / "workspace"
            home = root / "home"
            workspace.mkdir(parents=True)
            home.mkdir(parents=True)
            managed = home / ".krelcode" / "managed" / "AGENTS.md"
            session: BehaviorSession | None = None
            try:
                memory_entries = case.seed(workspace, home)
                with patch.dict(
                    os.environ,
                    {
                        "HOME": str(home),
                        "KRELCODE_MANAGED_INSTRUCTIONS": str(managed),
                    },
                    clear=False,
                ):
                    provider = self.provider_factory()
                    session = BehaviorSession(
                        workspace,
                        home,
                        provider,
                        max_steps=self.max_steps,
                        context_max_tokens=self.context_max_tokens,
                        keep_recent=(
                            2 if case.id == "compact-continuation" else 10),
                        sandbox_backend=self.sandbox_backend,
                    )
                    await session.build(memory_entries)
                    outcome = await case.execute(session)
                    usage = session.usage.session
                    facts = dict(outcome.facts)
                    facts.update(session.context_facts())
                    checks = list(outcome.checks)
                    if case.expected_workflow:
                        workflow = (
                            session.loop.current_task_workflow()
                            if session.loop is not None else None)
                        actual_workflow = (
                            workflow.task_type.value if workflow is not None else "")
                        confidence = (
                            workflow.classification.confidence
                            if workflow is not None else "")
                        checks.append(_check(
                            BehaviorMetric.WORKFLOW_FOLLOWED,
                            actual_workflow == case.expected_workflow,
                            (
                                f"selected workflow {actual_workflow or 'none'} "
                                f"(expected {case.expected_workflow}, "
                                f"confidence {confidence or 'none'})"
                            ),
                        ))
                        facts["task_workflow"] = actual_workflow
                        facts["task_workflow_confidence"] = confidence
                    extra_usage = dict(outcome.extra_usage)
                    cost_usd = session.usage.cost_usd
                    if outcome.extra_cost_usd is not None:
                        cost_usd = (
                            (cost_usd or 0.0)
                            + float(outcome.extra_cost_usd))
                    return BehaviorTrialResult(
                        case_id=case.id,
                        trial=trial,
                        passed=bool(checks) and all(
                            check.passed for check in checks),
                        checks=tuple(checks),
                        answer=session.redactor.redact(outcome.answer),
                        tools=(
                            *session.observer.tools,
                            *outcome.extra_tools,
                        ),
                        usage={
                            "calls": usage.calls
                            + int(extra_usage.get("calls", 0) or 0),
                            "input_tokens": usage.input
                            + int(extra_usage.get("input_tokens", 0) or 0),
                            "output_tokens": usage.output
                            + int(extra_usage.get("output_tokens", 0) or 0),
                            "total_tokens": usage.total
                            + int(extra_usage.get("total_tokens", 0) or 0),
                            "cache_read": usage.cache_read
                            + int(extra_usage.get("cache_read", 0) or 0),
                            "cache_creation": usage.cache_creation
                            + int(extra_usage.get("cache_creation", 0) or 0),
                        },
                        cost_usd=cost_usd,
                        duration_ms=_elapsed_ms(started),
                        workspace_diff=session.workspace_diff(),
                        facts=facts,
                    )
            except Exception as exc:
                return BehaviorTrialResult(
                    case_id=case.id,
                    trial=trial,
                    passed=False,
                    checks=(),
                    duration_ms=_elapsed_ms(started),
                    error=_safe_error(
                        exc, session.redactor if session is not None else None),
                    error_kind=_error_kind(exc),
                )
