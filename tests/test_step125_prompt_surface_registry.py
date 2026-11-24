"""Step 125: content-light Prompt Surface catalogue and observability."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompt_surface import (
    PromptSurfaceDescriptor,
    PromptSurfaceKind,
    PromptSurfaceRegistry,
    PromptSurfaceSnapshot,
    prompt_content_hash,
)
from krelcode.agent.prompt_surface_catalog import (
    custom_command_prompt_surfaces,
    hook_prompt_surfaces,
    output_style_prompt_surfaces,
    refresh_prompt_surfaces,
    skill_prompt_surfaces,
)
from krelcode.hooks import HookSpec
from krelcode.llm.providers.mock import MockProvider
from krelcode.review import review_prompt_surface
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.controller import SessionController
from krelcode.subagent.definition import AgentDefinitionManager
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


class FakePromptTool:
    name = "fake_prompt"
    description = "A fake tool for Prompt Surface tests."
    parameters = {
        "type": "object",
        "properties": {},
    }
    prompt = "SECRET_PROMPT_BODY_MUST_NOT_APPEAR"
    when_to_use = ("Use only in the Prompt Surface test.",)
    when_not_to_use = ("Do not use in production.",)

    async def execute(self, args, ctx) -> ToolResult:
        return ToolResult(content="ok")


class FakeMCPSupervisor:
    def prompt_records(self):
        return [{
            "server": "docs",
            "prompt_name": "review",
            "command_name": "mcp__docs__review",
            "description": "Remote review template",
            "argument_names": ("scope",),
        }]

    def server_instructions(self):
        return (("docs", "Use remote documentation as evidence."),)


def make_loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        llm=MockProvider(scripted=["done"]),
        registry=ToolRegistry([FakePromptTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM",
        today_provider=lambda: "2026-11-21",
    )


def _surface(
    surface_id: str,
    body: str,
    *,
    kind: PromptSurfaceKind = PromptSurfaceKind.TOOL,
) -> PromptSurfaceDescriptor:
    return PromptSurfaceDescriptor(
        id=surface_id,
        kind=kind,
        source="test",
        owner="test",
        content_hash=prompt_content_hash(body),
        token_estimate=len(body) // 4,
        description="test surface",
    )


def test_registry_is_content_light_and_preserves_observations_on_refresh():
    registry = PromptSurfaceRegistry({
        "test": (_surface("tool:demo", "body-v1"),),
    })
    assert registry.observe(
        "tool:demo",
        content_hash=prompt_content_hash("body-v1"),
        tokens=4,
        turn_id="turn-1",
        step=2,
        rendered_at=10.0,
    )

    first = registry.snapshot().as_dict()
    assert "body-v1" not in repr(first)
    assert first["surfaces"][0]["content_hash"] == prompt_content_hash("body-v1")
    assert first["observations"][0]["render_count"] == 1

    registry.replace_collections({
        "test": (_surface("tool:demo", "body-v2"),),
    })
    observation = registry.observation("tool:demo")
    assert observation is not None
    assert observation.render_count == 1
    assert observation.is_current(registry.get("tool:demo").content_hash) is False


def test_snapshot_roundtrip_and_catalogue_hash_ignore_generation():
    registry = PromptSurfaceRegistry({
        "test": (_surface("tool:demo", "body-v1"),),
    })
    first = registry.snapshot()
    restored = PromptSurfaceSnapshot.from_dict(first.as_dict())

    assert restored == first
    assert restored.as_dict() == first.as_dict()
    assert restored.content_hash == first.content_hash

    registry.replace_collections({
        "test": (_surface("tool:demo", "body-v1"),),
    })
    refreshed = registry.snapshot()
    assert refreshed.generation == first.generation + 1
    assert refreshed.content_hash == first.content_hash


def test_collection_replacement_is_atomic_and_prunes_empty_collections():
    registry = PromptSurfaceRegistry({
        "test": (_surface("tool:demo", "body-v1"),),
    })
    registry.observe("tool:demo", rendered_at=1.0)
    before = registry.snapshot()

    with pytest.raises(ValueError, match="id 重复"):
        registry.replace_collections({
            "left": (_surface("tool:duplicate", "left"),),
            "right": (_surface("tool:duplicate", "right"),),
        })

    assert registry.snapshot() == before

    registry.replace_collections({"test": ()})
    assert registry.list() == ()
    assert registry.collections() == {"test": ()}
    assert registry.observations() == ()


def test_runtime_catalogue_covers_main_tools_workflows_subagents_and_auxiliary(
    tmp_path,
):
    loop = make_loop(tmp_path)
    loop.agent_definitions = AgentDefinitionManager(tmp_path, home=tmp_path)
    loop.auxiliary_prompt_surfaces = (review_prompt_surface(),)
    registry = refresh_prompt_surfaces(
        loop, mcp_supervisor=FakeMCPSupervisor())

    assert registry.get("main:agent") is not None
    assert registry.get("tool:fake_prompt") is not None
    assert registry.get("workflow:bug-fix") is not None
    assert registry.get("subagent:verifier") is not None
    assert registry.get("aux:compact") is not None
    assert registry.get("aux:review") is not None
    mcp = registry.get("mcp-prompt:docs:review")
    assert mcp is not None
    assert mcp.role.value == "evidence"
    assert mcp.trust == "untrusted"
    assert registry.get("mcp-instructions:docs") is not None


def test_extension_surface_sources_are_classified_without_prompt_bodies(tmp_path):
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("SKILL_SECRET_BODY", encoding="utf-8")
    output_styles = SimpleNamespace(
        active_name="review",
        styles=(SimpleNamespace(
            name="review",
            scope="user",
            content="STYLE_SECRET_BODY",
            description="Review style",
            path=tmp_path / "review.md",
            keep_coding_instructions=True,
        ),),
    )
    custom_commands = SimpleNamespace(list=lambda: (
        SimpleNamespace(
            name="audit",
            source="user",
            prompt="COMMAND_SECRET_BODY",
            path=tmp_path / "audit.md",
            description="Audit command",
            argument_hint="<path>",
            allowed_tools=("read_file",),
        ),
    ))
    skills = SimpleNamespace(list=lambda: (
        SimpleNamespace(
            name="review-skill",
            source="project",
            skill_file=skill_file,
            description="Review skill",
            version="1",
            disable_model_invocation=False,
            user_invocable=True,
            argument_hint="<path>",
            allowed_tools=("read_file",),
        ),
    ))
    hooks = SimpleNamespace(specs=(HookSpec(
        on="user_prompt_submit",
        kind="prompt",
        prompt="HOOK_SECRET_BODY",
        source="plugin",
    ),))

    surfaces = (
        *output_style_prompt_surfaces(output_styles),
        *custom_command_prompt_surfaces(custom_commands),
        *skill_prompt_surfaces(skills),
        *hook_prompt_surfaces(hooks),
    )
    by_kind = {surface.kind: surface for surface in surfaces}

    assert by_kind[PromptSurfaceKind.OUTPUT_STYLE].authority == "user"
    assert by_kind[PromptSurfaceKind.OUTPUT_STYLE].trust == "user"
    assert by_kind[PromptSurfaceKind.CUSTOM_COMMAND].authority == "user"
    assert by_kind[PromptSurfaceKind.SKILL].authority == "project"
    assert by_kind[PromptSurfaceKind.HOOK].authority == "project"
    serialized = repr([surface.as_dict() for surface in surfaces])
    for secret in (
        "SKILL_SECRET_BODY",
        "STYLE_SECRET_BODY",
        "COMMAND_SECRET_BODY",
        "HOOK_SECRET_BODY",
    ):
        assert secret not in serialized


def test_agent_run_records_actual_surfaces_without_content_leak(tmp_path):
    loop = make_loop(tmp_path)
    refresh_prompt_surfaces(loop)

    reply = asyncio.run(loop.run("hello"))

    assert reply == "done"
    main = loop.prompt_surface_registry.observation("main:agent")
    tool = loop.prompt_surface_registry.observation("tool:fake_prompt")
    assert main is not None and main.render_count == 1
    refresh_prompt_surfaces(loop)
    main = loop.prompt_surface_registry.observation("main:agent")
    assert main.is_current(loop.prompt_surface_registry.get("main:agent").content_hash)
    assert tool is not None and tool.render_count == 1
    assert loop.llm.calls[0][0].content == loop.last_prompt_plan.system_text
    snapshot = loop.prompt_surface_registry.snapshot().as_dict()
    assert "SECRET_PROMPT_BODY_MUST_NOT_APPEAR" not in repr(snapshot)
    assert "SYSTEM" not in repr(snapshot)


def test_prompts_command_lists_and_inspects_metadata_only(tmp_path):
    loop = make_loop(tmp_path)
    controller = SessionController(loop, workspace_root=tmp_path)

    summary = "\n".join(asyncio.run(
        controller.execute_command("/prompts")).output)
    listed = "\n".join(asyncio.run(
        controller.execute_command("/prompts list tool")).output)
    shown = "\n".join(asyncio.run(
        controller.execute_command("/prompts show tool:fake_prompt")).output)
    coverage = "\n".join(asyncio.run(
        controller.execute_command("/prompts coverage")).output)
    hashed = "\n".join(asyncio.run(
        controller.execute_command("/prompts hash")).output)

    assert "[prompts]" in summary
    assert "tool:fake_prompt" in listed
    assert "instruction/managed" in shown
    assert "hash=" in shown
    assert "core coverage:" in coverage
    assert "catalogue hash" in hashed
    for output in (summary, listed, shown, coverage, hashed):
        assert "SECRET_PROMPT_BODY_MUST_NOT_APPEAR" not in output
