"""Deterministic Prompt/Context benchmark CLI and compatibility surface.

The scenarios, fixture and runner live in focused modules.  This facade retains
the historical ``krelcode.benchmarks.prompt`` monkeypatch seams while keeping
the executable entry point and report rendering stable.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .prompt_cases import BENCHMARK_CASES
from .prompt_fixture import _BenchmarkFixture, _BenchmarkProvider
from .prompt_helpers import (
    _check,
    _elapsed_ms,
    _message_payload,
    _provider_hash_is_exact,
    _safe_error,
    _safe_mapping,
    _safe_report_value,
)
from .prompt_models import (
    PROMPT_BENCHMARK_SCHEMA_VERSION,
    PromptBenchmarkCase,
    PromptBenchmarkCheck,
    PromptBenchmarkMetric,
    PromptBenchmarkMode,
    PromptBenchmarkReport,
    PromptBenchmarkResult,
    _ScenarioOutcome,
)
from .prompt_runner import PromptBenchmarkRunner as _CorePromptBenchmarkRunner
from .prompt_scenarios import _UsageTrackerFactory


async def _run_main(root: Path) -> _ScenarioOutcome:
    from .prompt_scenarios import _run_main as implementation

    return await implementation(root)


async def _run_headless(root: Path) -> _ScenarioOutcome:
    from .prompt_scenarios import _run_headless as implementation

    return await implementation(root)


async def _run_stream_json(root: Path) -> _ScenarioOutcome:
    from .prompt_scenarios import _run_stream_json as implementation

    return await implementation(root)


async def _run_sdk(root: Path, *, timeout: float) -> _ScenarioOutcome:
    from .prompt_scenarios import _run_sdk as implementation

    return await implementation(root, timeout=timeout)


async def _run_subagent(root: Path) -> _ScenarioOutcome:
    from .prompt_scenarios import _run_subagent as implementation

    return await implementation(root)


async def _run_resume(root: Path) -> _ScenarioOutcome:
    from .prompt_scenarios import _run_resume as implementation

    return await implementation(root)


async def _run_fork(root: Path) -> _ScenarioOutcome:
    from .prompt_scenarios import _run_fork as implementation

    return await implementation(root)


async def _run_rewind(root: Path) -> _ScenarioOutcome:
    from .prompt_scenarios import _run_rewind as implementation

    return await implementation(root)


async def _run_compact(root: Path) -> _ScenarioOutcome:
    from .prompt_scenarios import _run_compact as implementation

    return await implementation(root)


def _compact_summary_json() -> str:
    from .prompt_scenarios import _compact_summary_json as implementation

    return implementation()


class PromptBenchmarkRunner(_CorePromptBenchmarkRunner):
    """Compatibility runner whose handler globals remain monkeypatchable."""

    def _scenario_handlers(self):
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


def _text_report(report: PromptBenchmarkReport) -> str:
    lines = [
        f"Krelcode Prompt Benchmark v{report.schema_version}",
        (
            f"cases: {len(report.results)} · "
            f"passed: {sum(1 for item in report.results if item.passed)} · "
            f"failed: {sum(1 for item in report.results if not item.passed)}"
        ),
    ]
    for result in report.results:
        status = "PASS" if result.passed else "FAIL"
        lines.append(f"[{status}] {result.case_id} ({result.mode.value})")
        if result.error:
            lines.append(f"  error: {result.error}")
        for check in result.checks:
            marker = "ok" if check.passed else "fail"
            lines.append(f"  {marker:4s} {check.metric.value}: {check.detail}")
    metrics = report.metric_summary()
    lines.append("metrics:")
    for name, value in metrics.items():
        lines.append(
            f"  {name}: {value['passed_checks']}/{value['checks']}"
            + ("" if value["passed"] else " (not satisfied)")
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m krelcode.benchmarks.prompt",
        description=(
            "Run the offline Krelcode Prompt/Context suite or the --live behavior suite."
        ),
    )
    parser.add_argument("--case", action="append", default=[],
                        help="只运行指定 case，可重复指定")
    parser.add_argument("--json", action="store_true",
                        help="输出机器可读 JSON 报告")
    parser.add_argument("--list", action="store_true",
                        help="列出可用 case")
    parser.add_argument("--no-sdk", action="store_true",
                        help="跳过 SDK 子进程执行")
    parser.add_argument("--sdk-timeout", type=float, default=30.0,
                        help="SDK 子进程启动/关闭超时秒数")
    from .live_cli import add_cli_arguments, run_live_command

    add_cli_arguments(parser)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if (
        args.live or args.contract or args.release or args.final_acceptance
        or args.aggregate_shards
    ):
        return run_live_command(args)
    if args.list:
        cases = [
            case for case in BENCHMARK_CASES
            if not args.no_sdk or case.mode is not PromptBenchmarkMode.SDK
        ]
        if args.json:
            print(json.dumps(
                [case.as_dict() for case in cases],
                ensure_ascii=False, indent=2,
            ))
        else:
            for case in cases:
                print(f"{case.id:12s} {case.mode.value:12s} {case.description}")
        return 0
    cases = [
        case for case in BENCHMARK_CASES
        if not args.no_sdk or case.mode is not PromptBenchmarkMode.SDK
    ]
    requested = tuple(args.case) or tuple(case.id for case in cases)
    try:
        report = PromptBenchmarkRunner(
            cases=cases, sdk_timeout=args.sdk_timeout,
        ).run(requested)
    except ValueError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(_text_report(report))
    return 0 if report.passed else 1


__all__ = [
    "BENCHMARK_CASES",
    "PROMPT_BENCHMARK_SCHEMA_VERSION",
    "PromptBenchmarkCase",
    "PromptBenchmarkCheck",
    "PromptBenchmarkMetric",
    "PromptBenchmarkMode",
    "PromptBenchmarkReport",
    "PromptBenchmarkResult",
    "PromptBenchmarkRunner",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
