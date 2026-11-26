"""Unified governance decision and distributed enforcement consumers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompts import resolve_prompt_profile, select_prompt_release
from krelcode.contracts.governance import (
    ArtifactIdentity,
    AuditCorrelation,
    ControlPlanePort,
    GovernanceDecision,
    GovernanceRequest,
    PluginTrustContext,
    PolicyVersion,
    PromptReleaseIdentity,
    SandboxProfile,
)
from krelcode.contracts.runtime import PermissionMode, RuntimeSnapshot
from krelcode.core.errors import ConfigError
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.plugins import PluginManager, PluginSignatureError
from krelcode.safety.control_plane import DefaultControlPlane
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


class Identity:
    subject = "user-1"
    issuer = "https://idp.example"
    display_name = "User"
    email_hash = "hash"
    expires_at = 4_102_444_800.0
    auth_method = "oidc"


class Policy:
    enabled = True
    organization = "acme"
    policy_version = "2026.11"
    source = "managed:/etc/krelcode.toml"
    require_sso = True
    max_permission_mode = PermissionMode.DEFAULT
    allow_auto_approve = False
    allow_no_sandbox = False
    disable_plugins = True
    allowed_tools = ("read_file", "echo")
    disabled_tools = ("delete_repo",)

    def clamp_permission_mode(
        self, mode: PermissionMode | str,
    ) -> PermissionMode:
        requested = PermissionMode(mode)
        if requested is PermissionMode.AUTO_APPROVE:
            return PermissionMode.DEFAULT
        return requested

    def deny_tool_reason(
        self, tool_call: object, workspace_root: Path,
    ) -> str | None:
        del workspace_root
        if getattr(tool_call, "name", "") == "delete_repo":
            return "企业策略禁止 delete_repo"
        return None

    def audit_metadata(self) -> dict[str, object]:
        return {
            "organization": self.organization,
            "policy_version": self.policy_version,
            "source": self.source,
            "require_sso": self.require_sso,
            "allow_auto_approve": self.allow_auto_approve,
            "allow_no_sandbox": self.allow_no_sandbox,
        }


class CaptureGovernanceTool:
    name = "capture_governance"
    description = "capture governance decision"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    def __init__(self) -> None:
        self.decision: GovernanceDecision | None = None

    async def execute(self, args, ctx: ToolContext) -> ToolResult:
        del args
        self.decision = ctx.governance_decision
        return ToolResult(content="captured")


class DenyControlPlane:
    def __init__(self, reason: str = "denied by control plane") -> None:
        self.reason = reason
        self.requests: list[GovernanceRequest] = []

    async def evaluate(self, request: GovernanceRequest) -> GovernanceDecision:
        self.requests.append(request)
        return GovernanceDecision(
            allowed=False,
            reason=self.reason,
            permission_mode=request.runtime.permission_mode,
            audit=request.audit,
        )


def make_loop(
    tmp_path: Path,
    tool: object,
    *,
    control_plane: ControlPlanePort | None = None,
) -> AgentLoop:
    loop = AgentLoop(
        llm=MockProvider(scripted=[
            Message.assistant(tool_calls=[
                ToolCall("call-1", getattr(tool, "name"), {}),
            ]),
            "done",
        ]),
        registry=ToolRegistry([tool]),
        tool_context=ToolContext(
            workspace_root=tmp_path,
            sandbox_enabled=False,
        ),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    if control_plane is not None:
        loop.dependencies.control_plane = control_plane
    return loop


def test_control_plane_allows_and_normalizes_all_inputs() -> None:
    request = GovernanceRequest(
        identity=Identity(),
        enterprise_policy=Policy(),
        runtime=RuntimeSnapshot(
            permission_mode=PermissionMode.AUTO_APPROVE,
            tool_allowlist=frozenset({"echo"}),
        ),
        action="tool.execute",
        resource="tool",
        tool_name="echo",
        workspace_root=Path("."),
        sandbox_profile=SandboxProfile(enabled=True),
        plugin_trust=PluginTrustContext(),
        prompt_release=PromptReleaseIdentity(
            release_id="release-1",
            release_hash="hash-1",
            approved=True,
            required=True,
        ),
        artifacts=(
            ArtifactIdentity(
                kind="binary",
                id="krelcode",
                version="0.81.0",
                digest="sha256:ok",
            ),
        ),
        audit=AuditCorrelation(
            session_id="session-1",
            turn_id="turn-1",
            request_id="request-1",
        ),
    )

    decision = asyncio.run(DefaultControlPlane().evaluate(request))

    assert decision.allowed
    assert decision.permission_mode is PermissionMode.DEFAULT
    assert decision.identity_subject == "user-1"
    assert decision.tool_allowlist == frozenset({"echo"})
    assert decision.policy_version.organization == "acme"
    assert decision.policy_version.version == "2026.11"
    assert decision.audit.correlation_id == "session-1:turn-1:request-1"
    assert decision.decision_hash


def test_control_plane_combines_all_governance_failures() -> None:
    request = GovernanceRequest(
        identity=Identity(),
        enterprise_policy=Policy(),
        runtime=RuntimeSnapshot(
            permission_mode=PermissionMode.DEFAULT,
            tool_allowlist=frozenset({"read_file"}),
        ),
        tool_name="delete_repo",
        workspace_root=Path("."),
        sandbox_profile=SandboxProfile(enabled=False),
        plugin_trust=PluginTrustContext(
            name="unsafe-plugin",
            required=True,
            allowed=False,
            trust_status="invalid",
        ),
        prompt_release=PromptReleaseIdentity(
            release_id="release-1",
            release_hash="wrong",
            approved=False,
            required=True,
            expected_release_id="release-2",
            expected_release_hash="expected",
        ),
        artifacts=(
            ArtifactIdentity(
                kind="model",
                id="weights",
                version="",
                digest="sha256:actual",
                expected_digest="sha256:expected",
            ),
        ),
    )

    decision = asyncio.run(DefaultControlPlane().evaluate(request))

    assert not decision.allowed
    for expected in (
        "allowlist",
        "企业策略禁止 delete_repo",
        "禁止关闭 Sandbox",
        "禁用插件",
        "插件信任",
        "Prompt release",
        "Artifact",
    ):
        assert expected in decision.reason


def test_agent_tool_consumes_control_plane_decision(tmp_path: Path) -> None:
    tool = CaptureGovernanceTool()
    loop = make_loop(tmp_path, tool)

    assert asyncio.run(loop.run("capture")) == "done"
    assert tool.decision is not None
    assert tool.decision.allowed
    assert loop.history[3].content == "captured"


def test_agent_tool_stops_before_execution_when_governance_denies(
    tmp_path: Path,
) -> None:
    tool = CaptureGovernanceTool()
    control_plane = DenyControlPlane()
    loop = make_loop(tmp_path, tool, control_plane=control_plane)

    assert asyncio.run(loop.run("capture")) == "done"
    assert tool.decision is None
    assert control_plane.requests
    assert "治理控制面拒绝工具执行" in loop.history[3].content


def test_prompt_release_execution_point_consumes_decision() -> None:
    release = select_prompt_release()
    decision = GovernanceDecision(
        allowed=True,
        prompt_release=PromptReleaseIdentity(
            release_id=release.release_id,
            release_hash=release.release_hash,
            required=True,
            expected_release_id="other-release",
        ),
    )
    with pytest.raises(ConfigError, match="治理决策"):
        resolve_prompt_profile(
            "openai", "gpt-4o", governance_decision=decision)

    accepted = GovernanceDecision(
        allowed=True,
        prompt_release=PromptReleaseIdentity(
            release_id=release.release_id,
            release_hash=release.release_hash,
            required=True,
            expected_release_id=release.release_id,
            expected_release_hash=release.release_hash,
        ),
    )
    resolution = resolve_prompt_profile(
        "openai", "gpt-4o", governance_decision=accepted)
    assert resolution.release_id == release.release_id


def test_plugin_install_execution_point_consumes_decision(tmp_path: Path) -> None:
    plugin = tmp_path / "demo"
    plugin.mkdir()
    (plugin / "plugin.toml").write_text(
        'name = "demo"\nversion = "1.0.0"\n'
        'entrypoint = "plugin:tools"\n',
        encoding="utf-8",
    )
    (plugin / "plugin.py").write_text(
        "def tools():\n    return []\n",
        encoding="utf-8",
    )
    manager = PluginManager(tmp_path, home=tmp_path / "home")
    denied = GovernanceDecision(
        allowed=False,
        reason="managed policy denied",
        plugin_trust=PluginTrustContext(
            name="demo", required=True, allowed=False),
    )

    with pytest.raises(PluginSignatureError, match="治理控制面"):
        asyncio.run(manager.install(
            plugin, governance_decision=denied))
