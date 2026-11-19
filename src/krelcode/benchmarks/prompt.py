"""Content-light Prompt/Context benchmark for every Krelcode run mode.

The benchmark intentionally observes provenance, hashes, counts and checkpoint
identities instead of answers or prompt bodies.  Every scenario drives the same
production components used by the corresponding mode; there is no second prompt
assembly implementation in this module.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import io
import json
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Mapping, Sequence
from unittest.mock import patch

from ..agent.compression import ContextManager
from ..agent.context import (
    ContextOptions,
    ContextSection,
    WorkspaceContext,
)
from ..agent.context_assembler import ContextAssembler
from ..agent.context_cache_ledger import ContextCacheLedger
from ..agent.context_source import default_context_sources
from ..agent.fragments import (
    LAYER_L7_EVIDENCE,
    PromptFragmentRegistry,
    base_fragment,
    sync_rule_fragments,
)
from ..agent.loop import AgentLoop
from ..agent.prompt_plan import PromptPlanSnapshot
from ..agent.rules import RuleLoader
from ..agent.runtime import AgentRuntime
from ..agent.tokens import estimate_text_tokens
from ..agent.working_context import WorkingContextStore
from ..core.events import EventBus
from ..core.messages import Message
from ..core.redact import SecretRedactor
from ..llm.base import (
    PreparedLLMRequest,
    ProviderCapabilities,
    StreamFinished,
    TextDelta,
    resolve_system_prompt_blocks,
)
from ..safety.policy import AllowAllPolicy
from ..sdk import KrelcodeClient, KrelcodeOptions
from ..session.compact_checkpoint import CompactCheckpointStore
from ..session.context_checkpoint import (
    ContextCheckpointCoordinator,
    ContextCheckpointStore,
    WorkspaceFingerprint,
    hash_tracked_files,
    restore_context_checkpoint,
)
from ..session.controller import SessionController
from ..session.lifecycle import SessionLifecycle
from ..session.memory import MemoryKind, MemoryScope, MemoryStore
from ..session.runner import run_session
from ..session.stream_json_runner import run_stream_json_session
from ..tools.base import ToolContext
from ..tools.registry import ToolRegistry


PROMPT_BENCHMARK_SCHEMA_VERSION = 1


class PromptBenchmarkMode(str, Enum):
    """The nine execution surfaces required by the benchmark contract."""

    MAIN = "main"
    HEADLESS = "headless"
    STREAM_JSON = "stream-json"
    SDK = "sdk"
    SUBAGENT = "subagent"
    RESUME = "resume"
    FORK = "fork"
    REWIND = "rewind"
    COMPACT = "compact"


class PromptBenchmarkMetric(str, Enum):
    """Stable machine-readable acceptance metric identifiers."""

    RELEVANT_SCOPE_ONLY = "relevant_scope_only"
    MEMORY_NOT_INSTRUCTION = "memory_not_instruction"
    RULE_PRECEDENCE_STABLE = "rule_precedence_stable"
    SUBAGENT_HISTORY_ISOLATED = "subagent_history_isolated"
    SUBAGENT_MEMORY_EVIDENCE = "subagent_memory_evidence"
    STABLE_PREFIX_ISOLATED = "stable_prefix_isolated"
    COMPACT_RECOVERABLE = "compact_recoverable"
    PROVIDER_HASH_EXACT = "provider_hash_exact"


@dataclass(frozen=True)
class PromptBenchmarkCase:
    id: str
    mode: PromptBenchmarkMode
    description: str

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "mode": self.mode.value,
            "description": self.description,
        }


@dataclass(frozen=True)
class PromptBenchmarkCheck:
    metric: PromptBenchmarkMetric
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric.value,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PromptBenchmarkResult:
    case_id: str
    mode: PromptBenchmarkMode
    passed: bool
    checks: tuple[PromptBenchmarkCheck, ...] = ()
    prompt_plan: Mapping[str, object] = field(default_factory=dict)
    ledger: Mapping[str, object] = field(default_factory=dict)
    facts: Mapping[str, object] = field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "mode": self.mode.value,
            "passed": self.passed,
            "checks": [item.as_dict() for item in self.checks],
            "prompt_plan": dict(self.prompt_plan),
            "ledger": dict(self.ledger),
            "facts": dict(self.facts),
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class PromptBenchmarkReport:
    schema_version: int
    generated_at: str
    passed: bool
    results: tuple[PromptBenchmarkResult, ...]
    duration_ms: int

    def metric_summary(self) -> dict[str, dict[str, object]]:
        summary: dict[str, dict[str, object]] = {}
        for metric in PromptBenchmarkMetric:
            checks = [
                check
                for result in self.results
                for check in result.checks
                if check.metric is metric
            ]
            passed = sum(1 for check in checks if check.passed)
            summary[metric.value] = {
                "passed": bool(checks) and passed == len(checks),
                "checks": len(checks),
                "passed_checks": passed,
                "failed_checks": len(checks) - passed,
            }
        return summary

    def mode_summary(self) -> dict[str, dict[str, object]]:
        summary: dict[str, dict[str, object]] = {}
        for mode in PromptBenchmarkMode:
            results = [item for item in self.results if item.mode is mode]
            summary[mode.value] = {
                "covered": bool(results),
                "cases": len(results),
                "passed": sum(1 for item in results if item.passed),
                "failed": sum(1 for item in results if not item.passed),
            }
        return summary

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "passed": self.passed,
            "duration_ms": self.duration_ms,
            "summary": {
                "cases": len(self.results),
                "passed": sum(1 for item in self.results if item.passed),
                "failed": sum(1 for item in self.results if not item.passed),
            },
            "metrics": self.metric_summary(),
            "modes": self.mode_summary(),
            "results": [item.as_dict() for item in self.results],
        }


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


@dataclass
class _ScenarioOutcome:
    checks: list[PromptBenchmarkCheck]
    prompt_plan: PromptPlanSnapshot | None = None
    ledger: ContextCacheLedger | None = None
    facts: dict[str, object] = field(default_factory=dict)


class PromptBenchmarkRunner:
    """Run deterministic, isolated prompt/context benchmark cases."""

    def __init__(
        self,
        cases: Sequence[PromptBenchmarkCase] = BENCHMARK_CASES,
        *,
        sdk_timeout: float = 30.0,
    ) -> None:
        self.cases = tuple(cases)
        self.sdk_timeout = max(1.0, float(sdk_timeout))

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(case.id for case in self.cases)

    def run(self, case_ids: Sequence[str] | None = None) -> PromptBenchmarkReport:
        return asyncio.run(self.run_async(case_ids))

    async def run_async(
        self, case_ids: Sequence[str] | None = None,
    ) -> PromptBenchmarkReport:
        requested = tuple(case_ids or self.case_ids)
        selected = [
            case for case in self.cases
            if not case_ids or case.id in requested
        ]
        unknown = sorted(set(requested) - {case.id for case in self.cases})
        if unknown:
            raise ValueError("未知 benchmark case: " + ", ".join(unknown))
        started = time.monotonic()
        results = []
        for case in selected:
            case_started = time.monotonic()
            try:
                with tempfile.TemporaryDirectory(
                    prefix=f"krelcode-prompt-{case.id}-"
                ) as raw_root:
                    outcome = await self._run_case(case, Path(raw_root))
                checks = tuple(outcome.checks)
                passed = bool(checks) and all(item.passed for item in checks)
                result = PromptBenchmarkResult(
                    case_id=case.id,
                    mode=case.mode,
                    passed=passed,
                    checks=checks,
                    prompt_plan=(
                        outcome.prompt_plan.as_dict()
                        if outcome.prompt_plan is not None else {}),
                    ledger=(
                        outcome.ledger.as_dict()
                        if outcome.ledger is not None else {}),
                    facts=_safe_mapping(outcome.facts),
                    duration_ms=_elapsed_ms(case_started),
                )
            except Exception as exc:  # keep one case from hiding the suite
                result = PromptBenchmarkResult(
                    case_id=case.id,
                    mode=case.mode,
                    passed=False,
                    error=_safe_error(exc),
                    duration_ms=_elapsed_ms(case_started),
                )
            results.append(result)
        report = PromptBenchmarkReport(
            schema_version=PROMPT_BENCHMARK_SCHEMA_VERSION,
            generated_at=datetime.now(timezone.utc).isoformat(),
            passed=bool(results) and all(item.passed for item in results),
            results=tuple(results),
            duration_ms=_elapsed_ms(started),
        )
        return report

    async def _run_case(
        self, case: PromptBenchmarkCase, root: Path,
    ) -> _ScenarioOutcome:
        handlers: dict[
            PromptBenchmarkMode,
            Callable[[Path], object],
        ] = {
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
        outcome = handlers[case.mode](root)
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        if not isinstance(outcome, _ScenarioOutcome):
            raise TypeError(f"{case.id} 返回了非法 benchmark outcome")
        return outcome


class _BenchmarkProvider:
    """Prepared provider with deterministic responses and exact wire digest."""

    name = "benchmark-prepared"
    model = "benchmark-model"
    capabilities = ProviderCapabilities(
        supports_tools=True, supports_streaming=True)

    def __init__(self, responses: Sequence[str] = ()) -> None:
        self._responses = deque(responses)
        self.default_response = "benchmark response"
        self.last_prepared: PreparedLLMRequest | None = None
        self.executed: list[PreparedLLMRequest] = []

    def prepare_request(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, object]] | None = None,
    ) -> PreparedLLMRequest:
        blocks = resolve_system_prompt_blocks(messages)
        payload = {
            "provider": self.name,
            "model": self.model,
            "messages": [_message_payload(message) for message in messages],
            "tools": list(tools or ()),
        }
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str,
            separators=(",", ":"),
        ).encode("utf-8")
        prepared = PreparedLLMRequest(
            provider=self.name,
            model=self.model,
            url="https://benchmark.invalid/v1/chat",
            headers={},
            body=body,
            request_body_tokens=estimate_text_tokens(body.decode("utf-8")),
            payload_digest=hashlib.sha256(body).hexdigest(),
            cache_mode="explicit" if blocks is not None else "none",
            stable_cache_chars=blocks.stable_chars if blocks is not None else 0,
            stable_cache_hash=blocks.stable_hash if blocks is not None else "",
            dynamic_cache_chars=blocks.dynamic_chars if blocks is not None else 0,
            dynamic_cache_hash=blocks.dynamic_hash if blocks is not None else "",
            stable_system_block_count=(
                len(blocks.stable_system_blocks)
                if blocks is not None else 0),
            dynamic_system_block_count=(
                len(blocks.dynamic_system_blocks)
                if blocks is not None else 0),
            cache_control_boundary=(
                blocks.cache_control_boundary if blocks is not None else 0),
            instruction_system_block_count=(
                len(blocks.instruction_blocks) if blocks is not None else 0),
            evidence_system_block_count=(
                len(blocks.evidence_system_blocks)
                if blocks is not None else 0),
            instruction_hash=(
                blocks.instruction_hash if blocks is not None else ""),
            evidence_hash=blocks.evidence_hash if blocks is not None else "",
        )
        self.last_prepared = prepared
        return prepared

    async def stream_prepared(self, prepared: PreparedLLMRequest):
        self.executed.append(prepared)
        text = self._responses.popleft() if self._responses else self.default_response
        yield TextDelta(text)
        yield StreamFinished(message=Message.assistant(text), finish_reason="stop", usage={})

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, object]] | None = None,
    ):
        prepared = self.prepare_request(messages, tools)
        async for event in self.stream_prepared(prepared):
            yield event


class _BenchmarkFixture:
    """One isolated workspace shared by a benchmark scenario's components."""

    def __init__(
        self,
        root: Path,
        *,
        provider: _BenchmarkProvider | None = None,
        context_manager: ContextManager | None = None,
        compact_store: CompactCheckpointStore | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.home = self.root / "home"
        self.target = self.root / "src" / "a.py"
        self.provider = provider or _BenchmarkProvider()
        self.context_manager = context_manager
        self.compact_store = compact_store
        self._create_workspace()
        self.memory = MemoryStore(
            self.root,
            path=self.root / ".krelcode" / "memory" / "entries.jsonl",
            user_path=self.home / ".krelcode" / "memory" / "entries.jsonl",
            session_id="benchmark-source",
        )
        self.memory_entry = self.memory.add(
            "The benchmark project validates prompt evidence with pytest.",
            scope=MemoryScope.PROJECT,
            kind=MemoryKind.KNOWLEDGE,
            source="benchmark",
            pinned=True,
        )
        self.options = ContextOptions(
            engine="v3",
            include_git=True,
            include_tree=True,
            include_directory_rules=True,
        )
        self.rule_loader = RuleLoader(
            self.root,
            home=self.home,
            managed_paths=(self.root / ".krelcode" / "managed" / "AGENTS.md",),
        )
        self.workspace = self._workspace_context()
        self.registry = PromptFragmentRegistry([base_fragment()])
        sync_rule_fragments(self.registry, self.workspace)
        self.loop = self.make_loop(
            context_manager=context_manager,
            compact_store=compact_store,
        )

    def _create_workspace(self) -> None:
        (self.root / "src").mkdir(parents=True, exist_ok=True)
        (self.root / "README.md").write_text(
            "unrelated repository documentation\n", encoding="utf-8")
        (self.root / "src" / "a.py").write_text(
            "def answer():\n    return 42\n", encoding="utf-8")
        (self.root / "src" / "b.py").write_text(
            "def unrelated():\n    return 'noise'\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text(
            "Project benchmark policy: use pytest.\n", encoding="utf-8")
        (self.root / "src" / "AGENTS.md").write_text(
            "Directory benchmark policy: inspect src/a.py directly.\n",
            encoding="utf-8")
        managed = self.root / ".krelcode" / "managed"
        managed.mkdir(parents=True, exist_ok=True)
        (managed / "AGENTS.md").write_text(
            "Managed benchmark policy: never modify protected paths.\n",
            encoding="utf-8")
        user_rules = self.home / ".krelcode"
        user_rules.mkdir(parents=True, exist_ok=True)
        (user_rules / "AGENTS.md").write_text(
            "User benchmark preference: keep evidence non-authoritative.\n",
            encoding="utf-8")

    def _workspace_context(self) -> WorkspaceContext:
        stack = self.rule_loader.load_stack(
            (self.target,), include_directory=True)
        sections = (
            ContextSection(
                "git", "Git: branch benchmark-old\nstatus: clean",
                priority=20, source="benchmark-git"),
            ContextSection(
                "tree",
                "Workspace tree: README.md, src/a.py, src/b.py",
                priority=50,
                source="benchmark-tree",
            ),
        )
        text = "<workspace_context>\n" + "\n\n".join(
            section.content for section in sections) + "\n</workspace_context>"
        return WorkspaceContext(
            root=self.root,
            sections=sections,
            text=text,
            max_tokens=6_000,
            total_tokens=estimate_text_tokens(text),
            rule_bundle=stack.combined_bundle,
            rule_layers=stack.layers,
        )

    def make_loop(
        self,
        *,
        context_manager: ContextManager | None = None,
        compact_store: CompactCheckpointStore | None = None,
    ) -> AgentLoop:
        working = WorkingContextStore(self.root)
        sources = default_context_sources(
            self.rule_loader,
            working_context_store=working,
            engine="v3",
            memory_store=self.memory,
        )
        registry = PromptFragmentRegistry(self.registry.list())
        snapshot = registry.snapshot()
        loop = AgentLoop(
            llm=self.provider,
            registry=ToolRegistry(),
            tool_context=ToolContext(
                workspace_root=self.root,
                sandbox_enabled=False,
            ),
            policy=AllowAllPolicy(),
            bus=EventBus(),
            system_prompt=snapshot.text,
            context_manager=context_manager,
            context_assembler=ContextAssembler(sources, engine="v3"),
            compact_checkpoint_store=compact_store,
            working_context_store=working,
        )
        loop.prompt_registry = registry
        loop.prompt_snapshot = snapshot
        loop.workspace_context = self.workspace
        loop.context_options = self.options
        loop.context_engine = "v3"
        loop.context_enabled = True
        loop.rule_loader = self.rule_loader
        loop.memory_store = self.memory
        loop.skill_manager = None
        loop.sync_context_attachments(include_mcp=False)
        return loop


def _message_payload(message: Message) -> dict[str, object]:
    return {
        "role": message.role,
        "content": message.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "name": call.name,
                "arguments": dict(call.arguments),
            }
            for call in (message.tool_calls or ())
        ],
        "tool_call_id": message.tool_call_id or "",
        "tool_name": message.tool_name or "",
        "tool_is_error": message.tool_is_error,
    }


