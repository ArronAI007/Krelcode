"""Step 121: Claude Code-compatible built-in agents."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from krelcode.app import build_subagent_llm_resolver
from krelcode.core.events import EventBus
from krelcode.core.messages import Message
from krelcode.core.redact import SecretRedactor
from krelcode.llm.base import ProviderCapabilities, StreamFinished
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands.builtin import builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers import BUILTIN_HANDLERS
from krelcode.session.commands.types import CommandRequest
from krelcode.subagent.definition import AgentDefinitionManager
from krelcode.subagent.instructions import build_subagent_instructions
from krelcode.subagent.manager import SubagentManager
from krelcode.subagent.model_aliases import resolve_agent_model_spec
from krelcode.subagent.sidechain import read_sidechain
from krelcode.tools.base import ToolContext, ToolResult


CLAUDE_AGENT_NAMES = {
    "Explore",
    "Plan",
    "general-purpose",
    "claude",
    "statusline-setup",
    "claude-code-guide",
}


class CaptureProvider:
    capabilities = ProviderCapabilities()

    def __init__(self, name: str = "capture", model: str = "capture") -> None:
        self.name = name
        self.model = model
        self.system = ""
        self.tools = ()

    async def stream(self, messages, tools=None):
        self.system = messages[0].content or ""
        self.tools = tuple(tool.get("name", "") for tool in (tools or ()))
        yield StreamFinished(
            message=Message.assistant("ok"),
            finish_reason="stop",
            usage={},
        )


class StubTool:
    def __init__(
        self,
        name: str,
        *,
        read_only: bool = True,
        mcp_server: str = "",
    ) -> None:
        self.name = name
        self.description = name
        self.parameters = {"type": "object", "properties": {}}
        self.read_only = read_only
        if mcp_server:
            self.client = SimpleNamespace(name=mcp_server)

    async def execute(self, args, ctx):
        return ToolResult(content="ok")


def _manager(tmp_path, provider=None, tools=None, resolver=None):
    provider = provider or CaptureProvider()
    tools = tools or (lambda: [
        StubTool("read_file"),
        StubTool("grep"),
        StubTool("glob"),
        StubTool("check_syntax"),
        StubTool("write_file", read_only=False),
        StubTool("edit_file", read_only=False),
        StubTool("run_command", read_only=False),
        StubTool("mcp__alpha__search", mcp_server="alpha"),
    ])
    return SubagentManager(
        llm=provider,
        tool_factory=tools,
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=EventBus(),
        definitions=AgentDefinitionManager(tmp_path, home=tmp_path / "home"),
        llm_resolver=resolver,
    )


def test_six_claude_agents_and_verifier_are_discovered(tmp_path):
    manager = AgentDefinitionManager(tmp_path, home=tmp_path / "home")
    definitions = {item.name: item for item in manager.list()}

    assert set(definitions) == CLAUDE_AGENT_NAMES | {"verifier"}
    for name in CLAUDE_AGENT_NAMES:
        assert definitions[name].source == "builtin"
        assert definitions[name].path is None
        assert definitions[name].system_prompt.strip()

    assert definitions["Explore"].permission_mode == "read-only"
    assert definitions["Explore"].inherit_project_rules is False
    assert definitions["Explore"].mcp_servers == ()
    assert definitions["Plan"].permission_mode == "read-only"
    assert definitions["Plan"].inherit_project_rules is False
    assert definitions["general-purpose"].allowed_tools == ()
    assert definitions["claude"].allowed_tools == ()
    assert definitions["statusline-setup"].model == "sonnet"
    assert definitions["claude-code-guide"].model == "haiku"
    assert definitions["claude-code-guide"].permission_mode == "read-only"


def test_explore_and_plan_omit_user_and_project_rules(tmp_path):
    home = tmp_path / "home"
    (home / ".krelcode").mkdir(parents=True)
    (home / ".krelcode" / "AGENTS.md").write_text(
        "USER_RULE_SENTINEL", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "PROJECT_RULE_SENTINEL", encoding="utf-8")
    definitions = AgentDefinitionManager(tmp_path, home=home)

    for name in ("Explore", "Plan"):
        bundle = build_subagent_instructions(
            tmp_path,
            "inspect chapter1/context/run.py",
            base_prompt="BASE",
            definition=definitions.get(name),
            home=home,
        )
        assert bundle.rule_layers == ()
        assert "USER_RULE_SENTINEL" not in bundle.text
        assert "PROJECT_RULE_SENTINEL" not in bundle.text
        assert definitions.get(name).system_prompt.strip() in bundle.text

    general = build_subagent_instructions(
        tmp_path,
        "inspect chapter1/context/run.py",
        base_prompt="BASE",
        definition=definitions.get("general-purpose"),
        home=home,
    )
    assert "USER_RULE_SENTINEL" in general.text
    assert "PROJECT_RULE_SENTINEL" in general.text


def test_project_definition_can_override_every_claude_builtin(tmp_path):
    home = tmp_path / "home"
    agents = tmp_path / ".krelcode" / "agents"
    agents.mkdir(parents=True)
    manager = AgentDefinitionManager(tmp_path, home=home)

    for name in CLAUDE_AGENT_NAMES:
        (agents / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: custom {name}\n"
            "allowed-tools: [read_file]\n---\nCUSTOM OVERRIDE\n",
            encoding="utf-8",
        )
        manager.reload()
        definition = manager.get(name)
        assert definition.source == "project"
        assert definition.description == f"custom {name}"
        assert definition.system_prompt == "CUSTOM OVERRIDE"


def test_spawn_without_definition_uses_general_purpose(tmp_path):
    async def go():
        provider = CaptureProvider()
        manager = _manager(tmp_path, provider)

        handle = await manager.spawn("Fix a bounded parser regression")
        handle = await manager.wait(handle.id, timeout=5)

        assert handle.status == "completed"
        assert handle.definition == "general-purpose"
        assert "You are general-purpose" in provider.system
        assert "write_file" in provider.tools
        assert "edit_file" in provider.tools
        assert "mcp__alpha__search" in provider.tools
        sidechain = read_sidechain(handle.sidechain_path)
        start = next(
            item for item in sidechain if item.get("type") == "sidechain_start")
        assert start["definition"] == "general-purpose"
        await manager.shutdown()

    asyncio.run(go())


def test_explore_and_plan_spawns_are_read_only_and_mcp_free(tmp_path):
    async def go():
        for name, prompt_sentinel in (
            ("Explore", "You are Explore"),
            ("Plan", "You are Plan"),
        ):
            provider = CaptureProvider()
            manager = _manager(tmp_path, provider)
            handle = await manager.spawn("Research the parser", definition=name)
            handle = await manager.wait(handle.id, timeout=5)

            assert handle.status == "completed"
            assert handle.definition == name
            assert handle.permission_mode == "read-only"
            assert handle.mcp_servers == ()
            assert prompt_sentinel in provider.system
            assert {"read_file", "grep", "glob"} <= set(provider.tools)
            assert "write_file" not in provider.tools
            assert "edit_file" not in provider.tools
            assert "run_command" not in provider.tools
            assert "mcp__alpha__search" not in provider.tools
            await manager.shutdown()

    asyncio.run(go())


def test_claude_agent_inherits_full_child_pool_and_parent_model(tmp_path):
    async def go():
        provider = CaptureProvider("openai", "deepseek-chat")
        manager = _manager(tmp_path, provider)
        handle = await manager.spawn("Handle the catch-all task", definition="claude")
        handle = await manager.wait(handle.id, timeout=5)

        assert handle.status == "completed"
        assert handle.model == "deepseek-chat"
        assert "You are claude" in provider.system
        assert {"write_file", "edit_file", "run_command", "mcp__alpha__search"} <= set(
            provider.tools)
        await manager.shutdown()

    asyncio.run(go())


def test_statusline_and_guide_use_expected_tool_scopes(tmp_path):
    async def go():
        child = CaptureProvider("anthropic", "claude-sonnet-4-5")
        manager = _manager(tmp_path, resolver=lambda spec: child)
        handle = await manager.spawn("Configure status line", definition="statusline-setup")
        handle = await manager.wait(handle.id, timeout=5)

        assert handle.status == "completed"
        assert handle.model == "claude-sonnet-4-5"
        assert handle.mcp_servers == ()
        assert "You are statusline-setup" in child.system
        assert {"read_file", "grep", "glob", "check_syntax", "write_file", "edit_file"} <= set(
            child.tools)
        assert "run_command" not in child.tools
        assert "mcp__alpha__search" not in child.tools
        await manager.shutdown()

        guide = CaptureProvider()
        manager = _manager(tmp_path, guide, resolver=lambda spec: guide)
        handle = await manager.spawn("Explain hooks", definition="claude-code-guide")
        handle = await manager.wait(handle.id, timeout=5)
        assert handle.status == "completed"
        assert handle.permission_mode == "read-only"
        assert "You are claude-code-guide" in guide.system
        assert "write_file" not in guide.tools
        assert "edit_file" not in guide.tools
        assert "mcp__alpha__search" not in guide.tools
        await manager.shutdown()

    asyncio.run(go())


def test_agent_model_alias_resolution_and_overrides():
    assert resolve_agent_model_spec(
        "sonnet", parent_provider="anthropic") == (
            "anthropic", "claude-sonnet-4-5")
    assert resolve_agent_model_spec(
        "haiku", parent_provider="anthropic") == (
            "anthropic", "claude-haiku-4-5")
    assert resolve_agent_model_spec(
        "sonnet", parent_provider="openai") is None
    assert resolve_agent_model_spec(
        "haiku",
        parent_provider="openai",
        config={"agent_models": {"haiku": "openai/deepseek-flash"}},
    ) == ("openai", "deepseek-flash")
    assert resolve_agent_model_spec(
        "inherit", parent_provider="openai") is None


def test_subagent_resolver_falls_back_for_non_anthropic_parent():
    parent = CaptureProvider("openai", "deepseek-chat")
    resolver = build_subagent_llm_resolver(
        SimpleNamespace(base_url="https://api.deepseek.com/v1", retries=0),
        {"providers": {}},
        parent,
        SecretRedactor(),
    )

    assert resolver("sonnet") is parent


def test_statusline_command_is_exposed_and_spawns_focused_agent(tmp_path):
    registry = builtin_command_registry()
    assert registry.resolve("/statusline") is not None
    assert BUILTIN_HANDLERS["statusline"] is not None

    spawned = []

    class FakeManager:
        async def spawn(self, task, definition=None):
            spawned.append((task, definition))
            return SimpleNamespace(id="a1")

    request = CommandRequest(
        name="statusline", arguments="show model and branch", raw_text="/statusline show model and branch")
    ctx = CommandContext(
        request=request,
        loop=SimpleNamespace(),
        command_registry=registry,
        workspace_root=tmp_path,
        subagents=FakeManager(),
    )

    result = asyncio.run(BUILTIN_HANDLERS["statusline"](ctx))
    assert result.handled
    assert spawned and spawned[0][1] == "statusline-setup"
    assert "show model and branch" in spawned[0][0]
    assert any("a1" in line for line in result.output)
