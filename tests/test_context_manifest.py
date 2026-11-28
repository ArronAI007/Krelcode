"""Per-model-call Context Manifest generation and replay."""

from __future__ import annotations

import asyncio
from pathlib import Path

from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.turn.query import QueryEvent
from krelcode.core.events import ContextManifestCreated, EventBus
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.stream_json import StreamJsonEncoder, StreamJsonMetadata
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.registry import ToolRegistry


def test_each_model_call_has_a_distinct_context_manifest(tmp_path: Path) -> None:
    (tmp_path / "input.txt").write_text("body", encoding="utf-8")
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[
                ToolCall("read-1", "read_file", {"path": "input.txt"}),
            ]),
            "done",
        ]),
        registry=ToolRegistry([ReadFileTool()]),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYSTEM",
    )

    assert asyncio.run(loop.run("inspect", turn_id="turn-manifest")) == "done"
    manifests = [
        event for event in events if isinstance(event, ContextManifestCreated)
    ]
    assert len(manifests) == 2
    assert [item.manifest["model_call_id"] for item in manifests] == [
        "turn-manifest:model:1",
        "turn-manifest:model:2",
    ]
    first = manifests[0].manifest
    assert first["context_policy"]["policy_id"] == "krelcode-context"
    assert first["context_policy"]["digest"]
    assert first["sources"]
    assert any(
        source["source_type"] == "tool_schema"
        for source in first["sources"]
    )
    assert all(
        {
            "source_type", "source_id", "scope", "version",
            "trust_level", "permission_basis", "selected_reason",
            "token_count", "transform", "content_hash",
        }.issubset(source)
        for source in first["sources"]
    )
    assert first["total_tokens"] > 0
    assert first["digest"]
    assert "content" not in first
    assert all("content" not in source for source in first["sources"])

    encoder = StreamJsonEncoder(StreamJsonMetadata(
        session_id="session-1",
        cwd=str(tmp_path),
        provider="mock",
        model="mock-model",
    ))
    payload = encoder.encode(QueryEvent(
        "turn-manifest", 1, manifests[0]))
    assert payload["type"] == "context"
    assert payload["subtype"] == "manifest"
    assert payload["manifest"]["model_call_id"] == (
        "turn-manifest:model:1")


def test_context_manifest_persists_budget_omissions(tmp_path: Path) -> None:
    bus = EventBus()
    events = []
    bus.subscribe(events.append)
    loop = AgentLoop(
        llm=MockProvider(scripted=["done"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYSTEM",
    )
    loop.services.context.assembler.budget_policy = (
        loop.services.context.assembler.budget_policy.__class__(
            max_tokens=256,
            category_caps={
                "system": 0,
                "workspace": 0,
                "knowledge": 1,
                "memory": 1,
                "runtime": 1,
            },
        )
    )

    assert asyncio.run(loop.run("answer", turn_id="turn-budget")) == "done"
    manifest = next(
        event.manifest for event in events
        if isinstance(event, ContextManifestCreated)
    )
    assert "omitted" in manifest
    assert isinstance(manifest["omitted"], list)
