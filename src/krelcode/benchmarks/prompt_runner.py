"""Prompt benchmark runner with late-bound scenario handlers."""

from __future__ import annotations

import asyncio
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .prompt_cases import BENCHMARK_CASES
from .prompt_helpers import _elapsed_ms, _safe_error, _safe_mapping
from .prompt_models import (
    PROMPT_BENCHMARK_SCHEMA_VERSION,
    PromptBenchmarkCase,
    PromptBenchmarkMode,
    PromptBenchmarkReport,
    PromptBenchmarkResult,
    _ScenarioOutcome,
)


class PromptBenchmarkRunner:
    """Run deterministic, isolated prompt/context benchmark cases."""

    def __init__(
        self,
        cases: Sequence[PromptBenchmarkCase] = BENCHMARK_CASES,
        *,
        sdk_timeout: float = 30.0,
    ) -> None:
        self.cases = tuple(cases)
        self.sdk_timeout = max(1.0, float(sdk_timeout))

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(case.id for case in self.cases)

    def run(self, case_ids: Sequence[str] | None = None) -> PromptBenchmarkReport:
        return asyncio.run(self.run_async(case_ids))

    async def run_async(
        self, case_ids: Sequence[str] | None = None,
    ) -> PromptBenchmarkReport:
        requested = tuple(case_ids or self.case_ids)
        selected = [
            case for case in self.cases
            if not case_ids or case.id in requested
        ]
        unknown = sorted(set(requested) - {case.id for case in self.cases})
        if unknown:
            raise ValueError("未知 benchmark case: " + ", ".join(unknown))
        started = time.monotonic()
        results = []
        for case in selected:
            case_started = time.monotonic()
            try:
                with tempfile.TemporaryDirectory(
                    prefix=f"krelcode-prompt-{case.id}-"
                ) as raw_root:
                    outcome = await self._run_case(case, Path(raw_root))
                checks = tuple(outcome.checks)
                passed = bool(checks) and all(item.passed for item in checks)
                result = PromptBenchmarkResult(
                    case_id=case.id,
                    mode=case.mode,
                    passed=passed,
                    checks=checks,
                    prompt_plan=(
                        outcome.prompt_plan.as_dict()
                        if outcome.prompt_plan is not None else {}),
                    ledger=(
                        outcome.ledger.as_dict()
                        if outcome.ledger is not None else {}),
                    facts=_safe_mapping(outcome.facts),
                    duration_ms=_elapsed_ms(case_started),
                )
            except Exception as exc:  # keep one case from hiding the suite
                result = PromptBenchmarkResult(
                    case_id=case.id,
                    mode=case.mode,
                    passed=False,
                    error=_safe_error(exc),
                    duration_ms=_elapsed_ms(case_started),
                )
            results.append(result)
        report = PromptBenchmarkReport(
            schema_version=PROMPT_BENCHMARK_SCHEMA_VERSION,
            generated_at=datetime.now(timezone.utc).isoformat(),
            passed=bool(results) and all(item.passed for item in results),
            results=tuple(results),
            duration_ms=_elapsed_ms(started),
        )
        return report

    def _scenario_handlers(
        self,
    ) -> dict[PromptBenchmarkMode, Callable[[Path], object]]:
        from .prompt_scenarios import (
            _run_compact,
            _run_fork,
            _run_headless,
            _run_main,
            _run_resume,
            _run_rewind,
            _run_sdk,
            _run_stream_json,
            _run_subagent,
        )

        return {
            PromptBenchmarkMode.MAIN: _run_main,
            PromptBenchmarkMode.HEADLESS: _run_headless,
            PromptBenchmarkMode.STREAM_JSON: _run_stream_json,
            PromptBenchmarkMode.SDK: lambda path: _run_sdk(
                path, timeout=self.sdk_timeout),
            PromptBenchmarkMode.SUBAGENT: _run_subagent,
            PromptBenchmarkMode.RESUME: _run_resume,
            PromptBenchmarkMode.FORK: _run_fork,
            PromptBenchmarkMode.REWIND: _run_rewind,
            PromptBenchmarkMode.COMPACT: _run_compact,
        }

    async def _run_case(
        self, case: PromptBenchmarkCase, root: Path,
    ) -> _ScenarioOutcome:
        outcome = self._scenario_handlers()[case.mode](root)
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        if not isinstance(outcome, _ScenarioOutcome):
            raise TypeError(f"{case.id} 返回了非法 benchmark outcome")
        return outcome