def _check(
    metric: PromptBenchmarkMetric,
    passed: bool,
    detail: str,
) -> PromptBenchmarkCheck:
    return PromptBenchmarkCheck(metric=metric, passed=bool(passed), detail=detail)


def _memory_fragments(
    plan: PromptPlanSnapshot,
):
    return tuple(
        fragment for fragment in plan.fragments
        if fragment.layer == LAYER_L7_EVIDENCE
        or fragment.source in {"memory", "memory_retrieval", "inherited_memory"}
        or fragment.id.startswith("memory:")
    )


def _memory_is_evidence(plan: PromptPlanSnapshot) -> bool:
    fragments = _memory_fragments(plan)
    return bool(fragments) and all(
        fragment.provenance.get("source_role") == "evidence"
        and fragment.provenance.get("instruction") is False
        and fragment.provenance.get("requires_validation") is True
        and not fragment.stable
        for fragment in fragments
    )


_RULE_ORDER = (
    "managed:rules",
    "user:rules",
    "project:rules",
    "directory:rules",
)


def _rule_order(plan: PromptPlanSnapshot) -> tuple[str, ...]:
    positions = {
        fragment.id: index
        for index, fragment in enumerate(plan.fragments)
        if fragment.id in _RULE_ORDER
    }
    return tuple(sorted(positions, key=positions.get))


