"""Command-line orchestration for live behavior, parity and acceptance runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Mapping

from ..agent.prompts import (
    CURRENT_PROMPT_RELEASE,
    prompt_calibration_paths_from_config,
    prompt_profile_from_config,
    prompt_release_id_from_config,
    resolve_prompt_profile,
)
from ..config import load_config_layers
from ..core.errors import ConfigError, LLMHTTPError
from ..core.redact import SecretRedactor
from ..llm.base import LLMProvider
from ..llm.registry import (
    API_KEY_ENV,
    ProviderSettings,
    create_provider,
    resolve_settings,
)
from .behavior_cases import BEHAVIOR_CASES
from .behavior_models import BehaviorReport
from .behavior_runner import BehaviorBenchmarkRunner
from .acceptance_runner import FinalAcceptanceRunner
from .parity import (
    PROMPT_PARITY_CONTRACT,
    PROMPT_PARITY_MINIMUM_TRIALS,
    PromptParityBaseline,
    PromptParityGateResult,
    contract_summary,
    evaluate_prompt_parity,
    load_prompt_baseline,
    save_prompt_baseline,
)
from .report_store import (
    DEFAULT_BEHAVIOR_REPORT_PATH,
    aggregate_behavior_report_files,
    save_behavior_report,
)
from .suite import BENCHMARK_SUITES, BenchmarkSuiteError, resolve_suite, shard_cases


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


def _configured_prompt_calibrations(config, workspace: Path):
    calibrations = []
    for raw_path in prompt_calibration_paths_from_config(config):
        source = Path(raw_path).expanduser()
        if not source.is_absolute():
            source = Path(workspace) / source
        calibrations.append(load_prompt_baseline(source).calibration)
    return tuple(calibrations)


def _benchmark_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provider_execution_profile(settings: ProviderSettings) -> dict[str, object]:
    """Fingerprint execution settings without persisting endpoint or credentials."""
    endpoint = (settings.base_url or f"provider-default:{settings.provider}").rstrip("/")
    generation = settings.extra.get("generation")
    return {
        "provider_base_url_hash": _benchmark_digest(endpoint),
        "provider_generation_hash": _benchmark_digest(
            generation if isinstance(generation, Mapping) else {}),
        "provider_timeout_seconds": float(settings.timeout),
        "provider_max_tokens": int(settings.max_tokens or 0),
    }


def _text_report(
    report: BehaviorReport, *, include_content: bool = False,
) -> str:
    lines = [
        f"Krelcode Live Model Behavior Benchmark v{report.schema_version}",
        f"provider: {report.provider or '?'} · model: {report.model or '?'}",
    ]
    if report.profile:
        lines.append(
            "suite: "
            f"krelcode={report.profile.get('krelcode_version', '?')} · "
            f"python={report.profile.get('python_version', '?')} · "
            f"hash={str(report.profile.get('suite_hash', '?'))[:12]} · "
            f"timeout={report.profile.get('trial_timeout_seconds', '?')}s"
        )
        lines.append(
            "prompt: "
            f"release={report.profile.get('prompt_release_id', '?')}/"
            f"{str(report.profile.get('prompt_release_hash', '?'))[:12]} · "
            f"profile={report.profile.get('prompt_profile', '?')}/"
            f"{str(report.profile.get('prompt_profile_hash', '?'))[:12]} · "
            f"calibration={report.profile.get('profile_calibration_status', 'uncalibrated')}"
        )
    for result in report.results:
        status = "PASS" if result.passed else "FAIL"
        tokens = result.usage.get("total_tokens", 0)
        lines.append(
            f"[{status}] {result.case_id} trial {result.trial} · "
            f"{result.duration_ms}ms · {len(result.tools)} tools · "
            f"{tokens} tokens"
        )
        if result.error:
            kind = f" [{result.error_kind}]" if result.error_kind else ""
            lines.append(f"  error{kind}: {result.error}")
        for check in result.checks:
            marker = "ok" if check.passed else "fail"
            lines.append(f"  {marker:4s} {check.metric.value}: {check.detail}")
        if include_content and result.answer:
            lines.append(f"  answer: {result.answer[:800]}")
    lines.append("case summary:")
    for case_id, summary in report.case_summary().items():
        lines.append(
            f"  {case_id}: {summary['passed']}/{summary['trials']} passed · "
            f"pass@k={summary['pass_at_k']} · stable={summary['stable']} · "
            f"tools={summary['tools']} · input={summary['input_tokens']} · "
            f"output={summary['output_tokens']} · cost={summary['cost_usd']}"
        )
    return "\n".join(lines)


def add_cli_arguments(parser: argparse.ArgumentParser) -> None:
    """Add opt-in live behavior and parity-gate options."""
    parser.add_argument(
        "--live", action="store_true",
        help="运行真实模型行为 Benchmark（需要 provider/API key）")
    parser.add_argument(
        "--trials", type=int, default=0,
        help="每个 live case 的独立 trial 数（1-50；0=使用 suite 默认值）")
    parser.add_argument(
        "--suite", default="standard", choices=tuple(BENCHMARK_SUITES),
        help="live suite：smoke/standard/nightly/extended")
    parser.add_argument(
        "--shard", default=None, metavar="INDEX/COUNT",
        help="确定性分片运行，例如 --shard 2/4；适合 CI 并行扩展规模")
    parser.add_argument(
        "--aggregate-shards", nargs="+", default=None, metavar="REPORT",
        help="合并多个完整分片的 content-light report，不接受重叠或缺失 trial")
    parser.add_argument("--provider", default=None, help="覆盖 live provider")
    parser.add_argument("--model", default=None, help="覆盖 live model")
    parser.add_argument("--base-url", default=None, help="覆盖 live base URL")
    parser.add_argument(
        "--config", default=None,
        help="live provider 使用的显式 TOML 配置")
    parser.add_argument(
        "--contract", action="store_true",
        help="输出冻结的 Prompt Parity Contract")
    parser.add_argument(
        "--release", action="store_true",
        help="输出不可变 Prompt Release manifest（content-light）")
    parser.add_argument(
        "--gate", default=None, metavar="BASELINE",
        help="用 content-light baseline 执行 Prompt Parity benchmark gate")
    parser.add_argument(
        "--save-baseline", default=None, metavar="PATH",
        help="在完整 3-trial suite 通过后保存 prompt parity baseline")
    parser.add_argument(
        "--force-baseline", action="store_true",
        help="允许覆盖已有 baseline（默认禁止，避免意外替换冻结证据）")
    parser.add_argument(
        "--save-report", nargs="?", const=DEFAULT_BEHAVIOR_REPORT_PATH,
        default=None, metavar="PATH",
        help=("原子保存 content-light live report；省略 PATH 时写入 "
              f"{DEFAULT_BEHAVIOR_REPORT_PATH}"))
    parser.add_argument(
        "--include-content", action="store_true",
        help="在调试报告中包含最终答案和工具参数（gate/save/final acceptance 时禁用）")
    parser.add_argument(
        "--final-acceptance", action="store_true",
        help="在 fresh live Parity Gate 后执行 Step 112 最终 fail-closed 验收")
    parser.add_argument(
        "--tune", action="store_true",
        help="根据失败轨迹生成需人工审核的 Prompt 调优候选")
    parser.add_argument(
        "--tune-output", default=None,
        help="将 Prompt 调优候选 JSON 写入指定文件")
    parser.add_argument(
        "--live-max-steps", type=int, default=14,
        help="每个 live 模型轮次允许的最大代理步数")
    parser.add_argument(
        "--trial-timeout", type=float, default=300.0,
        help="单个 live trial 的总超时秒数（默认 300）")
    parser.add_argument(
        "--sandbox-backend", default="auto",
        choices=("auto", "static", "seatbelt", "bubblewrap"),
        help="live Benchmark 的运行时 Sandbox 后端")


def _parity_payload(
    report: BehaviorReport,
    *,
    include_content: bool,
    tuning_report=None,
    gate: PromptParityGateResult | None = None,
    baseline_path: str = "",
    report_path: str = "",
    final_acceptance=None,
) -> object:
    benchmark = report.as_dict(include_content=include_content)
    if (
        tuning_report is None
        and gate is None
        and not baseline_path
        and not report_path
        and final_acceptance is None
    ):
        return benchmark
    payload: dict[str, object] = {"benchmark": benchmark}
    if tuning_report is not None:
        payload["tuning"] = tuning_report.as_dict()
    if gate is not None:
        payload["gate"] = gate.as_dict()
    if baseline_path:
        payload["baseline_path"] = baseline_path
    if report_path:
        payload["report_path"] = report_path
    if final_acceptance is not None:
        payload["final_acceptance"] = final_acceptance.as_dict()
    return payload


def _loaded_attr(module_name: str, name: str, fallback):
    """Read an already-loaded compatibility facade without importing it."""
    module = sys.modules.get(module_name)
    return getattr(module, name, fallback) if module is not None else fallback


def run_live_command(args: argparse.Namespace) -> int:
    """CLI adapter used by ``python -m krelcode.benchmarks.prompt --live``."""
    # Use focused modules by default.  If the historical compatibility facade
    # is already loaded (for tests, plugins or embedders), honor its patched
    # symbols without creating a live_cli -> facade dependency edge.
    behavior = "krelcode.benchmarks.behavior"
    behavior_cases = _loaded_attr(behavior, "BEHAVIOR_CASES", BEHAVIOR_CASES)
    behavior_runner = _loaded_attr(
        behavior, "BehaviorBenchmarkRunner", BehaviorBenchmarkRunner)
    aggregate_reports = _loaded_attr(
        behavior, "aggregate_behavior_report_files",
        aggregate_behavior_report_files)
    provider_factory = _loaded_attr(behavior, "create_provider", create_provider)
    config_loader = _loaded_attr(
        behavior, "load_config_layers", load_config_layers)
    profile_from_config = _loaded_attr(
        behavior, "prompt_profile_from_config", prompt_profile_from_config)
    release_from_config = _loaded_attr(
        behavior, "prompt_release_id_from_config", prompt_release_id_from_config)
    resolve_profile = _loaded_attr(
        behavior, "resolve_prompt_profile", resolve_prompt_profile)
    settings_resolver = _loaded_attr(
        behavior, "resolve_settings", resolve_settings)
    report_saver = _loaded_attr(
        behavior, "save_behavior_report", save_behavior_report)
    try:
        suite = resolve_suite(getattr(args, "suite", "standard"))
    except BenchmarkSuiteError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if getattr(args, "final_acceptance", False) and any((
        getattr(args, "contract", False),
        getattr(args, "release", False),
        getattr(args, "list", False),
    )):
        print("--final-acceptance 不能与 contract/release/list 混用",
              file=sys.stderr)
        return 2

    aggregate_paths = tuple(getattr(args, "aggregate_shards", None) or ())
    if aggregate_paths:
        incompatible = any((
            getattr(args, "contract", False),
            getattr(args, "release", False),
            getattr(args, "list", False),
            getattr(args, "gate", None),
            getattr(args, "save_baseline", None),
            getattr(args, "final_acceptance", False),
            getattr(args, "include_content", False),
            getattr(args, "tune", False),
            getattr(args, "tune_output", None),
        ))
        if incompatible:
            print("--aggregate-shards 不能与 contract/release/list/gate/"
                  "baseline/final-acceptance/content/tune 混用", file=sys.stderr)
            return 2
        try:
            report = aggregate_reports(aggregate_paths)
        except (OSError, TypeError, ValueError) as exc:
            print(f"benchmark shard 聚合失败: {_safe_error(exc)}", file=sys.stderr)
            return 2
        saved_path = ""
        save_report_path = getattr(args, "save_report", None)
        if save_report_path:
            destination = Path(str(save_report_path)).expanduser()
            if not destination.is_absolute():
                destination = Path.cwd() / destination
            try:
                saved_path = str(report_saver(report, destination))
            except (OSError, TypeError, ValueError) as exc:
                print(f"live report 保存失败: {_safe_error(exc)}", file=sys.stderr)
                return 2
        if getattr(args, "json", False):
            print(json.dumps(
                report.as_dict(include_content=False),
                ensure_ascii=False,
                indent=2,
            ))
        else:
            print(_text_report(report))
            if saved_path:
                print()
                print(f"Live behavior report: {saved_path}")
        return 0 if report.passed else 1

    if getattr(args, "contract", False):
        payload = contract_summary(PROMPT_PARITY_CONTRACT)
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("Krelcode Prompt Parity Contract")
            print(
                f"version: {payload['version']} · hash: "
                f"{str(payload['contract_hash'])[:16]} · "
                f"min trials: {payload['minimum_trials']}"
            )
            raw_cases = payload.get("cases")
            raw_dimensions = payload.get("dimensions")
            print(
                "cases: "
                + str(len(raw_cases) if isinstance(raw_cases, (list, tuple)) else 0))
            if isinstance(raw_dimensions, Mapping):
                for dimension, requirement_ids in raw_dimensions.items():
                    if isinstance(requirement_ids, (list, tuple)):
                        print(
                            f"  {dimension}: "
                            + ", ".join(str(item) for item in requirement_ids))
        return 0

    if getattr(args, "release", False):
        payload = CURRENT_PROMPT_RELEASE.manifest()
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("Krelcode Prompt Release")
            print(
                f"id: {payload['release_id']} · "
                f"hash: {str(payload['release_hash'])[:16]} · "
                f"frozen: {payload['frozen_at']}"
            )
            raw_profiles = payload.get("profiles")
            if isinstance(raw_profiles, (list, tuple)):
                print("profiles: " + ", ".join(
                    str(item.get("name", ""))
                    for item in raw_profiles
                    if isinstance(item, Mapping)
                ))
        return 0

    if getattr(args, "list", False):
        requested = tuple(getattr(args, "case", []) or ())
        available = suite.select(case.id for case in behavior_cases)
        unknown = sorted(set(requested) - set(available))
        if unknown:
            print("suite 中不存在 benchmark case: " + ", ".join(unknown),
                  file=sys.stderr)
            return 2
        selected_ids = requested or available
        try:
            selected_ids = shard_cases(
                selected_ids, getattr(args, "shard", None))
        except BenchmarkSuiteError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        selected = set(selected_ids)
        cases = [case for case in behavior_cases if case.id in selected]
        if getattr(args, "json", False):
            print(json.dumps(
                [case.as_dict() for case in cases],
                ensure_ascii=False, indent=2,
            ))
        else:
            for case in cases:
                dimensions = PROMPT_PARITY_CONTRACT.requirement_for_case(case.id)
                suffix = f" · {', '.join(dimensions)}" if dimensions else ""
                print(f"{case.id:26s} {case.description}{suffix}")
        return 0

    gate_path = getattr(args, "gate", None)
    save_path = getattr(args, "save_baseline", None)
    force_baseline = bool(getattr(args, "force_baseline", False))
    include_content = bool(getattr(args, "include_content", False))
    final_acceptance = bool(getattr(args, "final_acceptance", False))
    if gate_path and save_path:
        print("--gate 与 --save-baseline 不能同时使用", file=sys.stderr)
        return 2
    if force_baseline and not save_path:
        print("--force-baseline 只能与 --save-baseline 一起使用", file=sys.stderr)
        return 2
    if save_path and Path(save_path).expanduser().exists() and not force_baseline:
        print(
            f"baseline 已存在，拒绝覆盖: {save_path}；确认替换时使用 --force-baseline",
            file=sys.stderr,
        )
        return 2
    if include_content and (gate_path or save_path or final_acceptance):
        print("gate/baseline/final acceptance 流程禁止 --include-content，"
              "以保持报告 content-light", file=sys.stderr)
        return 2
    if final_acceptance and not getattr(args, "live", False):
        print("--final-acceptance 必须与 --live 一起运行 fresh live gate",
              file=sys.stderr)
        return 2
    if final_acceptance and not gate_path:
        print("--final-acceptance 需要 --gate <accepted-baseline.json>",
              file=sys.stderr)
        return 2
    if final_acceptance and getattr(args, "no_sdk", False):
        print("--final-acceptance 必须运行 SDK 生产面，不能使用 --no-sdk",
              file=sys.stderr)
        return 2
    requested_cases = tuple(getattr(args, "case", []) or ())
    if (gate_path or save_path or final_acceptance) and requested_cases:
        print("gate/baseline/final acceptance 必须运行完整 case manifest，"
              "不能同时指定 --case", file=sys.stderr)
        return 2
    available_ids = suite.select(case.id for case in behavior_cases)
    outside_suite = sorted(set(requested_cases) - set(available_ids))
    if outside_suite:
        print("suite 中不存在 benchmark case: " + ", ".join(outside_suite),
              file=sys.stderr)
        return 2
    try:
        selected_case_ids = shard_cases(
            requested_cases or available_ids,
            getattr(args, "shard", None),
        )
    except BenchmarkSuiteError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if (gate_path or save_path or final_acceptance) and (
            suite.name == "smoke"
            or len(selected_case_ids) != len(behavior_cases)
            or bool(getattr(args, "shard", None))):
        print("gate/baseline/final acceptance 必须使用完整未分片 suite",
              file=sys.stderr)
        return 2

    baseline = None
    if gate_path:
        try:
            baseline = load_prompt_baseline(Path(gate_path))
        except ValueError as exc:
            print(f"baseline 配置错误: {_safe_error(exc)}", file=sys.stderr)
            return 2

    workspace = Path.cwd()
    try:
        config_path = (
            Path(args.config).expanduser()
            if getattr(args, "config", None) else None)
        bundle = (
            config_loader(explicit_path=config_path)
            if config_path is not None
            else config_loader(workspace=workspace)
        )
        if config_path is not None and not bundle.sources:
            raise ValueError(f"配置文件不存在: {config_path}")
        settings = settings_resolver(
            provider=getattr(args, "provider", None),
            model=getattr(args, "model", None),
            base_url=getattr(args, "base_url", None),
            config=bundle.data,
        )
        prompt_resolution = resolve_profile(
            settings.provider,
            settings.model or "provider-default",
            configured=profile_from_config(bundle.data),
            release_id=release_from_config(bundle.data),
            calibrations=_configured_prompt_calibrations(
                bundle.data, workspace),
        )
    except Exception as exc:
        print(f"live benchmark 配置错误: {_safe_error(exc)}", file=sys.stderr)
        return 2

    if settings.provider == "mock":
        print("live benchmark 需要真实 provider；mock 仅用于离线结构测试",
              file=sys.stderr)
        return 2
    if not settings.api_key:
        key_env = API_KEY_ENV.get(settings.provider) or "provider 配置"
        print(
            f"live benchmark 缺少 {settings.provider} API key；"
            f"请在分层 TOML 或 {key_env} 中配置",
            file=sys.stderr,
        )
        return 2

    def factory() -> LLMProvider:
        return provider_factory(settings)

    requested_trials = int(getattr(args, "trials", 0) or 0)
    trials = requested_trials or suite.default_trials
    if gate_path or save_path:
        trials = max(trials, PROMPT_PARITY_MINIMUM_TRIALS)
    runner = behavior_runner(
        factory,
        trials=trials,
        max_steps=getattr(args, "live_max_steps", 14),
        sandbox_backend=getattr(args, "sandbox_backend", "auto"),
        trial_timeout_seconds=getattr(args, "trial_timeout", 300.0),
        provider_name=settings.provider,
        model_name=settings.model or "provider-default",
        prompt_resolution=prompt_resolution,
        provider_profile=_provider_execution_profile(settings),
        suite_name=suite.name,
        shard=str(getattr(args, "shard", "") or ""),
    )
    # Preserve the historical full-run call shape for the complete standard
    # suite (and its nightly/extended trial variants). Runner case filtering is
    # only needed for smoke, explicit --case or deterministic shards.
    runner_case_ids = (
        None
        if (
            suite.name in {"standard", "nightly", "extended"}
            and not requested_cases
            and not getattr(args, "shard", None)
        )
        else selected_case_ids
    )
    try:
        report = runner.run(runner_case_ids)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    saved_report_path = ""
    save_report_path = getattr(args, "save_report", None)
    if save_report_path:
        destination = Path(str(save_report_path)).expanduser()
        if not destination.is_absolute():
            destination = workspace / destination
        try:
            saved_report_path = str(report_saver(report, destination))
        except (OSError, TypeError, ValueError) as exc:
            print(f"live report 保存失败: {_safe_error(exc)}", file=sys.stderr)
            return 2

    tuning_report = None
    if getattr(args, "tune", False) or getattr(args, "tune_output", None):
        from .tuning import analyze_failure_trajectories

        tuning_report = analyze_failure_trajectories(report)

    saved_baseline_path = ""
    baseline_error_rc = 0
    if save_path:
        if not report.passed:
            baseline_error_rc = 1
        else:
            try:
                prompt_baseline = PromptParityBaseline.from_report(
                    report, PROMPT_PARITY_CONTRACT)
                save_prompt_baseline(Path(save_path), prompt_baseline)
                saved_baseline_path = str(Path(save_path).expanduser())
            except (OSError, ValueError) as exc:
                baseline_error_rc = 2
                print(f"baseline 保存失败: {_safe_error(exc)}", file=sys.stderr)

    gate_result = None
    if baseline is not None:
        gate_result = evaluate_prompt_parity(
            report, baseline, PROMPT_PARITY_CONTRACT)

    acceptance_report = None
    if final_acceptance:
        if gate_result is None:
            print("final acceptance 缺少 live Parity Gate", file=sys.stderr)
            return 2
        acceptance_runner = _loaded_attr(
            "krelcode.benchmarks.acceptance",
            "FinalAcceptanceRunner",
            FinalAcceptanceRunner,
        )
        acceptance_report = acceptance_runner(
            workspace=workspace,
        ).run(
            include_sdk=True,
            sdk_timeout=float(getattr(args, "sdk_timeout", 30.0)),
            live_gate=gate_result,
            live_report=report,
        )

    tune_output = getattr(args, "tune_output", None)
    if tuning_report is not None and tune_output:
        try:
            output_path = Path(tune_output).expanduser()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(
                    tuning_report.as_dict(),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"无法写入 tuning report: {_safe_error(exc)}", file=sys.stderr)
            return 2
        if not getattr(args, "json", False):
            print(f"Prompt tuning report: {output_path}")

    include_tuning = bool(
        tuning_report is not None and getattr(args, "tune", False))
    selected_tuning = tuning_report if include_tuning else None
    if getattr(args, "json", False):
        print(json.dumps(
            _parity_payload(
                report,
                include_content=include_content,
                tuning_report=selected_tuning,
                gate=gate_result,
                baseline_path=saved_baseline_path,
                report_path=saved_report_path,
                final_acceptance=acceptance_report,
            ),
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(_text_report(report, include_content=include_content))
        if saved_baseline_path:
            print()
            print(f"Prompt Parity baseline: {saved_baseline_path}")
        if saved_report_path:
            print()
            print(f"Live behavior report: {saved_report_path}")
        if selected_tuning is not None:
            print()
            print(selected_tuning.render_text())
        if gate_result is not None:
            print()
            print(gate_result.render_text())
        if acceptance_report is not None:
            print()
            print(acceptance_report.render_text())
    if baseline_error_rc == 1:
        print("baseline 未保存：完整 live suite 未通过", file=sys.stderr)
    if baseline_error_rc:
        return baseline_error_rc
    if acceptance_report is not None:
        return acceptance_report.exit_code
    if gate_result is not None:
        return 0 if gate_result.passed else 1
    return 0 if report.passed else 1

__all__ = [
    "add_cli_arguments",
    "run_live_command",
]
