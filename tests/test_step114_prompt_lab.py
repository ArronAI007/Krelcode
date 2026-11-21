"""Step 114: the read-only Prompt engineering laboratory tool."""

from __future__ import annotations

import asyncio
import json

import pytest

from krelcode.agent.fragments import (
    GROUP_SYSTEM,
    LAYER_L0_MANAGED_POLICY,
    LAYER_L3_PROJECT_RULES,
    LAYER_L6_CATALOG,
    STABILITY_SESSION,
    TRUST_MANAGED,
    TRUST_PROJECT,
    PromptFragment,
    PromptFragmentRegistry,
    base_fragments,
)
from krelcode.agent.prompt_lab import PromptLabService
from krelcode.agent.tool_pool import TurnToolPool
from krelcode.agent.loop import AgentLoop
from krelcode.app import build_loop, build_parser
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.subagent.manager import SubagentManager
from krelcode.tools.base import (
    PermissionMode,
    RuntimeSnapshot,
    ToolContext,
    ToolResult,
)
from krelcode.tools.builtin.prompt_lab import PromptLabTool
from krelcode.tools.prompt import ToolPromptSpec
from krelcode.tools.registry import ToolRegistry


def _fragment(
    fragment_id: str,
    *,
    source: str,
    title: str,
    content: str,
    layer: str,
    trust: str,
) -> PromptFragment:
    return PromptFragment(
        id=fragment_id,
        source=source,
        title=title,
        content=content,
        group=GROUP_SYSTEM,
        scope="managed" if source == "managed" else "project",
        layer=layer,
        stability=STABILITY_SESSION,
        trust=trust,
        metadata={"instruction": True, "rule_scope": source},
    )


def _conflict_registry() -> PromptFragmentRegistry:
    registry = PromptFragmentRegistry(base_fragments())
    registry.register(_fragment(
        "managed:indent",
        source="managed",
        title="tabs",
        content="Use tabs for indentation.",
        layer=LAYER_L0_MANAGED_POLICY,
        trust=TRUST_MANAGED,
    ))
    registry.register(_fragment(
        "project:indent",
        source="project",
        title="spaces",
        content="Use spaces for indentation.",
        layer=LAYER_L3_PROJECT_RULES,
        trust=TRUST_PROJECT,
    ))
    return registry


def _valid_candidate_spec() -> dict:
    return {
        "objective": "Require every evidence claim to cite a repository path.",
        "target_fragment_id": "project:review-prompt",
        "target_layer": LAYER_L6_CATALOG,
        "items": ({
            "id": "project:review-prompt",
            "role": "instruction",
            "source": "project",
            "content": "Always cite the exact file path for evidence claims.",
            "scope": "project",
            "layer": LAYER_L6_CATALOG,
            "stability": STABILITY_SESSION,
            "cacheable": True,
        },),
        "permission_mode": "default",
        "tool_scope": "none",
        "validation_cases": (
            {"id": "success", "kind": "success",
             "metric": "assert response contains a cited path"},
            {"id": "regression", "kind": "regression",
             "metric": "assert exit_code == 0 for focused tests"},
            {"id": "security", "kind": "security",
             "metric": "assert output contains no credential-shaped text"},
        ),
        "success_metrics": (
            "assert every cited path exists",
        ),
        "non_goals": (
            "Do not change tool permissions.",
        ),
    }


def _tool_context(registry: PromptFragmentRegistry, tmp_path) -> ToolContext:
    return ToolContext(
        workspace_root=tmp_path,
        sandbox_enabled=False,
        prompt_registry=registry,
    )


def test_prompt_lab_has_a_canonical_read_only_core_spec():
    tool = PromptLabTool()
    spec = ToolPromptSpec.from_tool(tool)

    assert tool.name == "prompt_lab"
    assert tool.read_only is True
    assert tool.tool_tier.value == "core"
    assert spec.name == tool.name
    assert spec.parameters["required"] == ["action"]
    assert spec.parameters["properties"]["action"]["enum"] == [
        "inspect", "generate", "compare", "evaluate"]
    assert "prompt lab" in spec.aliases
    assert spec.when_to_use
    assert spec.when_not_to_use
    assert spec.examples


def test_prompt_lab_is_available_in_plan_read_only_pool():
    pool = TurnToolPool(
        ToolRegistry([PromptLabTool()]),
        runtime=RuntimeSnapshot(
            permission_mode=PermissionMode.PLAN,
        ),
    )
    assert pool.names() == ["prompt_lab"]
    assert pool.descriptor("prompt_lab").read_only is True


