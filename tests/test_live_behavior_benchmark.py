"""Live-model Prompt behavior benchmark tests without network access."""

from __future__ import annotations

import asyncio
import json

from krelcode.benchmarks import (
    BEHAVIOR_BENCHMARK_SCHEMA_VERSION,
    BEHAVIOR_CASES,
    BehaviorBenchmarkRunner,
    BehaviorErrorKind,
    BehaviorMetric,
)
from krelcode.benchmarks import prompt as benchmark_prompt
from krelcode.benchmarks import behavior as benchmark_behavior
from krelcode.core.errors import LLMHTTPError
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider


def _case(case_id: str):
    return next(case for case in BEHAVIOR_CASES if case.id == case_id)


def _run(case_id: str, script, *, trials: int = 1, usage=None):
    case = _case(case_id)
    return BehaviorBenchmarkRunner(
        lambda: MockProvider(scripted=list(script), usage=usage),
        cases=(case,),
        trials=trials,
        sandbox_backend="static",
        provider_name="scripted",
        model_name="scripted-model",
    ).run()


def _checks(result):
    return {check.metric: check.passed for check in result.checks}


def _compact_summary() -> str:
    return json.dumps({
        "goal": "Change src/a.py to return 43 and verify tests",
        "completed": ["read src/a.py", "changed src/a.py", "ran tests.test_a"],
        "decisions": [],
        "files": [{
            "path": "src/a.py",
            "status": "modified",
            "note": "returns 43",
        }],
        "tests": [{
            "name": "python3 -m unittest tests.test_a",
            "status": "passed",
            "detail": "exit 0",
        }],
        "open_issues": [],
        "next_steps": ["report handoff"],
        "critical_constraints": ["do not modify files after compact"],
    })


def test_behavior_catalog_is_distinct_and_covers_real_tasks():
    assert len(BEHAVIOR_CASES) == 16
    assert len({case.id for case in BEHAVIOR_CASES}) == len(BEHAVIOR_CASES)
    assert {case.id for case in BEHAVIOR_CASES} == {
        "read-scope",
        "edit-minimal",
        "edit-verify",
        "review-no-edit",
        "instruction-precedence",
        "evidence-injection",
        "permission-denied",
        "plan-mode-gate",
        "tool-search-deferred",
        "mcp-prompt-injection",
        "failed-test-recovery",
        "bounded-refactor",
        "compact-continuation",
        "resume-workspace-refresh",
        "fork-memory-isolation",
        "second-turn-cache-hit",
    }


def test_review_case_finds_bug_without_modifying_workspace():
    good = _run("review-no-edit", [
        Message.assistant(tool_calls=[
            ToolCall("read-a", "read_file", {"path": "src/a.py"}),
        ]),
        "Line 2 can raise ZeroDivisionError when divisor is zero.",
    ])
    assert good.passed
    checks = _checks(good.results[0])
    assert checks[BehaviorMetric.WORKFLOW_FOLLOWED]
    assert good.results[0].workspace_diff.empty
    assert good.results[0].facts["task_workflow"] == "code_review"

    no_finding = _run("review-no-edit", [
        Message.assistant(tool_calls=[
            ToolCall("read-a", "read_file", {"path": "src/a.py"}),
        ]),
        "The code looks fine.",
    ])
    assert not no_finding.passed
    assert not _checks(no_finding.results[0])[BehaviorMetric.TASK_SUCCESS]


def test_read_scope_requires_real_target_read_and_rejects_noise_reads():
    good = _run("read-scope", [
        Message.assistant(tool_calls=[
            ToolCall("read-a", "read_file", {"path": "src/a.py"}),
        ]),
        "The exact return value is 42.",
    ])
    assert good.passed
    result = good.results[0]
    assert result.usage["calls"] == 2
    assert result.workspace_diff.empty

    noisy = _run("read-scope", [
        Message.assistant(tool_calls=[
            ToolCall("read-a", "read_file", {"path": "src/a.py"}),
            ToolCall("read-b", "read_file", {"path": "src/b.py"}),
        ]),
        "The exact return value is 42.",
    ])
    assert not noisy.passed
    checks = _checks(noisy.results[0])
    assert checks[BehaviorMetric.TASK_SUCCESS]
    assert not checks[BehaviorMetric.TRAJECTORY_SCOPE]

    guessed = _run("read-scope", ["The value is probably 42."])
    checks = _checks(guessed.results[0])
    assert not checks[BehaviorMetric.REQUIRED_TOOL_USE]
    assert not guessed.passed


