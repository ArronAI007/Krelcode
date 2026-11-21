"""Step 119: adversarial verification-agent Prompt and adaptive checks."""

from __future__ import annotations

import asyncio

from krelcode.agent.verification import (
    VERIFICATION_AGENT_ALLOWED_TOOLS,
    VERIFICATION_AGENT_PROMPT_VERSION,
    VERIFICATION_AGENT_SYSTEM_PROMPT,
    VERIFICATION_CHANGE_PROFILES,
    VERIFICATION_OUTPUT_FORMAT,
    VERIFICATION_RATIONALIZATIONS,
    build_verification_agent_prompt,
)
from krelcode.app import default_tools
from krelcode.core.events import EventBus
from krelcode.core.messages import Message
from krelcode.llm.base import ProviderCapabilities, StreamFinished
from krelcode.safety.policy import AllowAllPolicy
from krelcode.subagent.definition import AgentDefinitionManager
from krelcode.subagent.manager import SubagentManager
from krelcode.subagent.sidechain import read_sidechain
from krelcode.tools.base import ToolContext, ToolResult


class CaptureProvider:
    name = "capture"
    model = "capture"
    capabilities = ProviderCapabilities()

    def __init__(self) -> None:
        self.system = ""
        self.tools = ()

    async def stream(self, messages, tools=None):
        self.system = messages[0].content or ""
        self.tools = tuple(tool.get("name", "") for tool in (tools or ()))
        yield StreamFinished(
            message=Message.assistant("verified"),
            finish_reason="stop",
            usage={},
        )


class StubTool:
    def __init__(self, name: str, *, read_only: bool = True) -> None:
        self.name = name
        self.description = name
        self.parameters = {"type": "object", "properties": {}}
        self.read_only = read_only

    async def execute(self, args, ctx):
        return ToolResult(content="ok")


def test_verification_prompt_models_adversarial_failure_modes():
    prompt = build_verification_agent_prompt()
    flat = " ".join(prompt.split()).lower()

    assert "you are an independent verifier" in flat
    assert "assume the validating model is" in flat
    assert "biased toward the cheapest explanation" in flat
    assert "treat every claim as unverified" in flat
    assert "RECOGNIZE_YOUR_OWN_RATIONALIZATIONS" in prompt
    assert "OUTPUT FORMAT" in prompt
    assert "never turn missing evidence" in flat
    assert "the implementer report is a claim, not evidence" in flat


def test_each_rationalization_has_a_required_evidence_response():
    prompt = VERIFICATION_AGENT_SYSTEM_PROMPT

    assert len(VERIFICATION_RATIONALIZATIONS) >= 8
    for thought, response in VERIFICATION_RATIONALIZATIONS:
        assert thought in prompt
        assert response in prompt

    assert '"It compiles, so it works."' in prompt
    assert '"The failure is unrelated."' in prompt
    assert '"The migration is simple, so rollback is unnecessary."' in prompt
    assert '"The refactor only moves code."' in prompt


def test_structured_output_contract_is_explicit_and_machine_shaped():
    required_fields = (
        '"verdict"',
        '"reason"',
        '"change_types"',
        '"claims_checked"',
        '"checks"',
        '"behavior_preservation"',
        '"rollback"',
        '"rationalizations_detected"',
        '"unverified_risks"',
        '"verdict_reason"',
    )

    assert VERIFICATION_OUTPUT_FORMAT.strip().startswith("Return exactly one JSON")
    for field in required_fields:
        assert field in VERIFICATION_OUTPUT_FORMAT
    assert "pass|fail|blocked" in VERIFICATION_OUTPUT_FORMAT
    assert "confirmed|refuted|unverified" in VERIFICATION_OUTPUT_FORMAT


def test_change_type_matrix_covers_high_risk_project_classes():
    profiles = {profile.id: profile for profile in VERIFICATION_CHANGE_PROFILES}
    prompt = VERIFICATION_AGENT_SYSTEM_PROMPT

    assert set(profiles) == {
        "backend-api",
        "frontend-web",
        "mobile",
        "database-migration",
        "data-ml-pipeline",
        "infrastructure",
        "dependency-upgrade",
        "refactor",
    }
    assert "down/rollback migration" in prompt
    assert "iOS or Android" in prompt
    assert "train/eval separation" in prompt
    assert "rollout and rollback" in prompt
    assert "pre-existing relevant tests unchanged" in prompt
    assert "do not rewrite, weaken, delete, or skip" in prompt


def test_verification_prompt_build_and_version_are_deterministic():
    assert build_verification_agent_prompt() == VERIFICATION_AGENT_SYSTEM_PROMPT
    assert VERIFICATION_AGENT_PROMPT_VERSION == "1"


def test_builtin_verifier_is_discovered_with_bounded_tools(tmp_path):
    manager = AgentDefinitionManager(tmp_path, home=tmp_path / "home")
    definition = manager.get("verifier")

    assert definition.source == "builtin"
    assert definition.path is None
    assert definition.allowed_tools == VERIFICATION_AGENT_ALLOWED_TOOLS
    assert "write_file" not in definition.allowed_tools
    assert "edit_file" not in definition.allowed_tools
    assert definition.mcp_servers == ()
    assert "independent verifier" in definition.system_prompt

    lines = manager.status_lines()
    assert any("[builtin]" in line and line.endswith("built-in") for line in lines)


def test_project_verifier_definition_overrides_builtin(tmp_path):
    agents = tmp_path / ".krelcode" / "agents"
    agents.mkdir(parents=True)
    (agents / "verifier.md").write_text(
        "---\nname: verifier\ndescription: custom verifier\n"
        "allowed-tools: [read_file]\n---\nCUSTOM VERIFIER\n",
        encoding="utf-8",
    )

    manager = AgentDefinitionManager(tmp_path, home=tmp_path / "home")
    definition = manager.get("verifier")

    assert definition.source == "project"
    assert definition.description == "custom verifier"
    assert definition.allowed_tools == ("read_file",)
    assert "CUSTOM VERIFIER" in definition.system_prompt


def test_verifier_allowed_tools_are_all_available_in_default_pool():
    available = {tool.name for tool in default_tools()}

    assert set(VERIFICATION_AGENT_ALLOWED_TOOLS) <= available


def test_spawn_verifier_uses_prompt_and_cannot_receive_write_tools(tmp_path):
    async def go():
        provider = CaptureProvider()
        tool_names = (*VERIFICATION_AGENT_ALLOWED_TOOLS, "write_file", "edit_file")
        manager = SubagentManager(
            llm=provider,
            tool_factory=lambda: [
                StubTool(name, read_only=name not in {"run_command"})
                for name in tool_names
            ],
            tool_context=ToolContext(workspace_root=tmp_path),
            policy=AllowAllPolicy(),
            bus=EventBus(),
            definitions=AgentDefinitionManager(tmp_path, home=tmp_path / "home"),
        )

        handle = await manager.spawn("Verify the refactor", definition="verifier")
        handle = await manager.wait(handle.id, timeout=5)

        assert handle.status == "completed"
        assert "independent verifier" in provider.system
        assert "OUTPUT FORMAT" in provider.system
        assert set(provider.tools) == set(VERIFICATION_AGENT_ALLOWED_TOOLS)
        assert "write_file" not in provider.tools
        assert "edit_file" not in provider.tools

        assert handle.sidechain_path is not None
        sidechain = read_sidechain(handle.sidechain_path)
        start = next(
            record for record in sidechain
            if record.get("type") == "sidechain_start")
        assert start["parent_history_inherited"] is False
        await manager.shutdown()

    asyncio.run(go())