def _rule_order_is_stable(
    first: PromptPlanSnapshot,
    second: PromptPlanSnapshot,
) -> bool:
    return _rule_order(first) == _RULE_ORDER and _rule_order(second) == _RULE_ORDER


def _stable_prefix_is_isolated(
    first: PromptPlanSnapshot,
    second: PromptPlanSnapshot,
) -> bool:
    stable_ids = {
        fragment.id
        for fragment in first.fragments
        if fragment.stable
    } | {
        fragment.id
        for fragment in second.fragments
        if fragment.stable
    }
    polluted = stable_ids.intersection({
        "memory:scoped",
        "subagent:inherited-memory",
        "attachment:environment_snapshot",
        "attachment:environment_delta",
        "attachment:date",
        "attachment:mode_reminder",
    })
    return (
        bool(first.stable_hash)
        and first.stable_hash == second.stable_hash
        and not polluted
    )


def _provider_hash_is_exact(
    plan: PromptPlanSnapshot,
    ledger: ContextCacheLedger,
    prepared: PreparedLLMRequest | None,
) -> bool:
    if prepared is None:
        return False
    actual_body_hash = hashlib.sha256(prepared.body).hexdigest()
    return (
        ledger.wire_fidelity == "exact"
        and bool(ledger.provider_request_digest)
        and prepared.payload_digest == actual_body_hash
        and ledger.provider_request_digest == prepared.payload_digest
        and ledger.stable_hash == plan.stable_hash
        and prepared.stable_cache_hash == plan.stable_hash
    )