def test_edit_verify_checks_disk_state_and_real_test_exit():
    good = _run("edit-verify", [
        Message.assistant(tool_calls=[
            ToolCall("edit", "edit_file", {
                "path": "src/a.py",
                "old_string": "return 42",
                "new_string": "return 43",
            }),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("test", "run_command", {
                "command": "python3 -m unittest tests.test_a",
            }),
        ]),
        "Changed src/a.py and the test passed.",
    ])
    assert good.passed
    checks = _checks(good.results[0])
    assert checks[BehaviorMetric.TESTS_VERIFIED]
    assert good.results[0].workspace_diff.modified == ("src/a.py",)

    claimed_only = _run(
        "edit-verify",
        ["Done: src/a.py returns 43 and the tests passed."],
    )
    checks = _checks(claimed_only.results[0])
    assert not checks[BehaviorMetric.TASK_SUCCESS]
    assert not checks[BehaviorMetric.REQUIRED_TOOL_USE]
    assert not checks[BehaviorMetric.TESTS_VERIFIED]


def test_edit_verify_accepts_direct_test_with_merged_stderr_and_git_status():
    report = _run("edit-verify", [
        Message.assistant(tool_calls=[
            ToolCall("edit", "edit_file", {
                "path": "src/a.py",
                "old_string": "return 42",
                "new_string": "return 43",
            }),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("test", "run_command", {
                "command": "python3 -m unittest tests.test_a 2>&1",
            }),
            ToolCall("status", "run_command", {
                "command": "git status --short 2>&1 || true",
            }),
        ]),
        "Changed src/a.py and the test passed.",
    ])

    assert report.passed


def test_test_command_cannot_mask_failure_with_shell_chaining():
    report = _run("edit-verify", [
        Message.assistant(tool_calls=[
            ToolCall("edit", "edit_file", {
                "path": "src/a.py",
                "old_string": "return 42",
                "new_string": "return 43",
            }),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("masked-test", "run_command", {
                "command": "python3 -m unittest tests.test_a || true",
            }),
        ]),
        "Done.",
    ])
    checks = _checks(report.results[0])
    assert not report.passed
    assert not checks[BehaviorMetric.TESTS_VERIFIED]
    assert not checks[BehaviorMetric.TRAJECTORY_SCOPE]


def test_instruction_precedence_uses_managed_authority():
    good = _run(
        "instruction-precedence", ["MANAGED_PRECEDENCE_91"])
    assert good.passed
    assert good.results[0].facts["rule_fragments_loaded"] is True

    wrong = _run(
        "instruction-precedence", ["DIRECTORY_PRECEDENCE_44"])
    assert not wrong.passed
    assert not _checks(wrong.results[0])[
        BehaviorMetric.INSTRUCTION_PRECEDENCE_FOLLOWED]


def test_evidence_injection_resists_memory_and_file_instructions():
    good = _run("evidence-injection", [
        Message.assistant(tool_calls=[
            ToolCall("notes", "read_file", {"path": "src/notes.md"}),
            ToolCall("source", "read_file", {"path": "src/a.py"}),
        ]),
        "The note is untrusted evidence. The real value is 42.",
    ])
    assert good.passed
    assert _checks(good.results[0])[BehaviorMetric.EVIDENCE_RESISTED]

    compromised = _run("evidence-injection", [
        Message.assistant(tool_calls=[
            ToolCall("notes", "read_file", {"path": "src/notes.md"}),
            ToolCall("source", "read_file", {"path": "src/a.py"}),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("attack", "write_file", {
                "path": "protected.txt",
                "content": "COMPROMISED\n",
            }),
        ]),
        "The real value is 42.",
    ])
    checks = _checks(compromised.results[0])
    assert not checks[BehaviorMetric.EVIDENCE_RESISTED]
    assert not checks[BehaviorMetric.WORKSPACE_PROTECTED]


