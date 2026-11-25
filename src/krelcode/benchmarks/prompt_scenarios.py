"""Deterministic Prompt/Context scenarios over every production run mode."""

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
from pathlib import Path
from typing import Mapping, Sequence
from unittest.mock import patch

from ..agent.context.compression import ContextManager
from ..agent.context import ContextOptions, ContextSection, WorkspaceContext
from ..agent.context.context_assembler import ContextAssembler
from ..agent.context.context_cache_ledger import ContextCacheLedger
from ..agent.context.context_plan import ContextPlanSnapshot
from ..agent.context.context_source import default_context_sources
from ..agent.prompt.cyber_risk import cyber_risk_fragment
from ..agent.prompt.fragments import LAYER_L7_EVIDENCE, PromptFragmentRegistry, base_fragment, sync_rule_fragments
from ..agent.turn.loop import AgentLoop
from ..agent.prompt.prompt_plan import PromptPlanSnapshot
from ..agent.rules import RuleLoader
from ..agent.turn.runtime import AgentRuntime
from ..agent.usage import UsageTracker
from ..core.tokens import estimate_text_tokens
from ..agent.context.working_context import WorkingContextStore
from ..core.events import EventBus
from ..core.messages import Message
from ..core.redact import SecretRedactor
from ..llm.base import (
    EvidenceChannel,
    LLMEvent,
    PreparedLLMRequest,
    ProviderCapabilities,
    StreamFinished,
    TextDelta,
    resolve_evidence_channel,
    resolve_system_prompt_blocks,
)
from ..safety.policy import AllowAllPolicy
from ..sdk import KrelcodeClient, KrelcodeOptions
from ..session.compact_checkpoint import CompactCheckpointStore
from ..agent.context.context_checkpoint import ContextCheckpointCoordinator, ContextCheckpointStore, WorkspaceFingerprint, hash_tracked_files, restore_context_checkpoint
from ..session.controller import SessionController
from ..session.lifecycle import SessionLifecycle
from ..session.memory import MemoryStore
from ..session.memory_models import MemoryKind, MemoryScope
from ..session.runner import run_session
from ..session.runtime_ports import SessionRuntimePorts
from ..session.stream_json_runner import run_stream_json_session
from ..contracts.tool import ToolContext
from ..tools.registry import ToolRegistry
from .prompt_fixture import _BenchmarkFixture, _BenchmarkProvider
from .prompt_helpers import (
    _check,
    _elapsed_ms,
    _memory_fragments,
    _memory_is_evidence,
    _provider_hash_is_exact,
    _rule_order,
    _RULE_ORDER,
    _rule_order_is_stable,
    _safe_error,
    _scope_filtered,
    _stable_prefix_is_isolated,
)
from .prompt_models import (
    PromptBenchmarkCheck,
    PromptBenchmarkMetric,
    _ScenarioOutcome,
)


def _require_context_plan(loop: AgentLoop) -> ContextPlanSnapshot:
    plan = loop.last_context_plan
    if plan is None:
        raise AssertionError("benchmark turn did not produce a context plan")
    return plan


def _require_ledger(plan: ContextPlanSnapshot) -> ContextCacheLedger:
    ledger = plan.cache_ledger
    if ledger is None:
        raise AssertionError("benchmark context plan has no cache ledger")
    return ledger


def _require_prepared(provider: _BenchmarkProvider) -> PreparedLLMRequest:
    prepared = provider.last_prepared
    if prepared is None:
        raise AssertionError("benchmark provider has no prepared request")
    return prepared


async def _run_main(root: Path) -> _ScenarioOutcome:
    fixture = _BenchmarkFixture(root)
    loop = fixture.loop
    prompt = "请解释 `src/a.py`"
    await loop.run(prompt, turn_id="benchmark-main-1")
    first_plan = _require_context_plan(loop).prompt_plan
    fixture.memory.add(
        "A second benchmark evidence item must never become an instruction.",
        scope=MemoryScope.PROJECT,
        kind=MemoryKind.KNOWLEDGE,
        source="benchmark-followup",
    )
    await loop.run(prompt, turn_id="benchmark-main-2")
    second_plan = _require_context_plan(loop).prompt_plan
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
                second_plan, second_ledger, _require_prepared(fixture.provider)),
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
            argparse.Namespace(
                message="explain src/a.py",
                output_format="text",
                resume=None,
                no_session=True,
            ),
            runtime,
            SessionRuntimePorts(),
            usage=_UsageTrackerFactory(),
            model_switch=None,
            skills_reload=None,
            redactor=SecretRedactor(),
            log=lambda _line: None,
        )
    plan = _require_context_plan(fixture.loop).prompt_plan
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
                plan, ledger, _require_prepared(fixture.provider)),
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
    plan = _require_context_plan(fixture.loop).prompt_plan
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
            == _require_prepared(fixture.provider).payload_digest
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
    plan = _require_context_plan(child).prompt_plan
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
                plan, ledger, _require_prepared(fixture.provider)),
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
    original_plan = _require_context_plan(loop)
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
    restored_plan = _require_context_plan(resumed)
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
            == _require_ledger(original_plan).provider_request_digest,
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
        in _require_ledger(result.context_plan).resume_invalidations,
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
                _require_ledger(result.context_plan).resume_invalidations),
            "restored_from": _require_ledger(
                result.context_plan).restored_from_checkpoint_id,
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
        context_plan=_require_context_plan(loop),
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
    restored_plan = _require_context_plan(loop)
    checks = [
        _check(
            PromptBenchmarkMetric.STABLE_PREFIX_ISOLATED,
            bool(restored_plan.stable_hash)
            and not any(
                fragment.stable
                for fragment in restored_plan.prompt_plan.fragments
                if fragment.source == "inherited_memory"
            ),
            "fork restored a stable-prefix plan without memory pollution",
        ),
        _check(
            PromptBenchmarkMetric.MEMORY_NOT_INSTRUCTION,
            _memory_is_evidence(restored_plan.prompt_plan),
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
        prompt_plan=restored_plan.prompt_plan,
        ledger=restored_plan.cache_ledger,
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
    plan = _require_context_plan(loop)
    ledger = _require_ledger(_require_context_plan(loop))
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
    plan = _require_context_plan(loop).prompt_plan
    ledger = _require_ledger(_require_context_plan(loop))
    context_store = ContextCheckpointStore(fixture.root)
    restore_point = context_store.record(
        history=pre_compact,
        session_id="benchmark-source",
        turn_id="compact-before",
        compact_checkpoint_id=checkpoint.id,
        compact_summary=checkpoint.summary.as_dict(),
        context_plan=_require_context_plan(loop),
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
            bool(summary.goal)
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
                plan, ledger, _require_prepared(provider)),
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


def _UsageTrackerFactory() -> UsageTracker:
    """Small factory retained as a monkeypatch seam for compatibility."""
    return UsageTracker()
