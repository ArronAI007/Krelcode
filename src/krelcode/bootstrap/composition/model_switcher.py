"""Provider hot-switch and model-calibration composition."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from krelcode.agent.context.compression import (
    DEFAULT_MAX_TOKENS as DEFAULT_CONTEXT_BUDGET,
)
from krelcode.agent.pricing import default_budget_for
from krelcode.agent.prompt.fragments import (
    PromptFragmentRegistry,
    refresh_registry_snapshot,
    replace_base_fragments,
)
from krelcode.agent.prompt.prompt_parity import load_prompt_baseline
from krelcode.agent.prompts import (
    prompt_calibration_paths_from_config,
    prompt_profile_from_config,
    prompt_release_id_from_config,
    resolve_prompt_profile,
)
from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.usage import UsageTracker
from krelcode.contracts.application import RiskLevel
from krelcode.contracts.change_admission import (
    AdmissionGatePort,
    ChangeApproval,
    ChangeCandidate,
    ChangeEvidence,
    ChangeType,
    EvaluationCheck,
    EvaluationReport,
    RollbackManifest,
)
from krelcode.contracts.tool import Tool
from krelcode.core.errors import ConfigError
from krelcode.core.messages import Message
from krelcode.core.redact import SecretRedactor
from krelcode.enterprise.policy import EnterprisePolicy
from krelcode.llm.base import LLMProvider
from krelcode.llm.registry import create_provider, resolve_settings
from krelcode.llm.retry import RetryingProvider, RetryPolicy
from krelcode.safety.admission import DefaultAdmissionGate

from ...agent.prompt.prompt_release import ModelProfileCalibration
from ...core.hooks import HookPort


class SubagentManagerPort(Protocol):
    """Mutable provider/prompt surface shared by subagent manager variants."""

    llm: LLMProvider
    prompt_profile: str
    prompt_release_id: str
    prompt_calibrations: Sequence[ModelProfileCalibration]
    get_parent_history: Optional[Callable[[], List[Message]]]
    cache_ledger_provider: Optional[Callable[[], object | None]]
    hook_port: Optional[HookPort]
    sidechains: object

    def build_tools(self) -> List[Tool]: ...

    async def shutdown(self) -> None: ...


def build_model_switcher(
    args: argparse.Namespace,
    config: Dict[str, Any],
    loop: AgentLoop,
    usage: UsageTracker,
    subagents: Optional[SubagentManagerPort] = None,
    redactor: Optional[SecretRedactor] = None,
    enterprise_policy: Optional[EnterprisePolicy] = None,
    admission_gate: Optional[AdmissionGatePort] = None,
) -> Callable[[Optional[str]], str]:
    """/model 热切换：返回 callable(new_spec|None) -> 结果描述文本。"""

    def current_base() -> str:
        return str(getattr(loop.llm, "name", "?"))

    def switch(new_spec: Optional[str]) -> str:
        if not new_spec:
            model = getattr(loop.llm, "model", "?")
            profile = loop.prompt_profile_name or "unknown"
            return (
                f"当前: {current_base()} / {model} · prompt {profile}"
                "（/model <名称> 或 <provider>/<名称> 切换）"
            )
        if "/" in new_spec:
            provider_name, model_name = new_spec.split("/", 1)
        else:
            provider_name, model_name = current_base(), new_spec
        if provider_name == "mock":
            return "mock 模式请显式指定目标 provider，如: /model openai/gpt-4o-mini"
        try:
            if enterprise_policy is not None:
                enterprise_policy.validate_provider(provider_name)
            settings = resolve_settings(
                provider=provider_name,
                model=model_name,
                base_url=args.base_url,
                config=config,
            )
            inner = create_provider(settings)
            if getattr(inner, "api_key", None) is None:
                return (
                    f"切换失败: {provider_name} 缺少 API key（设置对应环境变量后重试）"
                )
            provider: LLMProvider = inner
            if args.retries > 0:
                provider = RetryingProvider(
                    inner,
                    RetryPolicy(max_retries=args.retries),
                    on_retry=lambda a, d, e: print(
                        f"[retry] LLM 调用失败（{type(e).__name__}），"
                        f"第 {a} 次重试，{d:g}s 后…"
                    ),
                )
        except Exception as exc:
            return f"切换失败: {type(exc).__name__}: {exc}"

        try:
            calibration_records = load_prompt_calibrations(
                config, loop.tool_context.workspace_root
            )
            profile_resolution = resolve_prompt_profile(
                inner,
                getattr(inner, "model", ""),
                configured=prompt_profile_from_config(config),
                release_id=prompt_release_id_from_config(config),
                calibrations=calibration_records,
            )
        except ConfigError as exc:
            return f"切换失败: {exc}"
        before = {
            "provider": current_base(),
            "model": str(getattr(loop.llm, "model", "") or ""),
            "prompt_profile": loop.prompt_profile_name,
            "prompt_release_id": getattr(
                loop.prompt_profile_resolution, "release_id", ""),
        }
        after = {
            "provider": str(provider.name or ""),
            "model": str(getattr(inner, "model", "") or ""),
            "prompt_profile": profile_resolution.name,
            "prompt_release_id": profile_resolution.release_id,
        }
        risk = (
            RiskLevel.MEDIUM
            if profile_resolution.calibrated else RiskLevel.HIGH
        )
        candidate = ChangeCandidate(
            change_type=ChangeType.MODEL_PROFILE,
            target=f"{after['provider']}/{after['model']}",
            before=before,
            after=after,
            risk=risk,
            affected_artifacts=(
                {"kind": "model_profile", "role": "provider"},
                {"kind": "prompt_release", "id": profile_resolution.release_id},
            ),
            evidence=(
                ChangeEvidence(
                    kind="benchmark_calibration",
                    source="prompt-release-registry",
                    digest=profile_resolution.calibration_hash,
                    summary=profile_resolution.calibration_reason,
                    passed=True,
                    metadata={"status": profile_resolution.calibration_status},
                ),
            ),
            metadata={"entrypoint": "/model", "explicit_user_switch": True},
        )
        evaluation = EvaluationReport.from_checks(
            candidate.candidate_id,
            [
                EvaluationCheck(
                    name="provider_policy",
                    passed=True,
                    reason="enterprise provider policy accepted",
                    score=1.0,
                ),
                EvaluationCheck(
                    name="prompt_profile_resolution",
                    passed=True,
                    reason=profile_resolution.matched_by,
                    score=1.0,
                ),
                EvaluationCheck(
                    name="behavior_calibration",
                    passed=profile_resolution.calibrated,
                    reason=profile_resolution.calibration_reason,
                    required=False,
                    score=1.0 if profile_resolution.calibrated else 0.0,
                ),
            ],
        )
        admission = (admission_gate or DefaultAdmissionGate()).admit(
            candidate,
            evaluation=evaluation,
            approval=ChangeApproval(
                approved=True,
                actor="session-user",
                reason="用户显式执行 /model 热切换",
                scope="model-profile",
                metadata={"target": candidate.target},
            ),
            rollback=RollbackManifest(
                candidate_id=candidate.candidate_id,
                snapshot=before,
                instructions="通过 /model 恢复到 before 中的 provider/model",
            ),
            policy_version=str(getattr(
                enterprise_policy, "policy_version", "") or "local"),
            audit={
                "session_id": loop.tool_context.runtime.session_id or "",
                "entrypoint": "/model",
            },
        )
        if not admission.admitted:
            return (
                "切换失败: 模型变更未通过运行时准入"
                f"（{admission.status.value}: {admission.reason}）"
            )
        loop.set_llm(provider)
        if subagents is not None:
            subagents.llm = provider
            subagents.prompt_profile = prompt_profile_from_config(config)
            subagents.prompt_release_id = prompt_release_id_from_config(config)
            subagents.prompt_calibrations = calibration_records
        loop.prompt_profile = profile_resolution.profile
        loop.prompt_profile_resolution = profile_resolution
        loop.prompt_profile_name = profile_resolution.name
        prompt_note = ""
        registry = loop.prompt_registry
        if isinstance(registry, PromptFragmentRegistry):
            replace_base_fragments(
                registry,
                profile_resolution,
                provider=inner,
                model=getattr(inner, "model", ""),
            )
            refresh_registry_snapshot(loop)
            prompt_note = f"，prompt {profile_resolution.name}"
        budget_note = ""
        if loop.context_manager is not None:
            loop.context_manager.llm = provider
            if args.context_tokens is None:
                new_budget = (
                    default_budget_for(getattr(inner, "model", None))
                    or DEFAULT_CONTEXT_BUDGET
                )
                loop.context_manager.max_tokens = new_budget
                budget_note = f"，压缩预算 {new_budget}"
        usage.model = getattr(inner, "model", None)
        telemetry_runtime = loop.telemetry
        if telemetry_runtime is not None:
            telemetry_runtime.set_identity(
                str(provider.name or ""),
                str(getattr(inner, "model", "") or ""),
            )
        if redactor is not None:
            redactor.add(getattr(inner, "api_key", None))
        return (
            f"已切换: {provider.name} / {getattr(inner, 'model', '?')}"
            f"{budget_note}{prompt_note} · admission {admission.release_id}"
        )

    return switch


def load_prompt_calibrations(
    config: Dict[str, Any],
    workspace: Path,
) -> Tuple[ModelProfileCalibration, ...]:
    calibrations: List[ModelProfileCalibration] = []
    for raw_path in prompt_calibration_paths_from_config(config):
        source = Path(raw_path).expanduser()
        if not source.is_absolute():
            source = Path(workspace).expanduser().resolve() / source
        try:
            baseline = load_prompt_baseline(source)
        except ValueError as exc:
            raise ConfigError(f"Prompt calibration 加载失败: {source}: {exc}") from exc
        calibrations.append(baseline.calibration)
    return tuple(calibrations)


__all__ = [
    "SubagentManagerPort",
    "build_model_switcher",
    "load_prompt_calibrations",
]