@pytest.mark.parametrize("enabled", [False, True])
def test_prompt_lab_registration_is_opt_in_and_tracks_agent_registry(
    tmp_path, enabled,
):
    args = build_parser().parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-compress", "--no-session",
    ])
    config = {
        "context": {
            "include_rules": False,
            "include_git": False,
            "include_tree": False,
        },
        "prompt": {"authoring": {"enabled": enabled}},
    }

    loop, manager = asyncio.run(build_loop(args, config))
    try:
        assert (loop.registry.get("prompt_lab") is not None) is enabled
        child_tools = {tool.name for tool in manager.tool_factory()}
        assert ("prompt_lab" in child_tools) is enabled
        assert loop.tool_context.prompt_registry is loop.prompt_registry
    finally:
        asyncio.run(manager.shutdown())


def test_inspect_is_content_light_reports_hash_and_conflicts(tmp_path):
    registry = _conflict_registry()
    service = PromptLabService(lambda: registry)

    result = service.inspect("indent")

    assert result["action"] == "inspect"
    assert result["matched"] == 2
    assert result["conflict_report"]["conflict_count"] == 1
    assert result["contains_prompt_content"] is False
    record = result["records"][0]
    assert len(record["content_hash"]) == 64
    assert record["conflict_ids"]
    serialized = json.dumps(result, ensure_ascii=False)
    assert "Use tabs for indentation." not in serialized
    assert "Use spaces for indentation." not in serialized


def test_inspect_applies_target_and_limit_metadata_bounds(tmp_path):
    registry = _conflict_registry()
    result = PromptLabService(lambda: registry).inspect("indent", limit=1)

    assert result["matched"] == 2
    assert result["truncated"] is True
    assert len(result["records"]) == 1


def test_compare_resolves_id_source_and_inline_text_without_bodies():
    registry = _conflict_registry()
    service = PromptLabService(lambda: registry)

    by_id = service.compare("managed:indent", "project:indent")
    by_source = service.compare("managed:indent", "project")
    inline = service.compare(
        "Use tabs for indentation.", "Use spaces for indentation.")

    assert by_id["left"]["origin"] == "fragment-id"
    assert by_id["right"]["origin"] == "fragment-id"
    assert by_source["right"]["found_fragment"] is True
    assert by_source["right"]["origin"] == "fragment-source"
    assert inline["left"]["origin"] == "inline-text"
    assert inline["right"]["origin"] == "inline-text"
    assert inline["conflict_count"] == 1
    assert inline["contains_prompt_content"] is False
    assert "Use tabs for indentation." not in json.dumps(inline)


def test_compare_rejects_ambiguous_sources():
    registry = PromptFragmentRegistry(base_fragments())
    service = PromptLabService(lambda: registry)

    with pytest.raises(ValueError, match="多个 Fragment"):
        service.compare("core", "Use spaces.")


def test_generate_returns_reviewable_candidate_without_mutation(tmp_path):
    registry = _conflict_registry()
    before_generation = registry.generation
    service = PromptLabService(lambda: registry)

    spec = _valid_candidate_spec()
    spec.pop("items")
    result = service.generate(spec)

    assert result["action"] == "generate"
    assert result["applied"] is False
    assert result["contains_prompt_content"] is True
    assert result["evaluation"]["passed"] is True
    assert result["candidate"]["tool_scope"] == "none"
    assert result["candidate"]["items"][0]["content"].startswith("[Objective]")
    assert registry.generation == before_generation
    assert registry.get("project:review-prompt") is None


def test_evaluate_reuses_validator_and_never_returns_candidate_body(tmp_path):
    registry = _conflict_registry()
    service = PromptLabService(lambda: registry)
    secret = "never emit sk-test-secret-value"

    result = service.evaluate(_valid_candidate_spec())
    failed = service.evaluate({
        "objective": "",
        "target_fragment_id": "",
        "items": ({
            "id": "bad",
            "role": "evidence",
            "source": "project",
            "content": secret,
            "stability": STABILITY_SESSION,
            "cacheable": True,
        },),
    })

    assert result["action"] == "evaluate"
    assert result["report"]["passed"] is True
    assert result["contains_prompt_content"] is False
    assert failed["report"]["passed"] is False
    assert failed["report"]["contains_prompt_content"] is False
    assert secret not in json.dumps(failed, ensure_ascii=False)


