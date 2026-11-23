"""P2-3 scalable benchmarks and privacy-preserving real-user telemetry."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from krelcode.benchmarks import BEHAVIOR_CASES
from krelcode.benchmarks import behavior as benchmark_behavior
from krelcode.benchmarks.behavior import (
    BehaviorCheck,
    BehaviorMetric,
    BehaviorReport,
    BehaviorTrialResult,
    ToolObservation,
    aggregate_behavior_reports,
)
from krelcode.benchmarks.report_store import (
    aggregate_behavior_report_files,
    load_behavior_report,
    save_behavior_report,
)
from krelcode.benchmarks.suite import (
    BENCHMARK_SUITES,
    parse_shard,
    resolve_suite,
    shard_cases,
)
from krelcode.core.events import (
    ErrorOccurred,
    EventBus,
    LLMResponseReceived,
    RunFinished,
    RunStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from krelcode.core.messages import Message, ToolCall
from krelcode.doctor import STATUS_FAIL, STATUS_OK, check_telemetry
from krelcode.session.commands import CommandRequest, builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers.runtime_ops import handle_telemetry
from krelcode.telemetry import (
    TelemetryConfig,
    TelemetryConfigError,
    TelemetryPreferenceStore,
    TelemetryPrivacyError,
    TelemetryRuntime,
    TelemetryStore,
    TelemetryTransportError,
    build_telemetry_runtime,
    validate_event,
)


def _trial(
    case_id: str,
    trial: int,
    *,
    passed: bool,
    duration_ms: int = 100,
    tool_error: bool = False,
) -> BehaviorTrialResult:
    return BehaviorTrialResult(
        case_id=case_id,
        trial=trial,
        passed=passed,
        checks=(
            BehaviorCheck(
                BehaviorMetric.TASK_SUCCESS,
                passed,
                "synthetic",
            ),
        ),
        tools=(
            ToolObservation(
                call_id=f"{case_id}-{trial}",
                name="read_file",
                arguments={"path": "src/a.py"},
                completed=True,
                is_error=tool_error,
                data={},
                duration_ms=duration_ms,
            ),
        ),
        usage={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
        duration_ms=duration_ms,
    )


def _profile(*, shard: str, cases: tuple[str, ...], suite_size: int, trials: int) -> dict[str, object]:
    return {
        "krelcode_version": "test",
        "python_version": "3.12.0",
        "platform": "test",
        "parity_contract_version": "v1",
        "parity_contract_hash": "a" * 64,
        "suite_name": "extended",
        "suite_size": suite_size,
        "selected_cases": len(cases),
        "cases": list(cases),
        "shard": shard,
        "trials": trials,
        "max_steps": 14,
        "context_max_tokens": 30_000,
        "sandbox_backend": "static",
        "trial_timeout_seconds": 300.0,
        "prompt_release_id": "release",
        "prompt_release_hash": "b" * 64,
        "prompt_profile": "profile",
        "prompt_profile_hash": "c" * 64,
        "profile_calibration_status": "uncalibrated",
        "provider_base_url_hash": "d" * 64,
        "provider_generation_hash": "e" * 64,
        "provider_timeout_seconds": 120.0,
        "provider_max_tokens": 0,
    }


def _report(shard: str, case_ids: tuple[str, ...], *, passed: bool = True) -> BehaviorReport:
    trials = 2
    results = tuple(
        _trial(case_id, trial, passed=passed)
        for case_id in case_ids
        for trial in range(1, trials + 1)
    )
    return BehaviorReport(
        schema_version=benchmark_behavior.BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
        generated_at="2026-11-23T00:00:00+00:00",
        provider="openai",
        model="deepseek-flash",
        passed=passed,
        results=results,
        duration_ms=200,
        profile=_profile(
            shard=shard,
            cases=case_ids,
            suite_size=4,
            trials=trials,
        ),
    )


def _config(tmp_path: Path, *, mode: str = "local") -> TelemetryConfig:
    root = tmp_path / ".krelcode" / "telemetry"
    return TelemetryConfig(
        mode=mode,
        endpoint="https://telemetry.example.test/v1/events" if mode == "remote" else "",
        queue_path=root / "queue.jsonl",
        install_id_path=root / "install_id",
        preferences_path=root / "consent.json",
        flush_interval=60.0,
        request_timeout=1.0,
        batch_size=2,
    )


# --- benchmark suites, statistics and aggregation -------------------------


def test_named_suites_and_shards_are_deterministic():
    available = tuple(case.id for case in BEHAVIOR_CASES)
    smoke = resolve_suite("smoke").select(available)
    nightly = resolve_suite("nightly")
    assert len(smoke) == 5
    assert set(smoke) <= set(available)
    assert nightly.default_trials == 3
    assert resolve_suite("extended").default_trials == 5

    shards = [shard_cases(available, f"{index}/4") for index in range(1, 5)]
    assert set(item for shard in shards for item in shard) == set(available)
    assert len(set(item for shard in shards for item in shard)) == len(available)
    assert [shard[0] for shard in shards] == list(available[:4])
    assert parse_shard("2/3") == (2, 3)
    with pytest.raises(ValueError):
        parse_shard("4/3")


def test_suite_list_cli_returns_smoke_catalogue(capsys):
    from krelcode.benchmarks.prompt import main

    assert main(["--live", "--list", "--suite", "smoke", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in payload} == set(
        BENCHMARK_SUITES["smoke"].case_ids)


def test_behavior_report_exposes_confidence_flaky_latency_and_tool_errors():
    report = BehaviorReport(
        schema_version=benchmark_behavior.BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
        generated_at="2026-11-23T00:00:00+00:00",
        provider="openai",
        model="deepseek-flash",
        passed=False,
        results=(
            _trial("read-scope", 1, passed=True, duration_ms=100),
            _trial("read-scope", 2, passed=False, duration_ms=500, tool_error=True),
            _trial("read-scope", 3, passed=True, duration_ms=300),
        ),
        duration_ms=900,
    )

    case = report.case_summary()["read-scope"]
    overall = report.as_dict()["summary"]
    assert case["pass_rate"] == pytest.approx(2 / 3)
    assert case["flaky"] is True
    assert case["pass_at_k"] is True
    assert case["pass_hat_k"] is False
    assert case["p50_duration_ms"] == 300
    assert case["p95_duration_ms"] == 500
    assert case["tool_errors"] == 1
    assert 0.2 < case["pass_rate_ci_95"][0] < 0.3
    assert 0.9 < case["pass_rate_ci_95"][1] < 0.95
    assert overall["flaky_cases"] == 1
    assert overall["tool_error_rate"] == pytest.approx(1 / 3)
    assert overall["trial_success_rate_ci_95"][0] < overall["trial_success_rate"]


def test_shard_reports_aggregate_roundtrip_and_reject_overlap(tmp_path):
    first = _report("1/2", ("read-scope", "edit-verify"))
    second = _report("2/2", ("permission-denied", "failed-test-recovery"))
    merged = aggregate_behavior_reports([first, second])

    assert merged.passed
    assert len(merged.case_summary()) == 4
    assert merged.profile["shard"] == "aggregate"
    assert merged.profile["shards"] == ["1/2", "2/2"]
    assert merged.as_dict()["summary"]["trials"] == 8

    first_path = save_behavior_report(first, tmp_path / "shard-1.json")
    second_path = save_behavior_report(second, tmp_path / "shard-2.json")
    loaded = load_behavior_report(first_path)
    assert loaded.as_dict() == first.as_dict()
    from_files = aggregate_behavior_report_files([first_path, second_path])
    assert from_files.as_dict()["summary"] == merged.as_dict()["summary"]

    overlap = _report("2/2", ("edit-verify", "failed-test-recovery"))
    with pytest.raises(ValueError, match="重叠"):
        aggregate_behavior_reports([first, overlap])
    incomplete = _report("2/2", ("permission-denied",))
    with pytest.raises(ValueError, match="覆盖不完整"):
        aggregate_behavior_reports([first, incomplete])


def test_aggregate_shards_cli_writes_content_light_report(tmp_path, capsys):
    from krelcode.benchmarks.prompt import main

    first = save_behavior_report(
        _report("1/2", ("read-scope", "edit-verify")),
        tmp_path / "shard-1.json",
    )
    second = save_behavior_report(
        _report("2/2", ("permission-denied", "failed-test-recovery")),
        tmp_path / "shard-2.json",
    )
    output = tmp_path / "merged.json"
    rc = main([
        "--aggregate-shards", str(first), str(second),
        "--save-report", str(output), "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["cases"] == 4
    assert payload["summary"]["trials"] == 8
    assert payload["profile"]["shard"] == "aggregate"
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert "answer" not in json.dumps(saved)
    assert "arguments" not in json.dumps(saved)


# --- consent, privacy and persistence -------------------------------------


def test_telemetry_defaults_off_and_project_config_cannot_enable(tmp_path):
    config = TelemetryConfig.from_mapping(
        {
            "mode": "remote",
            "endpoint": "https://telemetry.example.test/v1/events",
            "sample_rate": 0.25,
        },
        home=tmp_path,
        environ={},
    )
    assert config.mode == "off"
    assert config.sample_rate == 0.25
    assert not config.enabled

    env_enabled = TelemetryConfig.from_mapping(
        {}, home=tmp_path, environ={"KRELCODE_TELEMETRY": "local"})
    assert env_enabled.mode == "local"


def test_consent_and_queue_are_private_and_reject_symlinks(tmp_path):
    root = tmp_path / ".krelcode" / "telemetry"
    store = TelemetryPreferenceStore(root / "consent.json")
    preferences = store.save(mode="local", sample_rate=1.0)
    assert preferences.mode == "local"
    assert (root.stat().st_mode & 0o777) == 0o700
    assert (store.path.stat().st_mode & 0o777) == 0o600

    queue = TelemetryStore(
        root / "queue.jsonl",
        install_id_path=root / "install_id",
        max_events=2,
        max_bytes=100_000,
    )
    runtime = TelemetryRuntime(_config(tmp_path), store=queue)
    queue.append(runtime._envelope("run_started"))
    queue.append(runtime._envelope("run_finished", {"outcome": "success"}))
    queue.append(runtime._envelope("run_cancelled"))
    assert len(queue.events()) == 2
    assert (queue.path.stat().st_mode & 0o777) == 0o600

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    symlink = root / "linked-consent.json"
    symlink.symlink_to(target)
    with pytest.raises(TelemetryConfigError, match="symlink"):
        TelemetryPreferenceStore(symlink).load()
    with pytest.raises(TelemetryConfigError, match="symlink"):
        TelemetryPreferenceStore(symlink).save(mode="off")

    queue_target = tmp_path / "outside.jsonl"
    queue_target.write_text("", encoding="utf-8")
    queue_link = root / "linked-queue.jsonl"
    queue_link.symlink_to(queue_target)
    linked_store = TelemetryStore(
        queue_link, install_id_path=root / "install_id")
    with pytest.raises(Exception, match="symlink"):
        linked_store.append(runtime._envelope("run_started"))


def test_telemetry_schema_rejects_free_form_or_extra_fields(tmp_path):
    runtime = TelemetryRuntime(_config(tmp_path))
    event = runtime._envelope("run_started")
    event["data"] = {"prompt": "do not collect this"}
    with pytest.raises(TelemetryPrivacyError, match="未允许字段"):
        validate_event(event)

    event = runtime._envelope("run_started")
    event["raw_user_input"] = "secret"
    with pytest.raises(TelemetryPrivacyError, match="未允许字段"):
        validate_event(event)


def test_event_bus_never_copies_prompt_code_arguments_or_answer_to_queue(tmp_path):
    secret = "sk-live-super-secret-value"

    async def scenario():
        runtime = TelemetryRuntime(_config(tmp_path))
        bus = EventBus()
        runtime.attach(bus)
        call = ToolCall(
            "call-1", "run_command",
            {"command": f"curl -H 'Authorization: Bearer {secret}'"},
        )
        await bus.emit(RunStarted(f"read my private prompt {secret}"))
        await bus.emit(ToolCallStarted(call))
        await bus.emit(ToolCallCompleted(
            call,
            f"command output {secret}",
            False,
            data={"stdout": secret},
        ))
        await bus.emit(LLMResponseReceived(
            Message.assistant(f"private answer {secret}"),
            usage={"input_tokens": 250, "output_tokens": 12, "total_tokens": 262},
        ))
        await bus.emit(ErrorOccurred(f"RuntimeError: provider rejected {secret}"))
        await bus.emit(RunFinished(f"private final answer {secret}"))
        await runtime.close()

    asyncio.run(scenario())
    raw = (tmp_path / ".krelcode" / "telemetry" / "queue.jsonl").read_text(
        encoding="utf-8")
    assert secret not in raw
    assert "private prompt" not in raw
    assert "private answer" not in raw
    assert "curl" not in raw
    events = [json.loads(line) for line in raw.splitlines()]
    assert {item["event"] for item in events} == {
        "telemetry_started",
        "run_started",
        "tool_call",
        "llm_response",
        "error",
        "run_finished",
    }


class _FakeTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.batches: list[list[dict[str, object]]] = []

    async def send(self, events):
        if self.fail:
            raise TelemetryTransportError("offline")
        self.batches.append([dict(item) for item in events])


def test_remote_delivery_acknowledges_only_after_success(tmp_path):
    async def scenario():
        config = _config(tmp_path, mode="remote")
        transport = _FakeTransport(fail=True)
        runtime = TelemetryRuntime(config, transport=transport)
        event = runtime._envelope("run_started")
        runtime.store.append(event)
        assert await runtime.flush() == 0
        assert len(runtime.store.events()) == 1
        assert runtime.last_error == "TelemetryTransportError"

        transport.fail = False
        assert await runtime.flush() == 1
        assert not runtime.store.events()
        assert transport.batches
        assert transport.batches[0][0]["event_id"] == event["event_id"]

    asyncio.run(scenario())


def test_managed_policy_disables_enable_and_doctor_reports_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = {
        "enterprise": {
            "enabled": True,
            "organization": "example",
            "policy_version": "v1",
            "allow_telemetry": False,
        }
    }
    runtime = build_telemetry_runtime(
        {}, run_mode="test", policy_allowed=False)
    with pytest.raises(TelemetryConfigError, match="企业策略"):
        runtime.enable("local")
    result = check_telemetry(config)
    assert result.status == STATUS_OK
    assert "managed" in result.detail
    assert "content_collection=never" in result.detail


def test_doctor_and_cli_do_not_leak_environment_secrets(tmp_path, monkeypatch, capsys):
    from krelcode.entrypoints.main import main

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-must-not-appear")
    monkeypatch.delenv("KRELCODE_TELEMETRY", raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert main(["telemetry", "status", "--workspace", str(workspace)]) == 0
    status = capsys.readouterr().out
    assert "mode: off" in status
    assert "content_collection: never" in status
    assert "sk-must-not-appear" not in status

    assert main(["telemetry", "enable", "local", "--workspace", str(workspace)]) == 0
    capsys.readouterr()
    assert main(["telemetry", "status", "--workspace", str(workspace)]) == 0
    enabled = capsys.readouterr().out
    assert "mode: local" in enabled
    assert "enabled: yes" in enabled

    assert main(["telemetry", "disable", "--workspace", str(workspace)]) == 0
    assert main(["telemetry", "status", "--workspace", str(workspace)]) == 0
    assert "mode: off" in capsys.readouterr().out
    doctor = check_telemetry({})
    assert doctor.status == STATUS_OK
    assert "sk-must-not-appear" not in doctor.detail


def test_repl_telemetry_command_has_status_enable_disable(tmp_path):
    runtime = TelemetryRuntime(_config(tmp_path, mode="off"))

    def context(arguments: str) -> CommandContext:
        return CommandContext(
            request=CommandRequest(
                "telemetry", arguments, f"/telemetry {arguments}".strip()),
            loop=SimpleNamespace(telemetry=runtime),
            command_registry=builtin_command_registry(),
            workspace_root=tmp_path,
        )

    status = asyncio.run(handle_telemetry(context("status")))
    output = "\n".join(status.output)
    assert "mode=off" in output
    assert "content_collection=never" in output
    enabled = asyncio.run(handle_telemetry(context("enable local")))
    assert "mode=local" in "\n".join(enabled.output)
    assert runtime.config.mode == "local"
    disabled = asyncio.run(handle_telemetry(context("disable")))
    assert "disabled" in "\n".join(disabled.output)
    assert runtime.config.mode == "off"


def test_invalid_telemetry_config_fails_secure_and_is_visible_to_doctor(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = check_telemetry({"telemetry": {"batch_size": "not-an-int"}})
    assert result.status == STATUS_FAIL
    assert "telemetry" in result.detail

    monkeypatch.setenv("KRELCODE_TELEMETRY", "remote")
    missing_endpoint = check_telemetry({})
    assert missing_endpoint.status == STATUS_FAIL
    assert "endpoint" in missing_endpoint.detail
