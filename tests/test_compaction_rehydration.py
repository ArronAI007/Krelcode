"""Post-compaction file and skill rehydration behavior."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from krelcode.agent.context.compression import ContextManager
from krelcode.agent.context.rehydration import (
    CompactionRehydrationPolicy,
    CompactionRehydrator,
)
from krelcode.agent.context.working_context import WorkingContextStore
from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.turn.query import QueryEvent
from krelcode.core.events import CompactionRehydrated, EventBus
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.compact_checkpoint import CompactCheckpointStore
from krelcode.session.stream_json import StreamJsonEncoder, StreamJsonMetadata
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def _summary_json() -> str:
    return json.dumps({
        "goal": "continue after compact",
        "completed": ["implemented change"],
        "decisions": [],
        "files": [{
            "path": "recent.py",
            "status": "modified",
            "note": "current change",
        }],
        "tests": [],
        "open_issues": [],
        "next_steps": ["verify"],
        "critical_constraints": [],
    })


def _tool_history(recent: Path, *, skill_body: str) -> list[Message]:
    return [
        Message.system("SYS"),
        Message.assistant(tool_calls=[
            ToolCall("write-1", "write_file", {"path": "recent.py"}),
        ]),
        Message.tool_result(
            "write-1", "written", tool_name="write_file", is_error=False),
        Message.assistant(tool_calls=[
            ToolCall("skill-1", "load_skill", {
                "name": "review",
                "arguments": "recent.py",
            }),
        ]),
        Message.tool_result(
            "skill-1", skill_body, tool_name="load_skill", is_error=False),
        Message.user("continue"),
        Message.assistant("working"),
    ]


def test_rehydrator_restores_recent_file_and_loaded_skill(
    tmp_path: Path,
) -> None:
    recent = tmp_path / "recent.py"
    recent.write_text("CURRENT_FILE_CONTENT = True\n", encoding="utf-8")
    rehydrator = CompactionRehydrator(
        tmp_path,
        policy=CompactionRehydrationPolicy(
            max_files=1,
            max_file_tokens=100,
            max_skills=1,
            max_skill_tokens=100,
        ),
    )
    bundle = rehydrator.rehydrate(
        history=_tool_history(recent, skill_body="SKILL_BODY_V1"),
        working_context=WorkingContextStore(tmp_path).snapshot(),
        trigger="auto",
        generation=1,
    )

    assert bundle.report.files[0].path == "recent.py"
    assert bundle.report.skills[0].name == "review"
    assert "CURRENT_FILE_CONTENT = True" in bundle.text
    assert "SKILL_BODY_V1" in bundle.text
    assert bundle.report.total_tokens > 0


def test_rehydrator_drops_oldest_skill_when_total_budget_is_exceeded(
    tmp_path: Path,
) -> None:
    history = [
        Message.assistant(tool_calls=[
            ToolCall("old", "load_skill", {"name": "old-skill"}),
            ToolCall("new", "load_skill", {"name": "new-skill"}),
        ]),
        Message.tool_result(
            "old", "OLD " * 200, tool_name="load_skill", is_error=False),
        Message.tool_result(
            "new", "NEW " * 200, tool_name="load_skill", is_error=False),
    ]
    bundle = CompactionRehydrator(
        tmp_path,
        policy=CompactionRehydrationPolicy(
            max_files=0,
            max_skill_tokens=10,
            max_total_skill_tokens=10,
        ),
    ).rehydrate(
        history=history,
        working_context=WorkingContextStore(tmp_path).snapshot(),
        trigger="auto",
        generation=2,
    )
    assert [item.name for item in bundle.report.skills] == ["new-skill"]
    assert any(
        item["name"] == "old-skill" for item in bundle.report.omitted_skills
    )


def test_agent_loop_rehydrates_and_persists_report(tmp_path: Path) -> None:
    recent = tmp_path / "recent.py"
    recent.write_text("AFTER_COMPACT = 1\n", encoding="utf-8")
    provider = MockProvider(scripted=[_summary_json()])
    bus = EventBus()
    checkpoint_store = CompactCheckpointStore(tmp_path)
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
        context_manager=ContextManager(
            provider, max_tokens=10**9, keep_recent=2),
        compact_checkpoint_store=checkpoint_store,
    )
    loop.history = _tool_history(recent, skill_body="REVIEW_SKILL_BODY")
    events = []
    bus.subscribe(events.append)

    assert asyncio.run(loop.compress_context(force=True))

    report = loop.last_compaction_rehydration
    assert report is not None
    assert [item.path for item in report.files] == ["recent.py"]
    assert [item.name for item in report.skills] == ["review"]
    assert any(
        message.role == "user"
        and "[Compaction Rehydration]" in (message.content or "")
        for message in loop.history
    )
    rehydrated = next(
        event for event in events if isinstance(event, CompactionRehydrated))
    assert rehydrated.report_digest == report.digest
    checkpoint = loop.last_compact_checkpoint
    assert checkpoint is not None
    stored = checkpoint_store.load(checkpoint.id)
    assert stored is not None
    assert stored.rehydration["files"][0]["path"] == "recent.py"

    payload = StreamJsonEncoder(StreamJsonMetadata(
        session_id="session-1",
        cwd=str(tmp_path),
        provider="mock",
        model="mock-model",
    )).encode(QueryEvent("turn-1", 1, rehydrated))
    assert payload["type"] == "compact"
    assert payload["subtype"] == "rehydrated"
    assert payload["files"] == ["recent.py"]
