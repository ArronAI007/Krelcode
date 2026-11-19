"""P2: content-light Prompt Plan observability."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from krelcode.agent.context_plan import ContextPlanSnapshot
from krelcode.agent.fragments import (
    LAYER_L1_BASE_AGENT,
    LAYER_L7_MEMORY,
    LAYER_L8_ENVIRONMENT,
    STABILITY_STATIC,
    STABILITY_TURN,
    PromptFragment,
    PromptFragmentRegistry,
    base_fragment,
)
from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompt_plan import (
    PromptOmissionSnapshot,
    PromptPlanSnapshot,
    tool_catalog_hash,
)
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.controller import SessionController
from krelcode.session.memory import MemoryKind, MemoryScope, MemoryStore
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.glob import GlobTool
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.registry import ToolRegistry


def _fragment(
    fragment_id: str,
    content: str,
    *,
    source: str = "test",
    scope: str = "project",
    layer: str = LAYER_L1_BASE_AGENT,
    stability: str = STABILITY_STATIC,
    metadata=None,
):
    return SimpleNamespace(
        id=fragment_id,
        source=source,
        scope=scope,
        layer=layer,
        stability=stability,
        trust="project",
        tokens=max(1, len(content) // 4),
        cacheable=stability == STABILITY_STATIC,
        content=content,
        metadata=metadata or {},
    )


def _fake_plan(*fragments, stable=("core:base",), omitted=()):
    return SimpleNamespace(
        fragments=tuple(fragments),
        cache=SimpleNamespace(
            offset=128,
            stable_fragments=tuple(stable),
            stable_hash="stable-hash",
            dynamic_hash="dynamic-hash",
        ),
        engine="v3",
        omitted_details=tuple(omitted),
    )


def _tool(name: str, marker: str) -> dict:
    return {
        "name": name,
        "description": marker,
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    }


def test_prompt_plan_snapshot_is_content_light_and_order_stable():
    memory_id = "memory-project-1"
    memory_body = "MEMORY BODY MUST NOT BE PERSISTED"
    prompt_body = "PROMPT BODY MUST NOT BE PERSISTED"
    memory = _fragment(
        "memory:scoped",
        memory_body,
        source="memory",
        scope="project",
        layer=LAYER_L7_MEMORY,
        stability=STABILITY_TURN,
        metadata={
            "memory_hits": ({
                "id": memory_id,
                "source": "memory",
                "scope": "project",
                "kind": "knowledge",
                "origin": "reviewed",
                "tokens": 12,
            },),
        },
    )
    base = _fragment("core:base", prompt_body)
    omission = PromptOmissionSnapshot(
        id="workspace:tree",
        source="workspace",
        scope="project",
        layer="L8_ENVIRONMENT_DATE_SESSION",
        stability=STABILITY_TURN,
        trust="project",
        tokens=44,
        reason="file-explanation-scope",
    )

    snapshot = PromptPlanSnapshot.from_prompt_plan(
        _fake_plan(base, memory, omitted=(omission,)),
        turn_id="turn-p2",
        step=2,
        history_digest="history-hash",
        tools=(_tool("write_file", "WRITE"), _tool("read_file", "READ")),
    )

    assert [item.id for item in snapshot.stable_fragments] == ["core:base"]
    assert [item.id for item in snapshot.volatile_fragments] == ["memory:scoped"]
    assert snapshot.stable_tokens == base.tokens
    assert snapshot.volatile_tokens == memory.tokens
    assert snapshot.cache_boundary_chars == 128
    assert snapshot.tool_count == 2
    assert snapshot.tool_catalog_hash
    assert snapshot.omitted[0].reason == "file-explanation-scope"
    assert snapshot.memory_hits[0].id == memory_id
    assert snapshot.memory_hits[0].scope == "project"

    serialized = json.dumps(snapshot.as_dict(), ensure_ascii=False)
    assert prompt_body not in serialized
    assert memory_body not in serialized
    assert "WRITE" not in serialized and "READ" not in serialized
    assert PromptPlanSnapshot.from_dict(snapshot.as_dict()) == snapshot

    reversed_hash = tool_catalog_hash((
        _tool("read_file", "READ"), _tool("write_file", "WRITE")))
    assert reversed_hash == snapshot.tool_catalog_hash
    assert tool_catalog_hash((
        _tool("read_file", "CHANGED"), _tool("write_file", "WRITE"),
    )) != snapshot.tool_catalog_hash


def test_agent_loop_records_real_prompt_diff_and_memory_source(tmp_path):
    memory = MemoryStore(
        tmp_path,
        user_path=tmp_path / "user-memory.jsonl",
    )
    entry = memory.add(
        "Project tests use pytest.",
        scope=MemoryScope.PROJECT,
        kind=MemoryKind.KNOWLEDGE,
        source="reviewed-memory",
    )
    registry = PromptFragmentRegistry([
        base_fragment(),
        PromptFragment(
            id="project:extra",
            source="project",
            title="Project extra",
            content="PROJECT RUNTIME CONTEXT",
            layer=LAYER_L8_ENVIRONMENT,
            stability=STABILITY_TURN,
            trust="project",
            scope="project",
        ),
        PromptFragment(
            id="memory:stale",
            source="memory",
            title="Stale memory registry entry",
            content="STALE MEMORY MUST BE FILTERED",
            layer=LAYER_L7_MEMORY,
            stability=STABILITY_TURN,
            trust="untrusted",
            scope="project",
        ),
    ])
    loop = AgentLoop(
        llm=MockProvider(scripted=["first", "second"]),
        registry=ToolRegistry([ReadFileTool(), GlobTool()]),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM",
    )
    loop.prompt_registry = registry
    loop.prompt_snapshot = registry.snapshot()
    loop.memory_store = memory

    asyncio.run(loop.run("first request", turn_id="p2-one"))
    first = loop.last_context_plan.prompt_plan
    assert first.tool_count == 2
    assert first.tool_catalog_hash
    assert any(hit.id == entry.id for hit in first.memory_hits)

    asyncio.run(loop.run("second request", turn_id="p2-two"))
    second = loop.last_context_plan.prompt_plan
    assert second.diff.available
    assert second.diff.previous_turn_id == "p2-one"
    assert second.diff.history_changed
    assert second.diff.full_changed
    assert second.diff.changed or second.diff.added_fragments

    controller = SessionController(loop, workspace_root=tmp_path)
    plan_output = "\n".join(asyncio.run(
        controller.execute_command("/context plan")).output)
    cache_output = "\n".join(asyncio.run(
        controller.execute_command("/context cache")).output)
    why_output = "\n".join(asyncio.run(
        controller.execute_command(f"/context why {entry.id}")).output)
    omitted_output = "\n".join(asyncio.run(
        controller.execute_command("/context why memory:stale")).output)

    assert "prompt engine v3" in plan_output
    assert "stable" in plan_output and "volatile" in plan_output
    assert "stable sources:" in plan_output and "volatile sources:" in plan_output
    assert "[omitted/" in plan_output
    assert f"[memory] {entry.id}" in plan_output
    assert "source=memory" in plan_output and "kind=knowledge" in plan_output
    assert "origin=reviewed-memory" in plan_output
    assert "diff ← p2-one" in plan_output
    assert "tool catalog" in cache_output
    assert "changed:" in cache_output
    assert f"[context why] {entry.id}" in why_output
    assert "scope=project" in why_output
    assert "Project tests use pytest." not in why_output
    assert "memory-evidence-separation" in omitted_output
    assert "未计入 provider 请求正文" in omitted_output
    assert "STALE MEMORY MUST BE FILTERED" not in omitted_output

    persisted = loop.last_context_plan.as_dict()
    serialized = json.dumps(persisted, ensure_ascii=False)
    assert "Project tests use pytest." not in serialized
    assert "PROJECT RUNTIME CONTEXT" not in serialized
    assert "first request" not in serialized
    assert "second request" not in serialized


def test_context_plan_roundtrips_prompt_observability():
    prompt_plan = PromptPlanSnapshot(
        turn_id="turn-roundtrip",
        step=1,
        engine="v3",
        stable_tokens=30,
        volatile_tokens=12,
        fragment_tokens=42,
        cache_boundary_chars=100,
        stable_hash="stable",
        dynamic_hash="dynamic",
        full_hash="full",
        history_digest="history",
        tool_catalog_hash="tools",
        tool_count=3,
        omitted=(PromptOmissionSnapshot(
            id="workspace:tree", source="workspace", tokens=9, reason="budget"),),
    )
    plan = ContextPlanSnapshot(
        turn_id="turn-roundtrip",
        step=1,
        stable_hash="stable",
        dynamic_hash="dynamic",
        full_hash="full",
        prompt_plan=prompt_plan,
    )

    restored = ContextPlanSnapshot.from_dict(plan.as_dict())

    assert restored == plan
    assert restored.prompt_plan.tool_catalog_hash == "tools"
    assert restored.prompt_plan.omitted[0].reason == "budget"
