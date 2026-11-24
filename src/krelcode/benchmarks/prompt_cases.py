"""Stable deterministic Prompt/Context benchmark catalog."""

from __future__ import annotations

from .prompt_models import PromptBenchmarkCase, PromptBenchmarkMode


BENCHMARK_CASES: tuple[PromptBenchmarkCase, ...] = (
    PromptBenchmarkCase(
        "main", PromptBenchmarkMode.MAIN,
        "Main Agent prompt assembly, memory authority and exact wire identity"),
    PromptBenchmarkCase(
        "headless", PromptBenchmarkMode.HEADLESS,
        "Single-message session runner through AgentRuntime"),
    PromptBenchmarkCase(
        "stream-json", PromptBenchmarkMode.STREAM_JSON,
        "JSONL stream runner context event and ledger consistency"),
    PromptBenchmarkCase(
        "sdk", PromptBenchmarkMode.SDK,
        "Python SDK subprocess protocol and context event identity"),
    PromptBenchmarkCase(
        "subagent", PromptBenchmarkMode.SUBAGENT,
        "Subagent instruction inheritance, history isolation and L7 memory"),
    PromptBenchmarkCase(
        "resume", PromptBenchmarkMode.RESUME,
        "Context checkpoint restore with live workspace invalidation"),
    PromptBenchmarkCase(
        "fork", PromptBenchmarkMode.FORK,
        "Session fork checkpoint and session-memory independence"),
    PromptBenchmarkCase(
        "rewind", PromptBenchmarkMode.REWIND,
        "Turn rewind restores history, plan and the matching context ledger"),
    PromptBenchmarkCase(
        "compact", PromptBenchmarkMode.COMPACT,
        "Structured compact continuation and checkpoint recovery"),
)