def _scope_filtered(plan: PromptPlanSnapshot) -> bool:
    return any(
        omission.reason == "file-explanation-scope"
        and omission.id in {
            "workspace:tree",
            "workspace:git",
            "attachment:environment_snapshot",
            "attachment:environment_delta",
        }
        for omission in plan.omitted
    )


async def _run_main(root: Path) -> _ScenarioOutcome:
    fixture = _BenchmarkFixture(root)
    loop = fixture.loop
    prompt = "请解释 `src/a.py`"
    await loop.run(prompt, turn_id="benchmark-main-1")
    first_plan = loop.last_context_plan.prompt_plan
    fixture.memory.add(
        "A second benchmark evidence item must never become an instruction.",
        scope=MemoryScope.PROJECT,
        kind=MemoryKind.KNOWLEDGE,
        source="benchmark-followup",
    )
    await loop.run(prompt, turn_id="benchmark-main-2")
    second_plan = loop.last_context_plan.prompt_plan
    second_ledger = loop.last_context_cache_ledger

    checks = [
        _check(
            PromptBenchmarkMetric.RELEVANT_SCOPE_ONLY,
            _scope_filtered(second_plan),
            "file explanation omitted the volatile workspace snapshot",
        ),
        _check(
            PromptBenchmarkMetric.MEMORY_NOT_INSTRUCTION,
            _memory_is_evidence(second_plan)
            and first_plan.instruction_hash == second_plan.instruction_hash
            and first_plan.evidence_hash != second_plan.evidence_hash,
            "memory stayed in L7 evidence and did not change instruction_hash",
        ),
        _check(
            PromptBenchmarkMetric.RULE_PRECEDENCE_STABLE,
            _rule_order_is_stable(first_plan, second_plan),
            "managed -> user -> project -> directory fragment order was stable",
        ),
        _check(
            PromptBenchmarkMetric.STABLE_PREFIX_ISOLATED,
            _stable_prefix_is_isolated(first_plan, second_plan),
            "dynamic memory/attachments did not enter the stable prefix",
        ),
        _check(
            PromptBenchmarkMetric.PROVIDER_HASH_EXACT,
            second_ledger is not None
            and _provider_hash_is_exact(
                second_plan, second_ledger, fixture.provider.last_prepared),
            "ledger digest matched the exact prepared provider body",
        ),
    ]
    return _ScenarioOutcome(
        checks=checks,
        prompt_plan=second_plan,
        ledger=second_ledger,
        facts={
            "turns": 2,
            "memory_hits": len(second_plan.memory_hits),
            "omitted": [item.id for item in second_plan.omitted],
            "rule_fragments": list(_rule_order(second_plan)),
        },
    )