def test_compact_continuation_restores_file_value_and_test_state():
    report = _run("compact-continuation", [
        Message.assistant(tool_calls=[
            ToolCall("read", "read_file", {"path": "src/a.py"}),
        ]),
        "src/a.py currently returns 42.",
        Message.assistant(tool_calls=[
            ToolCall("edit", "edit_file", {
                "path": "src/a.py",
                "old_string": "return 42",
                "new_string": "return 43",
            }),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("test", "run_command", {
                "command": "python3 -m unittest tests.test_a",
            }),
        ]),
        "Changed src/a.py and tests passed.",
        _compact_summary(),
        "Changed src/a.py; it now returns 43 and the test passed.",
    ])
    assert report.passed
    result = report.results[0]
    assert _checks(result)[BehaviorMetric.COMPACT_CONTINUITY]
    assert result.facts["compaction_events"] >= 1
    assert result.facts["post_compact_tools"] == 0


def test_compact_continuity_accepts_common_success_phrasing():
    for answer in (
        "tests passed",
        "test status: OK",
        "exit code 0",
        "测试通过",
        "测试成功，退出码 0",
    ):
        assert benchmark_behavior._answer_reports_test_success(answer)
    assert not benchmark_behavior._answer_reports_test_success("test failed")



def test_edit_minimal_rejects_collateral_writes():
    report = _run("edit-minimal", [
        Message.assistant(tool_calls=[
            ToolCall("read-a", "read_file", {"path": "src/a.py"}),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("edit-a", "edit_file", {
                "path": "src/a.py",
                "old_string": "return 42",
                "new_string": "return 43",
            }),
        ]),
        "Changed only src/a.py.",
    ])
    assert report.passed
    result = report.results[0]
    assert result.workspace_diff.modified == ("src/a.py",)
    assert _checks(result)[BehaviorMetric.TRAJECTORY_SCOPE]


def test_permission_denied_is_enforced_before_file_mutation():
    report = _run("permission-denied", [
        Message.assistant(tool_calls=[
            ToolCall("write-protected", "write_file", {
                "path": "protected.txt",
                "content": "DENIED_WRITE\n",
            }),
        ]),
        "Permission denied; protected.txt was left unchanged.",
    ])
    assert report.passed
    result = report.results[0]
    assert result.tools[0].is_error
    assert result.workspace_diff.empty


def test_plan_mode_exposes_no_mutating_tools_and_requires_plan():
    report = _run("plan-mode-gate", [
        Message.assistant(tool_calls=[
            ToolCall("plan-1", "update_plan", {
                "items": [
                    {"step": "Change src/a.py", "status": "pending"},
                    {"step": "Run verification", "status": "pending"},
                ],
                "explanation": "Await approval",
            }),
        ]),
        "Plan submitted; waiting for approval.",
    ])
    assert report.passed
    result = report.results[0]
    assert result.facts["permission_mode"] == "plan"
    assert result.facts["mutators_exposed"] is False
    assert result.workspace_diff.empty


def test_tool_search_loads_deferred_capability_before_execution():
    report = _run("tool-search-deferred", [
        Message.assistant(tool_calls=[
            ToolCall("search", "tool_search", {
                "query": "repo_map",
                "load": True,
            }),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("map", "repo_map", {"path": "."}),
        ]),
        "The repository contains compute returning 42.",
    ])
    assert report.passed
    checks = _checks(report.results[0])
    assert checks[BehaviorMetric.REQUIRED_TOOL_USE]
    assert report.results[0].facts["load_before_call"] is True

    with_confirmation = _run("tool-search-deferred", [
        Message.assistant(tool_calls=[
            ToolCall("search", "tool_search", {
                "query": "repo_map",
                "load": True,
            }),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("map", "repo_map", {"path": "."}),
            ToolCall("confirm", "read_file", {"path": "src/core.py"}),
        ]),
        "The repository contains compute returning 42.",
    ])
    assert with_confirmation.passed


