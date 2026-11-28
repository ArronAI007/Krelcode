"""Context manifests, asset scopes and call-level observability."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from krelcode.agent.context.context_assembler import ContextAssembler
from krelcode.agent.context.context_contracts import ContextLayer
from krelcode.agent.context.context_plan import ContextPlanSnapshot
from krelcode.agent.context.context_policy import ContextBudgetPolicy
from krelcode.agent.context.context_request import ContextRequest
from krelcode.agent.turn.loop import AgentLoop
from krelcode.agent.turn.query import QueryEngine
from krelcode.contracts.application import ApplicationRequestFactory
from krelcode.contracts.asset import (
    AssetDeletionMode,
    AssetEnvelope,
    AssetScope,
)
from krelcode.contracts.knowledge import KnowledgeQuery
from krelcode.governance.asset_registry import AssetDependencyGraph
from krelcode.knowledge import LocalKnowledgeBase
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.memory import MemoryStore
from krelcode.skills import SkillManager
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


class _StaticSource:
    name = "static"

    def __init__(self, layer: ContextLayer) -> None:
        self.layer = layer

    def collect(self, request: ContextRequest) -> tuple[ContextLayer, ...]:
        return (self.layer,)


def test_context_plan_records_per_source_manifest() -> None:
    layer = ContextLayer(
        id="knowledge:1",
        source="knowledge",
        source_type="knowledge",
        source_id="kb:runbook",
        scope="project",
        version="7",
        category="knowledge",
        content="rollback requires snapshot",
        trust="untrusted",
        origin="kb",
        permission_basis="project-member",
        selected_reason="task relevance",
        transform="retrieved_evidence",
    )
    assembler = ContextAssembler([_StaticSource(layer)])
    request = ContextRequest(
        turn_id="turn-1",
        step=1,
        history=(),
        workspace_root=Path("."),
    )
    assembly = asyncio.run(assembler.assemble(request))
    plan = ContextPlanSnapshot.from_assembly(
        assembly, history=(), turn_id="turn-1", step=1)

    record = plan.source_records[0]
    assert record.source_id == "kb:runbook"
    assert record.scope == "project"
    assert record.version == "7"
    assert record.permission_basis == "project-member"
    assert record.transform == "retrieved_evidence"
    assert record.content_hash == layer.content_hash


def test_context_budget_policy_omits_optional_layers_explainably() -> None:
    system = ContextLayer(
        id="system",
        source="system",
        category="system",
        content="rule " * 100,
    )
    memory = ContextLayer(
        id="memory",
        source="memory",
        category="memory",
        content="memory " * 100,
    )
    policy = ContextBudgetPolicy(
        max_tokens=20,
        category_caps={
            "system": 0,
            "workspace": 0,
            "knowledge": 0,
            "memory": 0,
            "runtime": 0,
        },
    )
    selected, decisions = policy.apply((system, memory))
    assert [item.id for item in selected] == ["system"]
    assert decisions[1].selected is False
    assert decisions[1].reason.startswith("total-cap:")


def test_knowledge_retrieval_filters_principal_and_tenant(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".krelcode" / "knowledge"
    root.mkdir(parents=True)
    document = root / "release.md"
    document.write_text("# Release\n\nRollback requires a snapshot.", encoding="utf-8")
    document.with_suffix(".md.meta.json").write_text(json.dumps({
        "acl": ["alice"],
        "tenant_id": "tenant-a",
        "owner": "release-team",
        "authority_level": "authoritative",
    }), encoding="utf-8")
    knowledge = LocalKnowledgeBase(tmp_path, home=tmp_path / "home")

    denied = knowledge.search(KnowledgeQuery(
        query="rollback",
        principal="bob",
        tenant_id="tenant-a",
    ))
    assert not denied.hits

    allowed = knowledge.search(KnowledgeQuery(
        query="rollback",
        principal="alice",
        tenant_id="tenant-a",
    ))
    assert allowed.hits
    assert allowed.hits[0].document.owner == "release-team"
    assert allowed.hits[0].document.authority_level == "authoritative"


def test_memory_identity_filter_and_outcome_feedback(tmp_path: Path) -> None:
    store = MemoryStore(
        tmp_path,
        user_path=tmp_path / "home" / "entries.jsonl",
    )
    entry = store.add(
        "Use pytest for verification",
        metadata={"tenant_id": "tenant-a", "confidence": 0.8},
    )
    hidden, entries, _topics = store.index_view(
        tenant_id="tenant-b",
        project_id=str(tmp_path),
    )
    assert hidden == ""
    assert entries == ()

    visible, entries, _topics = store.index_view(
        tenant_id="tenant-a",
        project_id=str(tmp_path),
    )
    assert visible
    assert entries[0].id == entry.id
    updated = store.record_use(entry.id, success=True, scope=entry.scope)
    assert updated is not None
    assert updated.use_count == 1
    assert updated.success_count == 1
    assert updated.last_outcome == "success"
    assert updated.last_verified_at
    derived = store.add(
        "Project-specific pytest convention",
        metadata={
            "tenant_id": "tenant-a",
            "derived_from": [updated.id],
        },
    )
    plan = store.deletion_plan(
        updated.id, mode=AssetDeletionMode.CASCADE)
    assert updated.id in plan.affected_asset_ids
    assert derived.id in plan.affected_asset_ids


def test_skill_manifest_tracks_dependencies_tests_and_release_readiness(
    tmp_path: Path,
) -> None:
    root = tmp_path / "skills"
    skill_dir = root / "upgrade"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: upgrade\n"
        "version: 3.0.0\n"
        "description: Upgrade dependencies\n"
        "owner: platform-team\n"
        "applicability: dependency migrations\n"
        "required-tools: read_file,run_command\n"
        "acceptance-contract: tests pass\n"
        "dependencies: policy-pack\n"
        "tests: upgrade-fixture\n"
        "---\n"
        "Run the upgrade checklist.\n",
        encoding="utf-8",
    )
    manager = SkillManager(
        tmp_path, extra_paths=[root], home=tmp_path / "home")
    skill = manager.get("upgrade")
    assert skill is not None
    assert skill.owner == "platform-team"
    assert skill.required_tools == ("read_file", "run_command")
    assert skill.acceptance_contract == ("tests pass",)
    assert skill.dependencies == ("policy-pack",)
    assert skill.tests == ("upgrade-fixture",)
    assert skill.release_ready is True


def test_query_session_exposes_call_context(tmp_path: Path) -> None:
    loop = AgentLoop(
        llm=MockProvider(scripted=["done"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    engine = QueryEngine(loop)

    async def scenario() -> None:
        query = engine.start(
            "hello",
            application_request=ApplicationRequestFactory.from_input_source(
                "sdk", goal="hello"),
            call_id="call-123",
        )
        assert query.call_id == "call-123"
        async for item in query.events:
            assert item.call_id == "call-123"
        assert await query.wait() == "done"

    asyncio.run(scenario())


def test_asset_dependency_graph_plans_recursive_deletion() -> None:
    source = AssetEnvelope(
        asset_id="knowledge:runbook",
        asset_type="knowledge",
        scope=AssetScope.PROJECT,
    )
    memory = AssetEnvelope(
        asset_id="memory:1",
        asset_type="memory",
        scope=AssetScope.PROJECT,
        derived_from=(source.asset_id,),
    )
    skill = AssetEnvelope(
        asset_id="skill:upgrade",
        asset_type="skill",
        scope=AssetScope.PROJECT,
        derived_from=(memory.asset_id,),
    )
    graph = AssetDependencyGraph((source, memory, skill))
    plan = graph.plan_delete(
        source.asset_id, mode=AssetDeletionMode.CASCADE)
    assert plan.affected_asset_ids == (
        "knowledge:runbook", "memory:1", "skill:upgrade")
    assert plan.mode is AssetDeletionMode.CASCADE