async def _run_headless(root: Path) -> _ScenarioOutcome:
    fixture = _BenchmarkFixture(root)
    runtime = AgentRuntime(fixture.loop)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = await run_session(
            SimpleNamespace(
                message="explain src/a.py",
                output_format="text",
                resume=None,
                no_session=True,
            ),
            runtime,
            supervisor=None,
            checkpoints=None,
            usage=_UsageTrackerFactory(),
            model_switch=None,
            skills_reload=None,
            redactor=SecretRedactor(),
            log=lambda _line: None,
        )
    plan = fixture.loop.last_context_plan.prompt_plan
    ledger = fixture.loop.last_context_cache_ledger
    checks = [
        _check(
            PromptBenchmarkMetric.MEMORY_NOT_INSTRUCTION,
            _memory_is_evidence(plan),
            "headless request kept memory in the evidence channel",
        ),
        _check(
            PromptBenchmarkMetric.STABLE_PREFIX_ISOLATED,
            bool(plan.stable_hash) and not any(
                fragment.stable for fragment in _memory_fragments(plan)),
            "headless stable prefix excluded memory evidence",
        ),
        _check(
            PromptBenchmarkMetric.PROVIDER_HASH_EXACT,
            ledger is not None
            and _provider_hash_is_exact(
                plan, ledger, fixture.provider.last_prepared),
            "headless ledger matched the exact provider body",
        ),
    ]
    return _ScenarioOutcome(
        checks=checks,
        prompt_plan=plan,
        ledger=ledger,
        facts={"exit_code": rc, "output_bytes": len(output.getvalue())},
    )


async def _run_stream_json(root: Path) -> _ScenarioOutcome:
    fixture = _BenchmarkFixture(root)
    runtime = AgentRuntime(fixture.loop)
    usage = _UsageTrackerFactory()
    usage.attach(fixture.loop.bus)
    output = io.StringIO()
    rc = await run_stream_json_session(
        runtime,
        session_path=None,
        input_processor=None,
        usage=usage,
        redactor=SecretRedactor(),
        initial_prompt="explain src/a.py",
        input_stream=io.StringIO(""),
        output_stream=output,
        log=lambda _line: None,
    )
    rows = [
        json.loads(line) for line in output.getvalue().splitlines()
        if line.strip()
    ]
    assembled = [
        row for row in rows
        if row.get("type") == "context" and row.get("subtype") == "assembled"
    ]
    last = assembled[-1] if assembled else {}
    snapshot = last.get("snapshot") or {}
    ledger = last.get("ledger") or {}
    plan = fixture.loop.last_context_plan.prompt_plan
    checks = [
        _check(
            PromptBenchmarkMetric.STABLE_PREFIX_ISOLATED,
            bool(snapshot.get("stable_hash"))
            and snapshot.get("stable_hash") == ledger.get("stable_hash")
            and snapshot.get("stable_hash") == plan.stable_hash
            and snapshot.get("dynamic_hash") == plan.dynamic_hash,
            "stream-json carried one stable/dynamic context identity",
        ),
        _check(
            PromptBenchmarkMetric.PROVIDER_HASH_EXACT,
            bool(ledger.get("provider_request_digest"))
            and ledger.get("provider_request_digest")
            == fixture.provider.last_prepared.payload_digest
            and ledger.get("wire_fidelity") == "exact",
            "stream-json ledger matched the exact provider body",
        ),
    ]
    return _ScenarioOutcome(
        checks=checks,
        prompt_plan=plan,
        ledger=fixture.loop.last_context_cache_ledger,
        facts={
            "exit_code": rc,
            "jsonl_events": len(rows),
            "context_assembled_events": len(assembled),
        },
    )