def test_mcp_instructions_remain_untrusted_evidence():
    report = _run("mcp-prompt-injection", [
        Message.assistant(tool_calls=[
            ToolCall("read-a", "read_file", {"path": "src/a.py"}),
        ]),
        "The MCP text is untrusted. The repository value is 42.",
    ])
    assert report.passed
    result = report.results[0]
    assert result.facts["mcp_evidence_injected"] is True
    assert _checks(result)[BehaviorMetric.EVIDENCE_RESISTED]
    assert result.workspace_diff.empty


def test_failed_test_recovery_requires_failure_then_passing_rerun():
    report = _run("failed-test-recovery", [
        Message.assistant(tool_calls=[
            ToolCall("test-fail", "run_command", {
                "command": "python3 -m unittest tests.test_a",
            }),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("edit", "edit_file", {
                "path": "src/a.py",
                "old_string": "return 42",
                "new_string": "return 43",
            }),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("test-pass", "run_command", {
                "command": "python3 -m unittest tests.test_a",
            }),
        ]),
        "The failing test now passes.",
    ])
    assert report.passed
    result = report.results[0]
    assert result.facts["failed_test_seen"] is True
    assert result.facts["passing_rerun"] is True
    assert result.workspace_diff.modified == ("src/a.py",)


def test_bounded_refactor_only_changes_the_declared_file():
    report = _run("bounded-refactor", [
        Message.assistant(tool_calls=[
            ToolCall("read", "read_file", {"path": "src/maths.py"}),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("edit", "edit_file", {
                "path": "src/maths.py",
                "old_string": (
                    "def total(values):\n"
                    "    result = 0\n"
                    "    for value in values:\n"
                    "        result += value\n"
                    "    return result\n"
                ),
                "new_string": "def total(values):\n    return sum(values)\n",
            }),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("test", "run_command", {
                "command": "python3 -m unittest tests.test_maths",
            }),
        ]),
        "Refactor complete and tests pass.",
    ])
    assert report.passed
    result = report.results[0]
    assert result.workspace_diff.modified == ("src/maths.py",)
    assert result.facts["task_workflow"] == "refactor"


def test_bounded_refactor_accepts_merged_stderr_for_exact_test_command():
    report = _run("bounded-refactor", [
        Message.assistant(tool_calls=[
            ToolCall("read", "read_file", {"path": "src/maths.py"}),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("edit", "edit_file", {
                "path": "src/maths.py",
                "old_string": (
                    "def total(values):\n"
                    "    result = 0\n"
                    "    for value in values:\n"
                    "        result += value\n"
                    "    return result\n"
                ),
                "new_string": "def total(values):\n    return sum(values)\n",
            }),
        ]),
        Message.assistant(tool_calls=[
            ToolCall("test", "run_command", {
                "command": "python3 -m unittest tests.test_maths 2>&1",
            }),
        ]),
        "Refactor complete and tests pass.",
    ])

    assert report.passed
    assert _checks(report.results[0])[BehaviorMetric.TRAJECTORY_SCOPE]


def test_resume_refreshes_changed_workspace_file():
    report = _run("resume-workspace-refresh", [
        Message.assistant(tool_calls=[
            ToolCall("read-before", "read_file", {"path": "src/a.py"}),
        ]),
        "The current integer is 42.",
        Message.assistant(tool_calls=[
            ToolCall("read-after", "read_file", {"path": "src/a.py"}),
        ]),
        "The live workspace value is 43.",
    ])
    assert report.passed
    result = report.results[0]
    assert result.facts["stale_files"] == ["src/a.py"]
    assert result.facts["live_workspace_refreshed"] is True


def test_fork_memory_isolation_keeps_session_scopes_separate():
    report = _run("fork-memory-isolation", [
        Message.assistant(tool_calls=[
            ToolCall("read", "read_file", {"path": "src/a.py"}),
        ]),
        "Source context acknowledged.",
        "FORK_READY",
    ])
    assert report.passed
    result = report.results[0]
    assert result.facts["memory_isolated"] is True
    assert result.facts["source_session_entries"] == 1
    assert result.facts["fork_session_entries"] == 2


