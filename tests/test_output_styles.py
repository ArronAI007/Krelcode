from __future__ import annotations

import asyncio
from pathlib import Path

from krelcode.agent.fragments import (
    LAYER_L4_AGENT_STYLE,
    STABILITY_SESSION,
    TRUST_MANAGED,
    TRUST_PROJECT,
    PromptFragmentRegistry,
    base_fragment,
)
from krelcode.app import build_parser
from krelcode.core.messages import Message
from krelcode.llm.providers.mock import MockProvider
from krelcode.output_styles import (
    BUILTIN_OUTPUT_STYLES,
    DEFAULT_OUTPUT_STYLE_NAME,
    OutputStyleConfig,
    OutputStyleManager,
)
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands.builtin import builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers.runtime_ops import handle_output_style
from krelcode.session.commands.types import CommandRequest
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry
from krelcode.agent.loop import AgentLoop


def write_style(path: Path, name: str, body: str, description: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )


def make_loop(tmp_path: Path):
    loop = AgentLoop(
        llm=MockProvider(),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="base",
    )
    registry = PromptFragmentRegistry([base_fragment()])
    loop.prompt_registry = registry
    return loop, registry


def test_output_style_discovery_project_overrides_user_and_frontmatter(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    write_style(home / ".krelcode/output-styles/concise.md", "concise", "USER")
    write_style(workspace / ".krelcode/output-styles/concise.md", "concise", "PROJECT")
    write_style(workspace / ".krelcode/output-styles/detail.md", "detail", "DETAIL")
    manager = OutputStyleManager(workspace, home=home)
    assert manager.get("concise").content == "PROJECT\n"
    assert manager.get("concise").scope == "project"
    assert manager.get("detail").source.startswith("output-style:project:")
    assert "concise.md" in manager.conflicts[0]


def test_output_style_fragment_is_traceable_and_replaceable(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    write_style(home / ".krelcode/output-styles/a.md", "a", "Answer tersely.")
    write_style(workspace / ".krelcode/output-styles/b.md", "b", "Explain deeply.")
    manager = OutputStyleManager(workspace, home=home)
    registry = PromptFragmentRegistry([base_fragment()])
    manager.select("a")
    fragment = manager.apply_to_registry(registry)
    assert fragment is not None
    assert fragment.id == "style:output"
    assert fragment.source.startswith("output-style:user:")
    assert "Answer tersely." in registry.snapshot().text
    manager.select("b")
    manager.apply_to_registry(registry)
    text = registry.snapshot().text
    assert "Explain deeply." in text
    assert "Answer tersely." not in text
    manager.select("off")
    manager.apply_to_registry(registry)
    assert "Explain deeply." not in registry.snapshot().text


def test_output_style_command_switches_and_reloads(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    write_style(home / ".krelcode/output-styles/a.md", "a", "A body")
    manager = OutputStyleManager(workspace, home=home)
    loop, registry = make_loop(workspace)
    loop.output_style_manager = manager
    context = CommandContext(
        request=CommandRequest("output-style", "a", "/output-style a"),
        loop=loop,
        command_registry=builtin_command_registry(),
        workspace_root=workspace,
    )
    result = asyncio.run(handle_output_style(context))
    assert result.handled
    assert "已切换" in result.output[0]
    assert "A body" in registry.snapshot().text
    assert any("output-style" in spec.name for spec in builtin_command_registry().specs)
    parsed = build_parser().parse_args(["--output-style", "a", "--output-style-dir", str(home)])
    assert parsed.output_style == "a"


def test_builtin_output_styles_are_discoverable_and_injected_as_l4_prompt(tmp_path):
    manager = OutputStyleManager(tmp_path, home=tmp_path / "home")

    assert [style.name for style in manager.styles[:3]] == [
        "default", "explanatory", "learning"]
    assert all(style.scope == "builtin" for style in BUILTIN_OUTPUT_STYLES)
    assert manager.select("explanatory") is not None

    registry = PromptFragmentRegistry([base_fragment()])
    fragment = manager.apply_to_registry(registry)

    assert fragment is not None
    assert fragment.id == "style:output"
    assert fragment.layer == LAYER_L4_AGENT_STYLE
    assert fragment.stability == STABILITY_SESSION
    assert fragment.trust == TRUST_MANAGED
    assert fragment.source == "output-style:builtin:explanatory"
    assert fragment.metadata["builtin"] is True
    assert fragment.metadata["keep_coding_instructions"] is True
    assert "Insight" in registry.snapshot().text
    assert "Do not expose hidden chain-of-thought" in registry.snapshot().text


def test_default_style_removes_extra_personality_prompt(tmp_path):
    manager = OutputStyleManager(tmp_path, home=tmp_path / "home")
    registry = PromptFragmentRegistry([base_fragment()])
    manager.select("learning")
    assert manager.apply_to_registry(registry) is not None

    selected = manager.select("default")
    manager.apply_to_registry(registry)

    assert selected is not None
    assert selected.name == DEFAULT_OUTPUT_STYLE_NAME
    assert manager.active_name == DEFAULT_OUTPUT_STYLE_NAME
    assert registry.get("style:output") is None
    assert "<output_style" not in registry.snapshot().text


def test_custom_style_keep_coding_instructions_controls_l4_conflict_contract(tmp_path):
    workspace = tmp_path / "workspace"
    path = workspace / ".krelcode/output-styles/custom.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "name: custom\n"
        "keep-coding-instructions: false\n"
        "---\n"
        "Use a teaching tone.\n",
        encoding="utf-8",
    )
    manager = OutputStyleManager(workspace, home=tmp_path / "home")
    manager.select("custom")
    fragment = manager.fragment()

    assert fragment is not None
    assert fragment.trust == TRUST_PROJECT
    assert fragment.metadata["keep_coding_instructions"] is False
    assert "supersedes conflicting L1 coding-style guidance" in fragment.content


def test_project_style_can_override_builtin_without_losing_traceability(tmp_path):
    workspace = tmp_path / "workspace"
    path = workspace / ".krelcode/output-styles/learning.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: learning\ndescription: project variant\n---\nPROJECT_LEARNING\n",
        encoding="utf-8",
    )
    manager = OutputStyleManager(workspace, home=tmp_path / "home")

    assert manager.get("learning").content == "PROJECT_LEARNING\n"
    assert manager.get("learning").scope == "project"
    assert any("learning" in item and "builtin" in item for item in manager.conflicts)


def test_output_style_command_switches_builtin_and_explains_default(tmp_path):
    manager = OutputStyleManager(tmp_path, home=tmp_path / "home")
    loop, registry = make_loop(tmp_path)
    loop.output_style_manager = manager

    explanatory = asyncio.run(handle_output_style(CommandContext(
        request=CommandRequest("output-style", "explanatory", "/output-style explanatory"),
        loop=loop,
        command_registry=builtin_command_registry(),
        workspace_root=tmp_path,
    )))
    assert explanatory.handled
    assert "已切换 Output Style: explanatory [builtin]" in explanatory.output[0]
    assert "Insight" in registry.snapshot().text

    default = asyncio.run(handle_output_style(CommandContext(
        request=CommandRequest("output-style", "default", "/output-style default"),
        loop=loop,
        command_registry=builtin_command_registry(),
        workspace_root=tmp_path,
    )))

    assert default.handled
    assert "已恢复默认输出样式" in default.output[0]
    assert registry.get("style:output") is None
