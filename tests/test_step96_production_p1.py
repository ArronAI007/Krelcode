"""P1 production-readiness tests: MCP, commands, hooks and subagents."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

import krelcode.hooks as hooks_module
from krelcode.core.messages import Message, ToolCall
from krelcode.core.redact import SecretRedactor
from krelcode.hooks import HookRunner, parse_hooks
from krelcode.llm.base import (
    ProviderCapabilities,
    StreamFinished,
    TextDelta,
)
from krelcode.mcp.client import MCPClient, MCPError, MCPServerConfig
from krelcode.mcp.oauth import OAuthTokenManager
from krelcode.mcp.supervisor import MCPSupervisor
from krelcode.mcp.tools import MCPListResourceTemplatesTool, discover_tools
from krelcode.mcp.transport import MCPAuthRequired
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands import (
    CommandRequest,
    CommandSource,
    builtin_command_registry,
    sync_custom_commands,
)
from krelcode.session.commands.custom import CustomCommandManager
from krelcode.session.commands.handlers.runtime_ops import (
    handle_custom_command,
    handle_mcp,
)
from krelcode.subagent.definition import AgentDefinitionManager
from krelcode.subagent.manager import SubagentManager
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


class PagedClient(MCPClient):
    def __init__(self) -> None:
        self.name = "paged"
        self.calls: List[tuple[str, Dict[str, Any]]] = []

    async def _request(self, method: str, params: Dict[str, Any] | None = None):
        params = dict(params or {})
        self.calls.append((method, params))
        cursor = params.get("cursor")
        pages = {
            ("tools/list", None): {
                "tools": [{"name": "one"}], "nextCursor": "t2"},
            ("tools/list", "t2"): {
                "tools": [{"name": "two"}], "nextCursor": ""},
            ("resources/list", None): {
                "resources": [{"uri": "krel://a"}], "nextCursor": "r2"},
            ("resources/list", "r2"): {"resources": [{"uri": "krel://b"}]},
            ("resources/templates/list", None): {
                "resourceTemplates": [{"uriTemplate": "krel://{id}"}],
                "nextCursor": "q2",
            },
            ("resources/templates/list", "q2"): {
                "resourceTemplates": [{"uriTemplate": "file://{path}"}]},
            ("prompts/list", None): {
                "prompts": [{"name": "p1"}], "nextCursor": "p2"},
            ("prompts/list", "p2"): {"prompts": [{"name": "p2"}]},
        }
        return pages[(method, cursor)]


def test_mcp_paginated_lists_follow_all_cursors():
    async def go():
        client = PagedClient()
        assert [item["name"] for item in await client.list_tools()] == [
            "one", "two"]
        assert [item["uri"] for item in await client.list_resources()] == [
            "krel://a", "krel://b"]
        assert [item["uriTemplate"] for item in
                await client.list_resource_templates()] == [
            "krel://{id}", "file://{path}"]
        assert [item["name"] for item in await client.list_prompts()] == [
            "p1", "p2"]

    asyncio.run(go())


def test_mcp_pagination_rejects_repeated_cursor():
    class Repeated(PagedClient):
        async def _request(self, method, params=None):
            return {"tools": [], "nextCursor": "same"}

    async def go():
        with pytest.raises(MCPError, match="cursor 重复"):
            await Repeated().list_tools()

    asyncio.run(go())


class FakeMCPClient:
    name = "fake"
    alive = True
    capabilities = {"tools": {}, "resources": {}, "prompts": {}}
    server_info = {"name": "fake-server"}

    def __init__(self) -> None:
        self.tool_version = 1

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    async def list_tools(self):
        return [{
            "name": f"tool{self.tool_version}",
            "description": "remote",
            "inputSchema": {"type": "object", "properties": {}},
        }]

    async def list_resources(self):
        return [{"uri": "krel://one", "name": "one"}]

    async def list_resource_templates(self):
        return [{"uriTemplate": "krel://{id}", "name": "item"}]

    async def list_prompts(self):
        return [{"name": f"prompt{self.tool_version}"}]


def test_mcp_list_changed_refreshes_tools_resources_prompts_and_commands():
    async def go():
        registry = ToolRegistry()
        supervisor = MCPSupervisor(registry, log=lambda _line: None)
        client = FakeMCPClient()
        supervisor.clients["fake"] = client
        supervisor.configs["fake"] = MCPServerConfig(name="fake", url="https://x")
        refreshed: list[bool] = []

        async def changed() -> None:
            refreshed.append(True)

        supervisor.on_prompts_changed = changed
        await supervisor.refresh_metadata("fake")
        initial = registry.names()
        assert "fake__tool1" in initial
        assert "fake__list_resource_templates" in initial
        assert supervisor.resources["fake"][0]["uri"] == "krel://one"
        assert supervisor.resource_templates["fake"][0]["name"] == "item"

        client.tool_version = 2
        await supervisor._handle_notification(
            "fake", "notifications/tools/list_changed", {})
        assert "fake__tool2" in registry.names()
        assert "fake__tool1" not in registry.names()
        await supervisor._handle_notification(
            "fake", "notifications/prompts/list_changed", {})
        assert supervisor.prompts["fake"][0]["name"] == "prompt2"
        assert refreshed

    asyncio.run(go())


def test_resource_template_tool_renders_and_returns_count():
    class Client:
        name = "templates"

        async def list_resource_templates(self):
            return [{
                "uriTemplate": "repo://{path}",
                "name": "repo file",
                "mimeType": "text/plain",
                "description": "read a repo file",
            }]

    async def go():
        result = await MCPListResourceTemplatesTool(Client()).execute(
            {}, ToolContext(workspace_root=Path(".")))
        assert not result.is_error
        assert "repo://{path}" in result.content
        assert result.data["count"] == 1

    asyncio.run(go())


def test_oauth_refresh_persists_private_token_cache(tmp_path):
    requests: list[tuple[str, str, Dict[str, str], bytes | None]] = []

    def request(method, url, headers, body, timeout):
        requests.append((method, url, headers, body))
        return 200, {"content-type": "application/json"}, (
            b'{"access_token":"fresh-token","refresh_token":"next-refresh",'
            b'"expires_in":3600,"token_type":"Bearer"}')

    async def go():
        manager = OAuthTokenManager(
            "remote",
            "https://example.test/mcp",
            {
                "refresh_token": "old-refresh",
                "token_url": "https://auth.example.test/token",
                "client_id": "client",
                "client_secret": "secret",
            },
            token_path=tmp_path / "oauth.json",
            request=request,
        )
        headers = await manager.headers()
        assert headers == {"Authorization": "Bearer fresh-token"}
        record = (tmp_path / "oauth.json").read_text(encoding="utf-8")
        assert "fresh-token" in record and "next-refresh" in record
        assert stat.S_IMODE((tmp_path / "oauth.json").stat().st_mode) == 0o600
        assert requests and requests[0][1] == "https://auth.example.test/token"
        assert requests[0][2]["Authorization"].startswith("Basic ")

    asyncio.run(go())


def test_oauth_protected_resource_discovery_uses_well_known_metadata():
    def request(method, url, headers, body, timeout):
        if "oauth-protected-resource" in url:
            return 200, {}, (
                b'{"resource":"https://example.test/mcp",'
                b'"authorization_servers":["https://auth.example.test"],'
                b'"scopes_supported":["mcp.read"]}')
        if "oauth-authorization-server" in url:
            return 200, {}, (
                b'{"authorization_endpoint":"https://auth.example.test/authorize",'
                b'"token_endpoint":"https://auth.example.test/token",'
                b'"registration_endpoint":"https://auth.example.test/register"}')
        return 404, {}, b""

    async def go():
        manager = OAuthTokenManager(
            "remote", "https://example.test/mcp", {"enabled": True},
            token_path=Path("/tmp/krelcode-unused-oauth-test.json"),
            request=request,
        )
        metadata = await manager.discover()
        assert metadata.authorization_url.endswith("/authorize")
        assert metadata.token_url.endswith("/token")
        assert metadata.registration_url.endswith("/register")
        assert metadata.scopes == ("mcp.read",)

    asyncio.run(go())


def test_mcp_oauth_401_refreshes_and_retries_request_once(tmp_path):
    class AuthRetryTransport:
        kind = "streamable-http"
        alive = True

        def __init__(self):
            self.headers = {}
            self.payloads = []
            self.client = None

        def set_headers(self, headers):
            self.headers = dict(headers or {})

        async def send(self, payload):
            self.payloads.append((dict(self.headers), payload))
            if len(self.payloads) == 1:
                raise MCPAuthRequired(
                    "Unauthorized",
                    status=401,
                    headers={
                        "www-authenticate": 'Bearer resource_metadata="https://resource.test/meta"',
                    },
                )
            asyncio.get_running_loop().call_soon(
                self.client._dispatch,
                {"jsonrpc": "2.0", "id": payload["id"], "result": {"ok": True}},
            )

    def request(method, url, headers, body, timeout):
        assert url == "https://auth.example.test/token"
        return 200, {}, b'{"access_token":"new-access","refresh_token":"new-refresh"}'

    async def go():
        transport = AuthRetryTransport()
        client = MCPClient(
            MCPServerConfig(name="remote", url="https://mcp.example.test/mcp"),
            transport=transport,
        )
        transport.client = client
        client.oauth = OAuthTokenManager(
            "remote",
            "https://mcp.example.test/mcp",
            {
                "refresh_token": "old-refresh",
                "token_url": "https://auth.example.test/token",
            },
            token_path=tmp_path / "oauth-retry.json",
            request=request,
        )
        client._started = True

        assert await client._request("tools/list") == {"ok": True}
        assert len(transport.payloads) == 2
        assert "Authorization" not in transport.payloads[0][0]
        assert transport.payloads[1][0]["Authorization"] == "Bearer new-access"

    asyncio.run(go())


def test_mcp_auth_command_delegates_to_supervisor():
    class Supervisor:
        def __init__(self):
            self.authorized = []

        async def authorize(self, name):
            self.authorized.append(name)

        def status(self):
            return []

    supervisor = Supervisor()
    registry = builtin_command_registry()
    context = SimpleNamespace(
        request=CommandRequest("mcp", "auth remote", "/mcp auth remote"),
        mcp_supervisor=supervisor,
        mcp_reload=None,
        command_registry=registry,
    )
    result = asyncio.run(handle_mcp(context))
    assert supervisor.authorized == ["remote"]
    assert any("授权成功" in line for line in result.output)


def test_custom_command_project_override_execution_and_source(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    user_dir = home / ".krelcode" / "commands"
    project_dir = workspace / ".krelcode" / "commands"
    user_dir.mkdir(parents=True)
    project_dir.mkdir(parents=True)
    (user_dir / "review.md").write_text(
        "---\nname: review\ndescription: user review\n---\nUSER $ARGUMENTS\n",
        encoding="utf-8",
    )
    (project_dir / "review.md").write_text(
        "---\nname: review\ndescription: project review\n"
        "argument-hint: <path>\nallowed-tools: [read_file]\n---\n"
        "Review $ARGUMENTS carefully.\n",
        encoding="utf-8",
    )
    manager = CustomCommandManager(workspace, home=home)
    registry = builtin_command_registry()
    sync_custom_commands(registry, manager)
    spec = registry.resolve("/review")
    assert spec is not None and spec.source is CommandSource.CUSTOM
    assert spec.description == "project review"

    context = SimpleNamespace(
        request=CommandRequest("review", "src/app.py", "/review src/app.py"),
        loop=SimpleNamespace(
            services=SimpleNamespace(
                extensions=SimpleNamespace(custom_commands=manager),
            ),
        ),
    )

    async def go():
        result = await handle_custom_command(context)
        assert result.handled
        assert "Review src/app.py carefully." in result.run_prompt
        assert result.tool_allowlist == ("read_file",)

    asyncio.run(go())


def test_custom_command_reload_picks_up_files_and_isolates_invalid_entries(tmp_path):
    commands = tmp_path / ".krelcode" / "commands"
    commands.mkdir(parents=True)
    path = commands / "deploy.md"
    path.write_text(
        "---\nname: deploy\ndescription: deploy command\n---\n"
        "Run the first deployment procedure.\n", encoding="utf-8")
    manager = CustomCommandManager(tmp_path, home=tmp_path / "home")
    assert manager.get("deploy").prompt.startswith("Run the first")

    path.write_text(
        "---\nname: deploy\ndescription: deploy command\n---\n"
        "Run the updated deployment procedure.\n", encoding="utf-8")
    (commands / "bad.md").write_text("---\nname: ../escape\n---\nBad\n", encoding="utf-8")
    manager.reload()
    assert manager.get("deploy").prompt.startswith("Run the updated")
    assert manager.get("bad") is None
    assert manager.issues and manager.issues[0].path.name == "bad.md"


def test_http_hook_uses_environment_header_and_redacts_secret(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HOOK_TEST_TOKEN", "top-secret-token")
    seen: Dict[str, Any] = {}

    def fake_http(url, headers, body, timeout):
        seen.update(url=url, headers=headers, body=body, timeout=timeout)
        return 0, (
            '{"decision":"block","reason":"policy top-secret-token"}'
        ), ""

    monkeypatch.setattr(hooks_module, "_post_hook_http", fake_http)
    specs = parse_hooks({"hooks": [{
        "on": "pre_tool_use",
        "type": "http",
        "url": "https://user:password@hooks.example.test/check?token=target-secret",
        "headers": {"Authorization": "Bearer ${HOOK_TEST_TOKEN}"},
    }]})
    redactor = SecretRedactor(["top-secret-token"])
    runner = HookRunner(
        tmp_path, specs, log=lambda _line: None, redactor=redactor)
    result = asyncio.run(runner.before_tool(ToolCall("t1", "write_file", {"path": "x"})))
    assert result.blocked and result.reason == "policy ***"
    assert seen["headers"]["Authorization"] == "Bearer top-secret-token"
    assert "top-secret-token" not in str(runner.executions)
    assert seen["url"].startswith("https://user:password@hooks.example.test/check")
    assert runner.executions[0]["target"] == "https://hooks.example.test/check"
    assert "target-secret" not in str(runner.executions)


def test_http_hook_parser_rejects_non_http_transport(tmp_path):
    specs = parse_hooks({"hooks": [
        {"on": "pre_tool_use", "type": "http", "url": "file:///tmp/hook"},
        {"on": "pre_tool_use", "type": "prompt"},
    ]})
    assert specs == []


def test_mcp_config_expands_environment_secrets(monkeypatch):
    from krelcode.mcp.supervisor import config_from_spec

    monkeypatch.setenv("MCP_TEST_TOKEN", "header-secret")
    monkeypatch.setenv("MCP_TEST_CLIENT_SECRET", "oauth-secret")
    cfg = config_from_spec("remote", {
        "url": "https://mcp.example.test/mcp",
        "headers": {"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
        "oauth": {"client_secret": "${MCP_TEST_CLIENT_SECRET}"},
    })
    assert cfg.headers["Authorization"] == "Bearer header-secret"
    assert cfg.oauth["client_secret"] == "oauth-secret"
    assert "header-secret" not in repr(cfg)
    assert "oauth-secret" not in repr(cfg)


def test_prompt_hook_and_plugin_hook_type_share_decision_pipeline(tmp_path):
    calls: list[str] = []

    async def prompt_runner(prompt: str, payload: Dict[str, Any]) -> str:
        calls.append(prompt)
        return '{"additional_context":"Use the project policy"}'

    specs = parse_hooks({"hooks": [{
        "on": "user_input",
        "type": "prompt",
        "prompt": "Check this request: {prompt}",
        "_source": "plugin:guard@1.0.0",
    }]})
    runner = HookRunner(
        tmp_path, specs, log=lambda _line: None, prompt_runner=prompt_runner)
    context = asyncio.run(runner.collect_input_context("change auth"))
    assert context == ("Use the project policy",)
    assert calls and "<hook_input>" in calls[0]
    assert runner.executions[0]["kind"] == "prompt"
    assert runner.executions[0]["source"] == "plugin:guard@1.0.0"


class CaptureProvider:
    name = "parent"
    model = "parent"
    capabilities = ProviderCapabilities()

    def __init__(self, name: str = "parent", model: str = "parent") -> None:
        self.name = name
        self.model = model
        self.last_tools: list[str] = []

    async def stream(self, messages, tools=None):
        self.last_tools = [
            str((item or {}).get("name")) for item in (tools or [])]
        yield TextDelta("done")
        yield StreamFinished(
            message=Message.assistant("done"), finish_reason="stop", usage={})


class StaticTool:
    description = "static"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, name: str, *, read_only: bool, client: Any = None):
        self.name = name
        self.read_only = read_only
        self.client = client

    async def execute(self, args, ctx):
        return ToolResult(content=self.name)


def test_subagent_definition_model_permission_and_mcp_collection(tmp_path):
    home = tmp_path / "home"
    agents = home / ".krelcode" / "agents"
    agents.mkdir(parents=True)
    (agents / "reader.md").write_text(
        "---\nname: reader\ndescription: read only\n"
        "model: child/model-x\npermission-mode: read-only\n"
        "mcp-servers: [alpha]\n---\nRead only.\n",
        encoding="utf-8",
    )
    parent_provider = CaptureProvider()
    child_provider = CaptureProvider("child", "model-x")
    resolved: list[str] = []

    def resolver(spec: str):
        resolved.append(spec)
        return child_provider

    tools = [
        StaticTool("read_file", read_only=True),
        StaticTool("write_file", read_only=False),
        StaticTool("alpha__search", read_only=False,
                   client=SimpleNamespace(name="alpha")),
        StaticTool("beta__search", read_only=False,
                   client=SimpleNamespace(name="beta")),
    ]
    manager = SubagentManager(
        llm=parent_provider,
        tool_factory=lambda: list(tools),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        definitions=AgentDefinitionManager(tmp_path, home=home),
        llm_resolver=resolver,
        mcp_servers_provider=lambda: ("alpha", "beta"),
    )

    async def go():
        handle = await manager.spawn("inspect", definition="reader")
        assert resolved == ["child/model-x"]
        assert handle.loop.llm is child_provider
        assert handle.loop.tool_context.runtime.permission_mode.value == "read-only"
        assert "beta__search" not in handle.loop.registry.names()
        await manager.wait(handle.id, timeout=5)
        assert "read_file" in child_provider.last_tools
        assert "write_file" not in child_provider.last_tools
        assert "alpha__search" not in child_provider.last_tools  # read-only mode
        await manager.shutdown()

    asyncio.run(go())


def test_subagent_definition_rejects_unknown_mcp_server(tmp_path):
    home = tmp_path / "home"
    agents = home / ".krelcode" / "agents"
    agents.mkdir(parents=True)
    (agents / "bad.md").write_text(
        "---\nname: bad\ndescription: bad mcp\nmcp-servers: [missing]\n---\nBad.\n",
        encoding="utf-8",
    )
    manager = SubagentManager(
        llm=CaptureProvider(),
        tool_factory=list,
        tool_context=ToolContext(workspace_root=tmp_path),
        definitions=AgentDefinitionManager(tmp_path, home=home),
        mcp_servers_provider=lambda: ("alpha",),
    )

    async def go():
        with pytest.raises(ValueError, match="未知 MCP 服务器"):
            await manager.spawn("bad", definition="bad")
        await manager.shutdown()

    asyncio.run(go())


def test_subagent_model_resolver_reads_latest_parent_provider(monkeypatch):
    import krelcode.app as app_module
    import krelcode.bootstrap.composition.subagents as resolver_module

    parent_state = SimpleNamespace(llm=CaptureProvider("first", "parent-1"))
    child = CaptureProvider("child", "model-x")
    child.api_key = "child-key"
    resolved = []

    def resolve_settings(*, provider, model, base_url, config):
        resolved.append((provider, model, base_url, config))
        return {"provider": provider, "model": model}

    monkeypatch.setattr(resolver_module, "resolve_settings", resolve_settings)
    monkeypatch.setattr(
        resolver_module, "create_provider", lambda _settings: child)
    resolver = app_module.build_subagent_llm_resolver(
        SimpleNamespace(base_url="https://api.example.test", retries=0),
        {"providers": {}},
        lambda: parent_state.llm,
        SecretRedactor(),
    )

    parent_state.llm = CaptureProvider("second", "parent-2")
    assert resolver("model-x") is child
    assert resolved[0][0:2] == ("second", "model-x")