def test_prompt_lab_tool_wraps_service_errors_as_tool_errors(tmp_path):
    registry = _conflict_registry()
    tool = PromptLabTool()
    ctx = _tool_context(registry, tmp_path)

    invalid_action = asyncio.run(tool.execute({"action": "unknown"}, ctx))
    missing_spec = asyncio.run(tool.execute({"action": "generate"}, ctx))
    missing_compare = asyncio.run(tool.execute({"action": "compare"}, ctx))

    assert invalid_action.is_error is True
    assert missing_spec.is_error is True
    assert missing_compare.is_error is True
    assert "action" in invalid_action.data


def test_prompt_lab_tool_renders_all_four_actions(tmp_path):
    registry = _conflict_registry()
    tool = PromptLabTool()
    ctx = _tool_context(registry, tmp_path)

    inspect = asyncio.run(tool.execute({"action": "inspect", "target": "indent"}, ctx))
    compare = asyncio.run(tool.execute({
        "action": "compare",
        "left": "Use tabs for indentation.",
        "right": "Use spaces for indentation.",
    }, ctx))
    generate = asyncio.run(tool.execute({
        "action": "generate",
        "spec": _valid_candidate_spec(),
    }, ctx))
    evaluate = asyncio.run(tool.execute({
        "action": "evaluate",
        "spec": _valid_candidate_spec(),
    }, ctx))

    assert all(result.is_error is False for result in (
        inspect, compare, generate, evaluate))
    assert "body_returned=false" in inspect.content
    assert "PromptLab compare" in compare.content
    assert "NOT applied" in generate.content
    assert "PromptLab evaluate: PASS" in evaluate.content


def test_prompt_lab_large_compare_and_item_inputs_fail_closed(tmp_path):
    registry = PromptFragmentRegistry(base_fragments())
    tool = PromptLabTool()
    ctx = _tool_context(registry, tmp_path)
    oversized_text = "x" * 100_001
    oversized_item = dict(_valid_candidate_spec())
    oversized_item["items"] = ({
        "id": "large",
        "role": "instruction",
        "source": "project",
        "content": "x" * 40_001,
        "layer": LAYER_L6_CATALOG,
        "stability": STABILITY_SESSION,
        "cacheable": True,
    },)

    compare = asyncio.run(tool.execute({
        "action": "compare", "left": oversized_text, "right": "small",
    }, ctx))
    generate = asyncio.run(tool.execute({
        "action": "generate", "spec": oversized_item,
    }, ctx))

    assert compare.is_error is True
    assert generate.is_error is True


def test_prompt_lab_executes_through_agent_loop_with_context_registry(tmp_path):
    registry = _conflict_registry()
    context = _tool_context(registry, tmp_path)
    call = ToolCall("prompt-call", "prompt_lab", {
        "action": "inspect", "target": "indent", "limit": 1,
    })
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[call]),
        "done",
    ])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([PromptLabTool()]),
        tool_context=context,
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    loop.prompt_registry = registry

    reply = asyncio.run(loop.run("inspect the current prompt"))

    assert reply == "done"
    tool_message = next(message for message in loop.history if message.role == "tool")
    assert "PromptLab inspect" in tool_message.content
    assert "Use tabs for indentation." not in tool_message.content
    assert provider.calls[0][-1].role == "user"


def test_subagent_prompt_lab_uses_child_registry_not_parent(tmp_path):
    async def go():
        parent_registry = _conflict_registry()
        parent_context = _tool_context(parent_registry, tmp_path)
        manager = SubagentManager(
            llm=MockProvider(scripted=["child complete"]),
            tool_factory=lambda: [PromptLabTool()],
            tool_context=parent_context,
            policy=AllowAllPolicy(),
        )
        try:
            await manager.spawn("inspect the prompt architecture")
            handle = await manager.wait("a1", timeout=5)
            assert handle.status == "completed"
            assert handle.loop.tool_context.prompt_registry is (
                handle.loop.prompt_registry)
            assert handle.loop.tool_context.prompt_registry is not parent_registry
            result = await PromptLabTool().execute(
                {"action": "inspect", "limit": 1},
                handle.loop.tool_context,
            )
            assert result.is_error is False
            assert result.data["registry_generation"] >= 1
        finally:
            await manager.shutdown()

    asyncio.run(go())
