"""Step G: /prompts observability and real behaviour benchmark evidence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompt_surface import (
    PromptSurfaceDescriptor,
    PromptSurfaceKind,
    PromptSurfaceRegistry,
    prompt_content_hash,
)
from krelcode.agent.prompt_surface_catalog import refresh_prompt_surfaces
from krelcode.benchmarks import (
    BEHAVIOR_CASES,
    BehaviorBenchmarkRunner,
    BehaviorReport,
    BehaviorTrialResult,
    ToolObservation,
)
from krelcode.benchmarks import prompt as benchmark_prompt
from krelcode.benchmarks.report_store import (
    load_behavior_report_summary,
    save_behavior_report,
)
from krelcode.entrypoints.main import main as full_cli_main
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.controller import SessionController
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def _loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        llm=MockProvider(scripted=["done"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM PROMPT BODY MUST NOT LEAK",
        today_provider=lambda: "2026-11-21",
    )


def _surface(surface_id: str, body: str) -> PromptSurfaceDescriptor:
    return PromptSurfaceDescriptor(
        id=surface_id,
        kind=PromptSurfaceKind.TOOL,
        source="test",
        owner="test",
        content_hash=prompt_content_hash(body),
        token_estimate=4,
        description="test surface",
    )


def test_prompt_surface_health_and_turn_observation_are_content_light():
    registry = PromptSurfaceRegistry({
        "test": (_surface("tool:demo", "secret-body-v1"),),
    })
    registry.observe(
        "tool:demo",
        content_hash=prompt_content_hash("secret-body-v1"),
        turn_id="turn-1",
        rendered_at=10.0,
    )
    health = registry.health()
    assert health["surfaces"] == 1
    assert health["observed"] == 1
    assert health["stale"] == 0
    assert health["render_count"] == 1
    assert [item.surface_id for item in registry.observations_for_turn("turn-1")] == [
        "tool:demo",
    ]
    assert "secret-body-v1" not in repr(health)

    registry.replace_collections({
        "test": (_surface("tool:demo", "secret-body-v2"),),
    })
    assert registry.health()["stale"] == 1


def test_real_agent_turn_observes_main_and_selected_reminder(tmp_path):
    loop = _loop(tmp_path)
    registry = refresh_prompt_surfaces(loop)
    assert registry.get("reminder:workflow:verification-gate") is not None

    asyncio.run(loop.run("修复这个 bug", turn_id="step-g-turn"))
    controller = SessionController(loop, workspace_root=tmp_path)

    health = "\n".join(asyncio.run(
        controller.execute_command("/prompts health")).output)
    observations = "\n".join(asyncio.run(
        controller.execute_command("/prompts observations reminder")).output)
    active = "\n".join(asyncio.run(
        controller.execute_command("/prompts active")).output)

    assert "render" in health
    assert "reminder:workflow:verification-gate" in observations
    assert "turn=step-g-turn" in active
    assert "main:agent" in active
    assert "reminder:workflow:verification-gate" in active
    assert "SYSTEM PROMPT BODY MUST NOT LEAK" not in health + observations + active
    reminder = registry.get("reminder:workflow:verification-gate")
    assert reminder.kind is PromptSurfaceKind.REMINDER
    assert "content" not in reminder.metadata


def test_prompts_benchmark_list_and_status_commands(tmp_path):
    report = BehaviorReport(
        schema_version=4,
        generated_at="2026-11-21T00:00:00+00:00",
        provider="deepseek",
        model="deepseek-chat",
        passed=True,
        results=(BehaviorTrialResult(
            case_id="read-scope",
            trial=1,
            passed=True,
            checks=(),
            answer="ANSWER MUST NOT BE SAVED",
            tools=(ToolObservation(
                call_id="secret-call",
                name="read_file",
                arguments={"path": "src/a.py", "token": "SECRET_ARGUMENT"},
                completed=True,
                is_error=False,
                data={},
                duration_ms=1,
            ),),
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            duration_ms=2,
        ),),
        duration_ms=3,
        profile={"prompt_release_id": "test-release"},
    )
    report_path = tmp_path / ".krelcode" / "prompts" / "benchmarks" / "latest.json"
    save_behavior_report(report, report_path)
    serialized = report_path.read_text(encoding="utf-8")
    assert "ANSWER MUST NOT BE SAVED" not in serialized
    assert "SECRET_ARGUMENT" not in serialized
    assert json.loads(serialized)["content_light"] is True

    summary = load_behavior_report_summary(report_path)
    assert summary.provider == "deepseek"
    assert summary.total_tokens == 15

    loop = _loop(tmp_path)
    refresh_prompt_surfaces(loop)
    controller = SessionController(loop, workspace_root=tmp_path)
    listed = "\n".join(asyncio.run(
        controller.execute_command("/prompts benchmark list")).output)
    status = "\n".join(asyncio.run(
        controller.execute_command("/prompts benchmark status")).output)

    assert "read-scope" in listed
    assert "krelcode benchmark --live" in listed
    assert "[prompts benchmark] PASS" in status
    assert "deepseek/deepseek-chat" in status
    assert "content-light=true" in status
    assert "SECRET_ARGUMENT" not in status


def test_live_runner_report_includes_content_light_surface_evidence(tmp_path):
    case = next(item for item in BEHAVIOR_CASES if item.id == "read-scope")
    report = BehaviorBenchmarkRunner(
        lambda: MockProvider(scripted=["done"]),
        cases=(case,),
        sandbox_backend="static",
        provider_name="scripted",
        model_name="scripted-model",
    ).run()
    result = report.results[0]
    assert result.facts["prompt_surfaces"] > 0
    assert result.facts["observed_prompt_surfaces"] > 0
    assert len(result.facts["prompt_surface_hash"]) == 16
    encoded = json.dumps(report.as_dict(), ensure_ascii=False)
    assert "SYSTEM PROMPT" not in encoded


def test_benchmark_cli_fast_path_forwards_prompt_suite(capsys):
    assert full_cli_main([
        "benchmark", "prompt", "--live", "--list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in payload} == {case.id for case in BEHAVIOR_CASES}


def test_benchmark_prompt_parser_accepts_report_persistence_flag(capsys):
    assert benchmark_prompt.main([
        "--live", "--save-report", "--list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)