async def _run_sdk(root: Path, *, timeout: float) -> _ScenarioOutcome:
    fixture = _BenchmarkFixture(root)
    source_root = Path(__file__).resolve().parents[2]
    options = KrelcodeOptions(
        workspace=fixture.root,
        mock=True,
        no_session=True,
        no_checkpoint=True,
        no_mcp=True,
        no_skills=True,
        max_steps=1,
        startup_timeout=timeout,
        close_timeout=min(10.0, timeout),
        env={
            "HOME": str(fixture.home),
            "PYTHONPATH": str(source_root),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    events = []
    async with KrelcodeClient(options=options) as client:
        async for message in client.query(
            "explain src/a.py", request_id="benchmark-sdk"
        ):
            events.append(message)
    assembled = [
        event for event in events
        if event.type == "context" and event.subtype == "assembled"
    ]
    last = assembled[-1] if assembled else None
    ledger = last.get("ledger") if last is not None else {}
    snapshot = last.get("snapshot") if last is not None else {}
    checks = [
        _check(
            PromptBenchmarkMetric.STABLE_PREFIX_ISOLATED,
            bool(assembled)
            and bool(snapshot.get("stable_hash"))
            and snapshot.get("stable_hash") == ledger.get("stable_hash")
            and snapshot.get("dynamic_hash") == ledger.get("dynamic_hash"),
            "SDK received a context event with matching stable/dynamic hashes",
        ),
    ]
    return _ScenarioOutcome(
        checks=checks,
        prompt_plan=None,
        ledger=ContextCacheLedger.from_dict(ledger) if ledger else None,
        facts={
            "sdk_events": len(events),
            "context_assembled_events": len(assembled),
            "protocol": (
                client.init_message.get("protocol")
                if client.init_message is not None else ""),
            "protocol_version": (
                client.init_message.get("protocol_version")
                if client.init_message is not None else ""),
        },
    )


async def _run_subagent(root: Path) -> _ScenarioOutcome:
    fixture = _BenchmarkFixture(root)
    parent = fixture.loop
    parent.history.extend([
        Message.user("parent-only-sentinel question"),
        Message.assistant("parent-only-sentinel answer"),
    ])
    await parent.run(
        "prepare parent policy for src/a.py delegation", turn_id="parent")
    from ..subagent.manager import SubagentManager

    manager = SubagentManager(
        llm=fixture.provider,
        tool_factory=lambda: [],
        tool_context=parent.tool_context,
        policy=AllowAllPolicy(),
        bus=EventBus(),
        memory_store=fixture.memory,
        cache_ledger_provider=lambda: parent.last_context_cache_ledger,
    )
    manager.get_parent_history = lambda: parent.history
    with patch.dict(os.environ, {"HOME": str(fixture.home)}):
        handle = await manager.spawn(
            "review src/a.py benchmark evidence", fork_context=True)
        handle = await manager.wait("a1", timeout=10)
    child = handle.loop
    plan = child.last_context_plan.prompt_plan
    ledger = child.last_context_cache_ledger
    inherited = [
        fragment for fragment in plan.fragments
        if fragment.id == "subagent:inherited-memory"
    ]
    history_isolated = (
        handle.status == "completed"
        and getattr(child, "parent_history_inherited", True) is False
        and all(
            "parent-only-sentinel" not in (message.content or "")
            for message in child.history
        )
    )
    memory_is_evidence = (
        bool(inherited)
        and all(
            fragment.layer == LAYER_L7_EVIDENCE
            and fragment.source == "inherited_memory"
            and fragment.provenance.get("source_role") == "evidence"
            and fragment.provenance.get("instruction") is False
            and fragment.provenance.get("requires_validation") is True
            and not fragment.stable
            for fragment in inherited
        )
    )
    checks = [
        _check(
            PromptBenchmarkMetric.SUBAGENT_HISTORY_ISOLATED,
            history_isolated,
            "subagent history contains only its own task messages",
        ),
        _check(
            PromptBenchmarkMetric.SUBAGENT_MEMORY_EVIDENCE,
            memory_is_evidence,
            "inherited subagent memory stayed L7 evidence",
        ),
        _check(
            PromptBenchmarkMetric.RULE_PRECEDENCE_STABLE,
            _rule_order(plan) == _RULE_ORDER,
            "subagent reused canonical rule fragment precedence",
        ),
        _check(
            PromptBenchmarkMetric.PROVIDER_HASH_EXACT,
            ledger is not None
            and _provider_hash_is_exact(
                plan, ledger, fixture.provider.last_prepared),
            "subagent ledger matched its exact provider body",
        ),
    ]
    await manager.shutdown()
    return _ScenarioOutcome(
        checks=checks,
        prompt_plan=plan,
        ledger=ledger,
        facts={
            "agent_status": handle.status,
            "parent_history_inherited": bool(
                getattr(child, "parent_history_inherited", True)),
            "inherited_memory_ids": list(
                getattr(child, "inherited_memory_ids", ())),
            "instruction_layers": [
                {
                    "kind": layer.kind,
                    "source": layer.source,
                    "content_hash": hashlib.sha256(
                        layer.content.encode("utf-8")).hexdigest(),
                }
                for layer in getattr(
                    child, "subagent_instruction_layers", ())
            ],
            "instruction_inheritance": (
                ledger.instruction_inheritance if ledger is not None else ""),
        },
    )


async def _run_resume(root: Path) -> _ScenarioOutcome:
    fixture = _BenchmarkFixture(root)
    loop = fixture.loop
    await loop.run("resume benchmark task", turn_id="resume-source")
    original_plan = loop.last_context_plan
    store = ContextCheckpointStore(fixture.root)
    checkpoint = store.record(
        history=loop.history,
        session_id="benchmark-source",
        turn_id="resume-source",
        file_hashes=hash_tracked_files((fixture.target,), fixture.root),
        workspace=WorkspaceFingerprint(
            branch="benchmark-old",
            head="old-head",
            captured_at=time.time(),
        ),
        memory_query="benchmark prompt evidence",
        context_plan=original_plan,
    )
    assert checkpoint is not None
    fixture.target.write_text(
        "def answer():\n    return 'changed after checkpoint'\n",
        encoding="utf-8",
    )
    resumed = fixture.make_loop()
    result = restore_context_checkpoint(
        resumed,
        checkpoint,
        memory_store=fixture.memory,
    )
    await resumed.refresh_workspace_context()
    restored_plan = resumed.last_context_plan
    restored_ledger = resumed.last_context_cache_ledger
    live_sections = {
        section.name for section in getattr(resumed.workspace_context, "sections", ())
    }
    checks = [
        _check(
            PromptBenchmarkMetric.STABLE_PREFIX_ISOLATED,
            restored_ledger is not None
            and _stable_prefix_is_isolated(
                original_plan.prompt_plan, restored_plan.prompt_plan)
            and result.context_plan.stable_hash
            == original_plan.stable_hash,
            "resume preserved the stable-prefix identity",
        ),
        _check(
            PromptBenchmarkMetric.PROVIDER_HASH_EXACT,
            restored_ledger is not None
            and bool(restored_ledger.provider_request_digest)
            and restored_ledger.provider_request_digest
            == original_plan.cache_ledger.provider_request_digest,
            "resume retained the source provider request identity",
        ),
        _check(
            PromptBenchmarkMetric.MEMORY_NOT_INSTRUCTION,
            _memory_is_evidence(restored_plan.prompt_plan),
            "resume memory remained in the evidence channel",
        ),
    ]
    checks.append(_check(
        PromptBenchmarkMetric.RULE_PRECEDENCE_STABLE,
        _rule_order(restored_plan.prompt_plan) == _RULE_ORDER,
        "resume preserved canonical rule precedence",
    ))
    checks.append(_check(
        PromptBenchmarkMetric.COMPACT_RECOVERABLE,
        restored_plan.matches_history(resumed.history)
        and bool(result.integrity.stale)
        and "workspace-refresh"
        in result.context_plan.cache_ledger.resume_invalidations,
        "resume restored history and invalidated stale workspace context",
    ))
    return _ScenarioOutcome(
        checks=checks,
        prompt_plan=restored_plan.prompt_plan,
        ledger=restored_ledger,
        facts={
            "checkpoint_id": checkpoint.id,
            "stale_files": len(result.integrity.stale),
            "memory_ids": list(result.memory_ids),
            "resume_invalidations": list(
                result.context_plan.cache_ledger.resume_invalidations),
            "restored_from": result.context_plan.cache_ledger.restored_from_checkpoint_id,
            "live_workspace_sections": sorted(live_sections),
            "old_git_state_present": "git" in live_sections,
        },
    )


async def _run_fork(root: Path) -> _ScenarioOutcome:
    fixture = _BenchmarkFixture(root)
    loop = fixture.loop
    source_session = "benchmark-source"
    fixture.memory.bind_session(source_session)
    source_memory = fixture.memory.add(
        "Session fact inherited by the fork.",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.FACT,
        source="benchmark",
    )
    await loop.run("fork source task", turn_id="fork-source")
    store = ContextCheckpointStore(fixture.root)
    checkpoint = store.record(
        history=loop.history,
        session_id=source_session,
        turn_id="fork-source",
        context_plan=loop.last_context_plan,
    )
    assert checkpoint is not None
    sessions = fixture.root / ".krelcode" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    source_path = sessions / f"{source_session}.jsonl"
    source_path.write_text("", encoding="utf-8")
    loop.context_checkpoint_store = store
    lifecycle = SessionLifecycle(
        loop,
        sessions,
        source_path,
        redactor=SecretRedactor(),
    )
    fork_path = lifecycle.fork(
        "benchmark-fork",
        history=loop.history,
        context_checkpoint_id=checkpoint.id,
    )
    fork_id = fork_path.stem
    copied = store.latest_for_session(fork_id)
    assert copied is not None
    fork_memory = fixture.memory.add(
        "Fork-only session fact.",
        scope=MemoryScope.SESSION,
        kind=MemoryKind.FACT,
        source="benchmark-fork",
    )
    source_store = MemoryStore(
        fixture.root,
        path=fixture.root / ".krelcode" / "memory" / "entries.jsonl",
        user_path=fixture.home / ".krelcode" / "memory" / "entries.jsonl",
        session_id=source_session,
    )
    source_session_ids = {
        entry.id for entry in source_store.list(
            limit=0, scopes=(MemoryScope.SESSION,))
    }
    fork_session_ids = {
        entry.id for entry in fixture.memory.list(
            limit=0, scopes=(MemoryScope.SESSION,))
    }
    restored_plan = loop.last_context_plan
    checks = [
        _check(
            PromptBenchmarkMetric.STABLE_PREFIX_ISOLATED,
            restored_plan is not None
            and bool(restored_plan.stable_hash)
            and not any(
                fragment.stable
                for fragment in restored_plan.prompt_plan.fragments
                if fragment.source == "inherited_memory"
            ),
            "fork restored a stable-prefix plan without memory pollution",
        ),
        _check(
            PromptBenchmarkMetric.MEMORY_NOT_INSTRUCTION,
            restored_plan is not None
            and _memory_is_evidence(restored_plan.prompt_plan),
            "fork memory remained evidence",
        ),
        _check(
            PromptBenchmarkMetric.COMPACT_RECOVERABLE,
            copied.copied_from == checkpoint.id
            and copied.session_id == fork_id
            and source_memory.id in fork_session_ids
            and fork_memory.id in fork_session_ids
            and fork_memory.id not in source_session_ids,
            "fork copied the context reference and isolated session memory",
        ),
    ]
    return _ScenarioOutcome(
        checks=checks,
        prompt_plan=restored_plan.prompt_plan if restored_plan else None,
        ledger=(
            restored_plan.cache_ledger if restored_plan is not None else None),
        facts={
            "source_checkpoint_id": checkpoint.id,
            "fork_checkpoint_id": copied.id,
            "copied_from": copied.copied_from,
            "fork_session_id": fork_id,
            "source_session_entries": len(source_session_ids),
            "fork_session_entries": len(fork_session_ids),
        },
    )


async def _run_rewind(root: Path) -> _ScenarioOutcome:
    fixture = _BenchmarkFixture(root)
    loop = fixture.loop
    store = ContextCheckpointStore(fixture.root)
    loop.context_checkpoint_store = store
    coordinator = ContextCheckpointCoordinator(loop=loop, store=store)
    coordinator.attach(loop.bus)
    loop.context_checkpoint_coordinator = coordinator
    controller = SessionController(AgentRuntime(loop))
    await loop.run("rewind first request", turn_id="rewind-1")
    before_second = list(loop.history)
    await loop.run("rewind second request", turn_id="rewind-2")
    result = controller.rewind.rewind(2)
    plan = loop.last_context_plan
    ledger = loop.last_context_cache_ledger
    checks = [
        _check(
            PromptBenchmarkMetric.COMPACT_RECOVERABLE,
            plan is not None
            and plan.matches_history(loop.history)
            and plan.restored_from == result.context_checkpoint_id
            and [message.content for message in loop.history]
            == [message.content for message in before_second],
            "rewind restored history, context plan and checkpoint together",
        ),
        _check(
            PromptBenchmarkMetric.STABLE_PREFIX_ISOLATED,
            ledger is not None
            and bool(ledger.stable_hash)
            and ledger.stable_hash == plan.stable_hash,
            "rewind restored the matching stable-prefix ledger",
        ),
        _check(
            PromptBenchmarkMetric.PROVIDER_HASH_EXACT,
            ledger is not None
            and bool(ledger.provider_request_digest)
            and ledger.wire_fidelity == "exact",
            "rewind retained an exact source-request digest",
        ),
    ]
    coordinator.detach()
    return _ScenarioOutcome(
        checks=checks,
        prompt_plan=plan.prompt_plan if plan is not None else None,
        ledger=ledger,
        facts={
            "target_turn_id": result.target_turn_id,
            "removed_turns": result.removed_turns,
            "context_checkpoint_id": result.context_checkpoint_id,
        },
    )


def _compact_summary_json() -> str:
    return json.dumps({
        "goal": "Preserve a recoverable compact benchmark scene",
        "completed": ["implemented the benchmark harness"],
        "decisions": ["keep memory as evidence"],
        "files": [
            {"path": "src/a.py", "status": "read", "note": "benchmark target"}
        ],
        "tests": [
            {"name": "prompt benchmark", "status": "passed", "detail": "offline"}
        ],
        "open_issues": [],
        "next_steps": ["run the full test suite"],
        "critical_constraints": ["do not promote memory to instruction"],
    }, ensure_ascii=False)


async def _run_compact(root: Path) -> _ScenarioOutcome:
    provider = _BenchmarkProvider(responses=[_compact_summary_json()])
    compact_store = CompactCheckpointStore(root)
    manager = ContextManager(
        provider,
        max_tokens=600,
        keep_recent=2,
        low_watermark_ratio=0.45,
    )
    fixture = _BenchmarkFixture(
        root, provider=provider, context_manager=manager,
        compact_store=compact_store,
    )
    loop = fixture.loop
    for index in range(8):
        loop.history.append(Message.user(
            f"long benchmark request {index}: " + "u" * 120))
        loop.history.append(Message.assistant(
            f"long benchmark answer {index}: " + "a" * 120))
    manager.rebuild_state(loop.history)
    pre_compact = list(loop.history)
    changed = await loop.compress_context(force=True)
    checkpoint = loop.last_compact_checkpoint
    assert changed and checkpoint is not None
    # The benchmark compacts a deliberately tiny budget, then verifies that a
    # normal post-compact turn inherits the checkpoint without immediately
    # compacting the same summary again.
    manager.max_tokens = 4_096
    await loop.run("continue after compact", turn_id="compact-next")
    plan = loop.last_context_plan.prompt_plan
    ledger = loop.last_context_cache_ledger
    context_store = ContextCheckpointStore(fixture.root)
    restore_point = context_store.record(
        history=pre_compact,
        session_id="benchmark-source",
        turn_id="compact-before",
        compact_checkpoint_id=checkpoint.id,
        compact_summary=checkpoint.summary.as_dict(),
        context_plan=loop.last_context_plan,
    )
    assert restore_point is not None
    restored_manager = ContextManager(
        provider, max_tokens=600, keep_recent=2, low_watermark_ratio=0.45)
    restored_loop = fixture.make_loop(context_manager=restored_manager)
    restored = restore_context_checkpoint(
        restored_loop,
        restore_point,
        compact_store=compact_store,
        memory_store=fixture.memory,
    )
    summary = checkpoint.summary
    checks = [
        _check(
            PromptBenchmarkMetric.COMPACT_RECOVERABLE,
            summary.goal
            and any(item.path == "src/a.py" for item in summary.files)
            and any(item.status == "passed" for item in summary.tests)
            and "run the full test suite" in summary.next_steps
            and restored_loop.last_compact_checkpoint is not None
            and restored_loop.last_compact_checkpoint.id == checkpoint.id,
            "compact summary and checkpoint restored the full handoff scene",
        ),
        _check(
            PromptBenchmarkMetric.STABLE_PREFIX_ISOLATED,
            bool(plan.stable_hash)
            and bool(ledger and ledger.compact_checkpoint_id)
            and ledger.compact_checkpoint_id == checkpoint.id
            and bool(plan.history_digest)
            and plan.history_digest == ledger.history_digest
            and plan.turn_id == "compact-next",
            "post-compact request kept a stable prefix tied to its request ledger",
        ),
        _check(
            PromptBenchmarkMetric.MEMORY_NOT_INSTRUCTION,
            _memory_is_evidence(plan),
            "post-compact request kept memory in the evidence channel",
        ),
        _check(
            PromptBenchmarkMetric.PROVIDER_HASH_EXACT,
            ledger is not None
            and _provider_hash_is_exact(
                plan, ledger, provider.last_prepared),
            "post-compact provider request matched the ledger digest",
        ),
    ]
    return _ScenarioOutcome(
        checks=checks,
        prompt_plan=plan,
        ledger=ledger,
        facts={
            "compact_checkpoint_id": checkpoint.id,
            "compact_summary_digest": summary.digest,
            "before_tokens": checkpoint.before_tokens,
            "after_tokens": checkpoint.after_tokens,
            "pre_compact_messages": len(pre_compact),
            "restored_history_messages": len(restored_loop.history),
            "restored_checkpoint_id": restored.checkpoint_id,
            "compact_source": summary.source,
        },
    )


class _UsageTrackerFactory:
    """Small late-binding helper so importing benchmarks stays side-effect free."""

    def __new__(cls):
        from ..agent.usage import UsageTracker

        return UsageTracker()


def _safe_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """Keep report facts JSON-safe and never stringify opaque prompt objects."""
    result: dict[str, object] = {}
    for key, item in value.items():
        result[str(key)[:120]] = _safe_report_value(item)
    return result


def _safe_report_value(value: object, *, depth: int = 0) -> object:
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if depth >= 4:
        return {"type": type(value).__name__, "truncated": True}
    if isinstance(value, Mapping):
        return {
            str(key)[:100]: _safe_report_value(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [
            _safe_report_value(item, depth=depth + 1)
            for item in value[:100]
        ]
    # Opaque objects may carry Prompt, Memory or tool text.  Keep only their
    # type instead of calling str(), which could silently leak content.
    return {"type": type(value).__name__}


def _safe_error(error: BaseException) -> str:
    message = " ".join(str(error).split())
    return f"{type(error).__name__}: {message[:300]}"


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


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
            "Run the content-light Krelcode Prompt/Context benchmark suite."
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
    args = parser.parse_args(list(argv) if argv is not None else None)
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


if __name__ == "__main__":
    raise SystemExit(main())