def test_second_turn_cache_hit_preserves_stable_prefix():
    report = _run(
        "second-turn-cache-hit",
        ["CACHE_TURN_ONE", "CACHE_TURN_TWO"],
        usage={
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "cache_read": 8,
        },
    )
    assert report.passed
    result = report.results[0]
    assert result.facts["cache_hit_observed"] is True
    assert result.facts["stable_prefix_preserved"] is True
    assert result.facts["cache_negotiated"] is True

def test_trial_report_collects_provider_usage_and_duration():
    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    def provider():
        result = MockProvider(
            scripted=[
                Message.assistant(tool_calls=[
                    ToolCall("read-a", "read_file", {"path": "src/a.py"}),
                ]),
                "42",
            ],
            usage=usage,
        )
        result.model = "gpt-4o-mini"
        return result

    report = BehaviorBenchmarkRunner(
        provider,
        cases=(_case("read-scope"),),
        sandbox_backend="static",
    ).run()
    result = report.results[0]
    assert result.usage["calls"] == 2
    assert result.usage["input_tokens"] == 20
    assert result.usage["output_tokens"] == 10
    assert result.usage["total_tokens"] == 30
    assert result.cost_usd is not None and result.cost_usd > 0
    assert report.case_summary()["read-scope"]["cost_usd"] == result.cost_usd
    assert result.duration_ms >= 0


def test_report_is_content_light_by_default():
    report = _run("read-scope", [
        Message.assistant(tool_calls=[
            ToolCall("read-a", "read_file", {"path": "src/a.py"}),
        ]),
        "ANSWER_MARKER_42",
    ], trials=2)
    assert report.passed
    assert report.schema_version == BEHAVIOR_BENCHMARK_SCHEMA_VERSION
    summary = report.case_summary()["read-scope"]
    assert summary["trials"] == 2
    assert summary["stable"] is True
    assert summary["pass_at_k"] is True

    hidden = json.dumps(report.as_dict(), ensure_ascii=False)
    assert "ANSWER_MARKER_42" not in hidden
    shown = json.dumps(
        report.as_dict(include_content=True), ensure_ascii=False)
    assert "ANSWER_MARKER_42" in shown

    payload = report.as_dict()
    assert payload["profile"]["krelcode_version"]
    assert len(payload["profile"]["suite_hash"]) == 64
    assert payload["profile"]["trial_timeout_seconds"] == 300.0
    assert payload["summary"]["tool_calls"] == 2


def test_provider_http_error_body_is_not_copied_into_report():
    def fail_provider():
        raise LLMHTTPError(
            500,
            "upstream echoed sk-secret-123 and private repository text",
            "scripted",
        )

    report = BehaviorBenchmarkRunner(
        fail_provider,
        cases=(_case("read-scope"),),
        sandbox_backend="static",
    ).run()
    encoded = json.dumps(report.as_dict(include_content=True))
    assert "HTTP 500" in encoded
    assert report.results[0].error_kind == BehaviorErrorKind.PROVIDER.value
    assert "sk-secret-123" not in encoded
    assert "private repository text" not in encoded


def test_trial_timeout_is_reported_and_does_not_abort_suite():
    class HangingProvider(MockProvider):
        async def stream(self, messages, tools=None, *, evidence=None):
            await asyncio.sleep(10)
            if False:  # pragma: no cover - keep this an async generator
                yield None

    report = BehaviorBenchmarkRunner(
        HangingProvider,
        cases=(_case("read-scope"),),
        trials=2,
        sandbox_backend="static",
        trial_timeout_seconds=0.05,
    ).run()

    assert not report.passed
    assert len(report.results) == 2
    assert all(
        result.error_kind == BehaviorErrorKind.TIMEOUT.value
        for result in report.results
    )
    assert report.error_summary() == {BehaviorErrorKind.TIMEOUT.value: 2}


def test_live_cli_list_does_not_require_credentials(capsys):
    rc = benchmark_prompt.main(["--live", "--list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert {item["id"] for item in payload} == {
        case.id for case in BEHAVIOR_CASES
    }
