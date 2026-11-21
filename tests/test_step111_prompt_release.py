"""Step 111: immutable Prompt releases and model-profile calibration."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from krelcode.agent.context_inspection import ContextInspectionService
from krelcode.agent.fragments import PromptFragmentRegistry, apply_prompt_snapshot, base_fragments
from krelcode.agent.prompt_release import (
    MODEL_PROFILE_CALIBRATION_SCHEMA_VERSION,
    ModelProfileCalibration,
    ProfileCalibrationStatus,
    PromptReleaseRegistry,
    build_prompt_release,
    load_model_profile_calibration,
    save_model_profile_calibration,
)
from krelcode.agent.prompts import (
    CURRENT_PROMPT_RELEASE,
    CURRENT_PROMPT_RELEASE_ID,
    FROZEN_PROMPT_RELEASE_HASH,
    PROMPT_BASE_SECTIONS,
    PROMPT_PROFILES,
    resolve_prompt_profile,
)
from krelcode.agent.loop import AgentLoop
from krelcode.app import build_loop, build_parser
from krelcode.benchmarks import (
    BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
    PROMPT_BASELINE_SCHEMA_VERSION,
    FROZEN_PROMPT_PARITY_SUITE_HASH,
    PROMPT_PARITY_CONTRACT,
    BehaviorCheck,
    BehaviorMetric,
    BehaviorReport,
    BehaviorTrialResult,
    PromptParityBaseline,
    evaluate_prompt_parity,
    save_prompt_baseline,
)
from krelcode.benchmarks import prompt as benchmark_prompt
from krelcode.core.events import EventBus
from krelcode.core.messages import Message, ToolCall
from krelcode.core.errors import ConfigError
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.subagent.manager import SubagentManager
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def _resolution(*, provider="deepseek", model="deepseek-chat", profile="deepseek"):
    return resolve_prompt_profile(
        provider, model, configured=profile)


def _calibration(resolution, **overrides):
    values = {
        "profile_name": resolution.name,
        "profile_hash": resolution.profile_hash,
        "release_id": resolution.release_id,
        "release_hash": resolution.release_hash,
        "contract_version": PROMPT_PARITY_CONTRACT.version,
        "contract_hash": PROMPT_PARITY_CONTRACT.hash,
        "benchmark_schema_version": BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
        "suite_hash": FROZEN_PROMPT_PARITY_SUITE_HASH,
        "provider": resolution.provider,
        "model": resolution.model,
        "verified_at": "2026-11-21T00:00:00+00:00",
        "trials": 3,
    }
    values.update(overrides)
    return ModelProfileCalibration.certify(**values)


def _report(
    *,
    provider="mock",
    model="mock-model",
    profile="deepseek",
) -> BehaviorReport:
    resolution = resolve_prompt_profile(
        provider, model, configured=profile)
    results = tuple(
        BehaviorTrialResult(
            case_id=case_id,
            trial=trial,
            passed=True,
            checks=(BehaviorCheck(
                BehaviorMetric.TASK_SUCCESS, True, "release fixture"),),
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        for case_id in PROMPT_PARITY_CONTRACT.required_case_ids
        for trial in range(1, 4)
    )
    return BehaviorReport(
        schema_version=BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
        generated_at="2026-11-21T00:00:00+00:00",
        provider=provider,
        model=model,
        passed=True,
        results=results,
        duration_ms=1,
        profile={
            "krelcode_version": "test",
            "suite_hash": FROZEN_PROMPT_PARITY_SUITE_HASH,
            "parity_contract_version": PROMPT_PARITY_CONTRACT.version,
            "parity_contract_hash": PROMPT_PARITY_CONTRACT.hash,
            "sandbox_backend": "static",
            "max_steps": 14,
            "context_max_tokens": 30000,
            "trial_timeout_seconds": 300.0,
            "cases": list(PROMPT_PARITY_CONTRACT.required_case_ids),
            "trials": 3,
            "prompt_release_id": resolution.release_id,
            "prompt_release_hash": resolution.release_hash,
            "prompt_profile": resolution.name,
            "prompt_profile_hash": resolution.profile_hash,
            "profile_calibration_status": "uncalibrated",
        },
    )


def test_prompt_release_is_deterministic_pinned_and_content_light():
    rebuilt = build_prompt_release(
        CURRENT_PROMPT_RELEASE_ID,
        PROMPT_BASE_SECTIONS,
        PROMPT_PROFILES,
    )
    assert CURRENT_PROMPT_RELEASE.release_hash == FROZEN_PROMPT_RELEASE_HASH
    assert rebuilt.release_hash == CURRENT_PROMPT_RELEASE.release_hash
    assert CURRENT_PROMPT_RELEASE.profile("deepseek").profile_hash
    manifest = CURRENT_PROMPT_RELEASE.manifest()
    assert manifest["contains_prompt_content"] is False
    assert manifest["release_hash"] == FROZEN_PROMPT_RELEASE_HASH
    encoded = json.dumps(manifest, ensure_ascii=False)
    assert "You are Krelcode" not in encoded
    assert "For DeepSeek models" not in encoded


def test_exact_calibration_marks_runtime_profile_calibrated():
    initial = _resolution()
    assert initial.calibration_status == "uncalibrated"
    calibration = _calibration(initial)

    resolved = resolve_prompt_profile(
        "deepseek",
        "deepseek-chat",
        configured="deepseek",
        calibrations=(calibration,),
    )

    assert resolved.release_id == CURRENT_PROMPT_RELEASE_ID
    assert resolved.release_hash == FROZEN_PROMPT_RELEASE_HASH
    assert resolved.profile_hash == CURRENT_PROMPT_RELEASE.profile("deepseek").profile_hash
    assert resolved.calibration_status == "calibrated"
    assert resolved.calibration_id == calibration.calibration_id
    assert resolved.calibration_hash == calibration.calibration_hash


def test_stale_release_and_contract_are_visible_not_silently_calibrated():
    resolution = _resolution()
    stale_release = replace(
        _calibration(resolution), release_hash="b" * 64)
    stale_contract = replace(
        _calibration(resolution), contract_hash="c" * 64)

    release_resolution = resolve_prompt_profile(
        "deepseek", "deepseek-chat", configured="deepseek",
        calibrations=(stale_release,),
    )
    contract_resolution = resolve_prompt_profile(
        "deepseek", "deepseek-chat", configured="deepseek",
        calibrations=(stale_contract,),
    )

    assert release_resolution.calibration_status == "stale"
    assert "release_hash" in release_resolution.calibration_reason
    assert contract_resolution.calibration_status == "stale"
    assert "contract_hash" in contract_resolution.calibration_reason


def test_longest_model_calibration_pattern_wins():
    resolution = _resolution(model="deepseek-reasoner")
    broad = _calibration(
        resolution,
        provider="",
        model="",
        provider_patterns=("deepseek*",),
        model_patterns=("deepseek-*",),
    )
    specific = _calibration(
        resolution,
        provider="",
        model="",
        provider_patterns=("deepseek*",),
        model_patterns=("deepseek-reasoner*",),
    )
    registry = PromptReleaseRegistry(
        CURRENT_PROMPT_RELEASE, (broad, specific))
    selected = registry.resolve(
        "deepseek",
        "deepseek",
        "deepseek-reasoner",
        contract_version=PROMPT_PARITY_CONTRACT.version,
        contract_hash=PROMPT_PARITY_CONTRACT.hash,
        suite_hash=FROZEN_PROMPT_PARITY_SUITE_HASH,
    )

    assert selected.status is ProfileCalibrationStatus.CALIBRATED
    assert selected.calibration is specific


def test_calibration_file_roundtrip_rejects_tamper_and_prompt_body(tmp_path):
    calibration = _calibration(_resolution())
    path = tmp_path / "calibration.json"
    save_model_profile_calibration(path, calibration)
    loaded = load_model_profile_calibration(path)
    assert loaded.calibration_hash == calibration.calibration_hash
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["contains_prompt_content"] is False
    assert payload["schema_version"] == MODEL_PROFILE_CALIBRATION_SCHEMA_VERSION

    payload["prompt_body"] = "must fail"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="不受支持的字段"):
        load_model_profile_calibration(path)

    save_model_profile_calibration(path, calibration)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["trials"] = 10
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="calibration_hash"):
        load_model_profile_calibration(path)


def test_build_loop_loads_configured_calibration_and_context_why_is_traceable(tmp_path):
    report = _report()
    baseline = PromptParityBaseline.from_report(report)
    baseline_path = tmp_path / "baseline.json"
    save_prompt_baseline(baseline_path, baseline)
    args = build_parser().parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context",
    ])
    loop, manager = asyncio.run(build_loop(
        args,
        {
            "prompt": {
                "profile": "deepseek",
                "calibration_baselines": [str(baseline_path)],
            },
        },
        log=lambda _line: None,
    ))
    try:
        assert loop.prompt_profile_resolution.calibration_status == "calibrated"
        metadata = loop.prompt_registry.get("core:base:safety").metadata
        assert metadata["prompt_release_id"] == CURRENT_PROMPT_RELEASE_ID
        assert metadata["prompt_release_hash"] == FROZEN_PROMPT_RELEASE_HASH
        assert metadata["profile_calibration_status"] == "calibrated"

        loop.llm = MockProvider(fallback="done")
        base = loop.prompt_registry.get("core:base:safety")
        registry = PromptFragmentRegistry(base_fragments(
            loop.prompt_profile_resolution))
        loop.prompt_registry = registry
        apply_prompt_snapshot(loop, registry.snapshot())
        asyncio.run(loop.run("inspect release"))
        output = "\n".join(
            ContextInspectionService(loop).why_lines("core:base:safety"))
        assert f"prompt release: {CURRENT_PROMPT_RELEASE_ID}" in output
        assert "calibration: calibrated" in output
        assert "You are Krelcode" not in output
        assert base is not None
    finally:
        asyncio.run(manager.shutdown())


def test_baseline_schema_two_embeds_calibration_and_gate_fails_on_release_change():
    assert PROMPT_BASELINE_SCHEMA_VERSION == 2
    report = _report()
    baseline = PromptParityBaseline.from_report(report)
    assert baseline.calibration.release_hash == FROZEN_PROMPT_RELEASE_HASH
    assert baseline.calibration.profile_name == "deepseek"
    assert baseline.as_dict()["contains_prompt_content"] is False

    gate = evaluate_prompt_parity(report, baseline)
    assert gate.passed

    changed_profile = dict(report.profile)
    changed_profile["prompt_release_hash"] = "d" * 64
    changed = replace(report, profile=changed_profile)
    failed = evaluate_prompt_parity(changed, baseline)
    checks = {check.id: check.passed for check in failed.checks}
    assert not failed.passed
    assert not checks["prompt-release"]


def test_baseline_loader_rejects_legacy_record_without_calibration(tmp_path):
    report = _report()
    path = tmp_path / "baseline.json"
    save_prompt_baseline(path, PromptParityBaseline.from_report(report))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    raw.pop("calibration")
    path.write_text(json.dumps(raw), encoding="utf-8")
    from krelcode.benchmarks.parity import load_prompt_baseline

    with pytest.raises(ValueError, match="schema 2"):
        load_prompt_baseline(path)


def test_unknown_release_fails_closed():
    with pytest.raises(ConfigError, match="Prompt release"):
        resolve_prompt_profile(
            "deepseek", "deepseek-chat", release_id="not-released")


def test_cli_release_manifest_is_content_light(capsys):
    rc = benchmark_prompt.main(["--release", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["release_hash"] == FROZEN_PROMPT_RELEASE_HASH
    assert payload["contains_prompt_content"] is False
    assert "You are Krelcode" not in json.dumps(payload)


def test_subagent_release_provenance_uses_child_profile(tmp_path):
    class DeepSeekProvider(MockProvider):
        name = "openai"

        def __init__(self):
            super().__init__(fallback="delegated")
            self.model = "deepseek-reasoner"

    async def go():
        manager = SubagentManager(
            llm=DeepSeekProvider(),
            tool_factory=lambda: [],
            tool_context=ToolContext(workspace_root=tmp_path),
            policy=AllowAllPolicy(),
            bus=EventBus(),
        )
        handle = await manager.spawn("delegate")
        handle = await manager.wait(handle.id, timeout=5)
        base = handle.loop.prompt_registry.get("subagent:base:0")
        assert base is not None
        assert base.metadata["prompt_release_id"] == CURRENT_PROMPT_RELEASE_ID
        assert base.metadata["prompt_release_hash"] == FROZEN_PROMPT_RELEASE_HASH
        assert base.metadata["profile_calibration_status"] == "uncalibrated"
        await manager.shutdown()

    asyncio.run(go())
