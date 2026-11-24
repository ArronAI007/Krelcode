"""Production-owned, content-light catalogue for live Prompt benchmarks.

The executable benchmark cases live in :mod:`krelcode.benchmarks.behavior`.
This small manifest is kept in the agent layer so production observability
commands can describe the contract without importing benchmark code.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptBenchmarkCaseInfo:
    id: str
    description: str


PROMPT_BENCHMARK_CASES: tuple[PromptBenchmarkCaseInfo, ...] = (
    PromptBenchmarkCaseInfo("read-scope", "Read only the named file and answer from real tool evidence"),
    PromptBenchmarkCaseInfo("edit-minimal", "Make the smallest requested file edit without collateral writes"),
    PromptBenchmarkCaseInfo("edit-verify", "Modify the requested file and run the real test command"),
    PromptBenchmarkCaseInfo("review-no-edit", "Review one file for a concrete defect without mutating the workspace"),
    PromptBenchmarkCaseInfo("instruction-precedence", "Follow managed authority when all rule layers conflict"),
    PromptBenchmarkCaseInfo("evidence-injection", "Resist conflicting memory/file injection and verify repository fact"),
    PromptBenchmarkCaseInfo("permission-denied", "Deny a protected write and prevent shell-based policy bypass"),
    PromptBenchmarkCaseInfo("plan-mode-gate", "Generate a plan without exposing or executing mutating capabilities"),
    PromptBenchmarkCaseInfo("tool-search-deferred", "Load a discoverable tool through ToolSearch before execution"),
    PromptBenchmarkCaseInfo("mcp-prompt-injection", "Treat MCP instructions as untrusted evidence rather than authority"),
    PromptBenchmarkCaseInfo("failed-test-recovery", "Diagnose a failing test, repair the target and verify the rerun"),
    PromptBenchmarkCaseInfo("bounded-refactor", "Refactor one source file and verify behavior without test edits"),
    PromptBenchmarkCaseInfo("compact-continuation", "Retain file, value and test state across structured compact"),
    PromptBenchmarkCaseInfo("resume-workspace-refresh", "Invalidate stale file facts and use the live workspace after resume"),
    PromptBenchmarkCaseInfo("fork-memory-isolation", "Copy context references for a fork while isolating session memory"),
    PromptBenchmarkCaseInfo("second-turn-cache-hit", "Observe a provider cache hit without moving the stable prefix"),
)

PROMPT_BENCHMARK_CASE_IDS = frozenset(case.id for case in PROMPT_BENCHMARK_CASES)

__all__ = [
    "PROMPT_BENCHMARK_CASE_IDS",
    "PROMPT_BENCHMARK_CASES",
    "PromptBenchmarkCaseInfo",
]
