"""Stable live benchmark catalog; implementations live in scenario modules."""

from __future__ import annotations

from .behavior_advanced_scenarios import (
    _run_bounded_refactor,
    _run_edit_minimal,
    _run_failed_test_recovery,
    _run_fork_memory_isolation,
    _run_mcp_prompt_injection,
    _run_permission_denied,
    _run_plan_mode_gate,
    _run_resume_workspace_refresh,
    _run_second_turn_cache_hit,
    _run_tool_search_deferred,
)
from .behavior_fixtures import (
    _seed_bounded_refactor,
    _seed_compact,
    _seed_edit_minimal,
    _seed_edit_verify,
    _seed_evidence_injection,
    _seed_failed_test_recovery,
    _seed_fork_memory_isolation,
    _seed_instruction_precedence,
    _seed_mcp_prompt_injection,
    _seed_permission_denied,
    _seed_plan_mode,
    _seed_read_scope,
    _seed_resume_workspace_refresh,
    _seed_review_no_edit,
    _seed_tool_search,
)
from .behavior_models import BehaviorCase
from .behavior_scenarios import (
    _run_compact_continuation,
    _run_edit_verify,
    _run_evidence_injection,
    _run_instruction_precedence,
    _run_read_scope,
    _run_review_no_edit,
)


BEHAVIOR_CASES: tuple[BehaviorCase, ...] = (
    BehaviorCase(
        "read-scope",
        "Read only the named file and answer from real tool evidence",
        _seed_read_scope,
        _run_read_scope,
        expected_workflow="code_explanation",
    ),
    BehaviorCase(
        "edit-minimal",
        "Make the smallest requested file edit without collateral writes",
        _seed_edit_minimal,
        _run_edit_minimal,
    ),
    BehaviorCase(
        "edit-verify",
        "Modify the requested file and run the real test command",
        _seed_edit_verify,
        _run_edit_verify,
    ),
    BehaviorCase(
        "review-no-edit",
        "Review one file for a concrete defect without mutating the workspace",
        _seed_review_no_edit,
        _run_review_no_edit,
        expected_workflow="code_review",
    ),
    BehaviorCase(
        "instruction-precedence",
        "Follow managed authority when all rule layers conflict",
        _seed_instruction_precedence,
        _run_instruction_precedence,
    ),
    BehaviorCase(
        "evidence-injection",
        "Resist conflicting memory/file injection and verify repository fact",
        _seed_evidence_injection,
        _run_evidence_injection,
    ),
    BehaviorCase(
        "permission-denied",
        "Deny a protected write and prevent shell-based policy bypass",
        _seed_permission_denied,
        _run_permission_denied,
    ),
    BehaviorCase(
        "plan-mode-gate",
        "Generate a plan without exposing or executing mutating capabilities",
        _seed_plan_mode,
        _run_plan_mode_gate,
    ),
    BehaviorCase(
        "tool-search-deferred",
        "Load a discoverable tool through ToolSearch before execution",
        _seed_tool_search,
        _run_tool_search_deferred,
        expected_workflow="repository_exploration",
    ),
    BehaviorCase(
        "mcp-prompt-injection",
        "Treat MCP instructions as untrusted evidence rather than authority",
        _seed_mcp_prompt_injection,
        _run_mcp_prompt_injection,
    ),
    BehaviorCase(
        "failed-test-recovery",
        "Diagnose a failing test, repair the target and verify the rerun",
        _seed_failed_test_recovery,
        _run_failed_test_recovery,
        expected_workflow="bug_fix",
    ),
    BehaviorCase(
        "bounded-refactor",
        "Refactor one source file and verify behavior without test edits",
        _seed_bounded_refactor,
        _run_bounded_refactor,
        expected_workflow="refactor",
    ),
    BehaviorCase(
        "compact-continuation",
        "Retain file, value and test state across structured compact",
        _seed_compact,
        _run_compact_continuation,
    ),
    BehaviorCase(
        "resume-workspace-refresh",
        "Invalidate stale file facts and use the live workspace after resume",
        _seed_resume_workspace_refresh,
        _run_resume_workspace_refresh,
    ),
    BehaviorCase(
        "fork-memory-isolation",
        "Copy context references for a fork while isolating session memory",
        _seed_fork_memory_isolation,
        _run_fork_memory_isolation,
    ),
    BehaviorCase(
        "second-turn-cache-hit",
        "Observe a provider cache hit without moving the stable prefix",
        _seed_resume_workspace_refresh,
        _run_second_turn_cache_hit,
    ),
)
