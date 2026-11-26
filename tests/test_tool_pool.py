"""单轮工具池：来源元数据、意图过滤和动态注册边界。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.intent import MODE_EXPLAIN_FILES, TurnIntent
from krelcode.agent.tool_pool import TurnToolPool
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers.runtime_ops import handle_tools
from krelcode.session.commands.registry import CommandRegistry
from krelcode.session.commands.types import CommandRequest
from krelcode.tools.base import (
    PermissionMode,
    RunMode,
    RuntimeSnapshot,
    ToolContext,
    ToolDescriptor,
    ToolResult,
    ToolSource,
    ToolTier,
)
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.builtin.repo_map import RepoMapTool
from krelcode.tools.builtin.time_tool import GetTimeTool
from krelcode.tools.builtin.tool_search import ToolSearchTool
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.tools.registry import ToolRegistry


class DummyMcpTool:
    name = "srv__echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args, ctx):
        return ToolResult(content="ok")


class FeatureTool:
    name = "feature_tool"
    description = "feature-gated"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args, ctx):
        return ToolResult(content="ok")


def test_registry_preserves_and_clears_tool_provenance():
    tool = DummyMcpTool()
    registry = ToolRegistry()
    registry.register(
        tool,
        ToolDescriptor(source=ToolSource.MCP, mcp_server="srv"),
    )

    descriptor = registry.descriptor(tool.name)
    assert descriptor is not None
    assert descriptor.source is ToolSource.MCP
    assert descriptor.mcp_server == "srv"

    assert registry.unregister(tool.name)
    assert registry.descriptor(tool.name) is None


def test_turn_pool_filters_tools_and_keeps_registry_source():
    registry = ToolRegistry([GetTimeTool()])
    read_tool = ReadFileTool()
    registry.register(
        read_tool,
        ToolDescriptor.from_tool(read_tool, source=ToolSource.SKILL),
    )
    intent = TurnIntent(
        mode=MODE_EXPLAIN_FILES,
        target_files=(Path("target.py"),),
    )

    pool = TurnToolPool(registry, intent=intent)

    assert pool.names() == ["read_file"]
    assert [spec["name"] for spec in pool.tool_specs()] == ["read_file"]
    assert pool.get("get_time") is None
    assert pool.excluded_reason("get_time") == "本轮请求意图限制了可用工具"
    assert pool.descriptor("read_file").source is ToolSource.SKILL


def test_new_turn_pool_excludes_unregistered_mcp_tools():
    tool = DummyMcpTool()
    registry = ToolRegistry()
    registry.register(
        tool,
        ToolDescriptor(source=ToolSource.MCP, mcp_server="srv"),
    )
    assert TurnToolPool(registry).names() == ["srv__echo"]

    registry.unregister_prefix("srv__")
    pool = TurnToolPool(registry)

    assert pool.names() == []
    assert pool.get("srv__echo") is None


def test_tool_descriptor_and_system_hint_expose_side_effect_class():
    registry = ToolRegistry([ReadFileTool(), WriteFileTool()])
    pool = TurnToolPool(registry)

    assert pool.descriptor("read_file").read_only is True
    assert pool.descriptor("write_file").read_only is False
    hint = pool.system_hint()
    assert "read_file [read-only]" in hint
    assert "write_file [mutating]" in hint
    assert "calls may run in parallel" in hint


def test_permission_mode_read_only_and_plan_exclude_mutating_tools():
    registry = ToolRegistry([ReadFileTool(), WriteFileTool()])

    for mode in (PermissionMode.READ_ONLY, PermissionMode.PLAN):
        pool = TurnToolPool(
            registry, runtime=RuntimeSnapshot(permission_mode=mode))
        assert pool.names() == ["read_file"]
        assert "只允许本地只读工具" in pool.excluded_reason("write_file")


def test_disabled_allowlist_feature_and_run_mode_filters_are_enforced():
    registry = ToolRegistry([ReadFileTool(), GetTimeTool()])
    registry.register(
        FeatureTool(),
        ToolDescriptor(
            required_feature="feature-x",
            run_modes=frozenset({RunMode.SDK}),
        ),
    )
    runtime = RuntimeSnapshot(
        run_mode=RunMode.REPL,
        disabled_tools=frozenset({"get_time"}),
        tool_allowlist=frozenset({"Read", "feature_tool"}),
    )
    pool = TurnToolPool(registry, runtime=runtime)

    assert pool.names() == ["read_file"]
    assert "配置" in pool.excluded_reason("get_time")
    assert "功能开关" in pool.excluded_reason("feature_tool")


def test_mcp_health_filter_only_applies_when_health_is_known():
    tool = DummyMcpTool()
    registry = ToolRegistry()
    registry.register(
        tool,
        ToolDescriptor(source=ToolSource.MCP, mcp_server="srv", read_only=True),
    )

    unknown = TurnToolPool(
        registry, runtime=RuntimeSnapshot(mcp_health_known=False))
    offline = TurnToolPool(
        registry,
        runtime=RuntimeSnapshot(
            mcp_health_known=True, alive_mcp_servers=frozenset()),
    )
    online = TurnToolPool(
        registry,
        runtime=RuntimeSnapshot(
            mcp_health_known=True, alive_mcp_servers=frozenset({"srv"})),
    )

    assert unknown.names() == ["srv__echo"]
    assert offline.names() == []
    assert "不在线" in offline.excluded_reason("srv__echo")
    assert online.names() == ["srv__echo"]


def test_remote_readonly_hint_does_not_satisfy_readonly_permission_mode():
    tool = DummyMcpTool()
    registry = ToolRegistry()
    registry.register(
        tool,
        ToolDescriptor(
            source=ToolSource.MCP,
            mcp_server="srv",
            read_only=True,
        ),
    )
    pool = TurnToolPool(
        registry,
        runtime=RuntimeSnapshot(
            permission_mode=PermissionMode.READ_ONLY,
            mcp_health_known=True,
            alive_mcp_servers=frozenset({"srv"}),
        ),
    )
    assert pool.names() == []


# -- Dynamic tool tiers / ToolSearch ------------------------------------------------


class CatalogTool:
    def __init__(
        self,
        name: str,
        tier,
        *,
        read_only: bool = True,
        search_terms=(),
    ) -> None:
        self.name = name
        self.description = f"{name} one line description"
        self.parameters = {"type": "object", "properties": {}}
        self.read_only = read_only
        self.tool_tier = tier
        self.search_terms = tuple(search_terms)
        self.calls = 0

    async def execute(self, args, ctx):
        self.calls += 1
        return ToolResult(content=f"{self.name} ok")


def _tier_registry(order):
    tools = {
        "core_b": CatalogTool("core_b", ToolTier.CORE),
        "core_a": CatalogTool("core_a", ToolTier.CORE),
        "discoverable_b": CatalogTool(
            "discoverable_b", ToolTier.DISCOVERABLE, search_terms=("report",)),
        "discoverable_a": CatalogTool(
            "discoverable_a", ToolTier.DISCOVERABLE),
        "deferred_b": CatalogTool("deferred_b", ToolTier.DEFERRED),
        "deferred_a": CatalogTool("deferred_a", ToolTier.DEFERRED),
    }
    return ToolRegistry([tools[name] for name in order]), tools


def test_tool_search_uses_ranked_partial_multi_token_matching():
    registry = ToolRegistry([
        ToolSearchTool(),
        RepoMapTool(),
        CatalogTool(
            "job_list",
            ToolTier.DISCOVERABLE,
            search_terms=("background", "process", "shell"),
        ),
        CatalogTool("unrelated_tool", ToolTier.DISCOVERABLE),
    ])
    pool = TurnToolPool(registry)

    matches = pool.search_catalog(
        "repo_map repository structure symbols code map", limit=4)
    assert matches[0].name == "repo_map"
    assert "repo_map" in {
        item.name for item in pool.search_catalog("repository structure")
    }
    assert pool.search_catalog("background process list")[0].name == "job_list"
    assert pool.search_catalog("helpful generic operation") == ()


def test_dynamic_catalog_has_three_tiers_and_stable_order():
    first, _ = _tier_registry([
        "deferred_b", "core_b", "discoverable_b",
        "core_a", "deferred_a", "discoverable_a",
    ])
    second, _ = _tier_registry([
        "discoverable_a", "core_a", "deferred_a",
        "discoverable_b", "core_b", "deferred_b",
    ])

    pool_a = TurnToolPool(first)
    pool_b = TurnToolPool(second)

    assert [spec["name"] for spec in pool_a.tool_specs()] == [
        "core_a", "core_b"]
    assert pool_a.tool_specs() == pool_b.tool_specs()
    assert pool_a.system_hint() == pool_b.system_hint()
    assert pool_a.system_hint().index("discoverable_a") < pool_a.system_hint().index(
        "discoverable_b")
    assert "deferred_a" not in pool_a.system_hint().split("</available_tools>")[0]
    assert "Deferred tools: 2" in pool_a.system_hint()
    assert [item.name for item in pool_a.search_catalog("*", limit=10)] == [
        "core_a", "core_b", "deferred_a", "deferred_b",
        "discoverable_a", "discoverable_b",
    ]


def test_tool_search_loads_schema_on_next_provider_step(tmp_path):
    search_tool = ToolSearchTool()
    deferred = CatalogTool("deferred_report", ToolTier.DEFERRED)
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[ToolCall(
            "search-1", "tool_search", {"query": "deferred_report"})]),
        Message.assistant(tool_calls=[ToolCall(
            "deferred-1", "deferred_report", {})]),
        "完成",
    ])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([search_tool, deferred]),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )

    assert asyncio.run(loop.run("加载延迟工具")) == "完成"
    assert deferred.calls == 1
    first_names = {spec["name"] for spec in provider.tools_seen[0]}
    second_names = {spec["name"] for spec in provider.tools_seen[1]}
    assert "deferred_report" not in first_names
    assert {"tool_search", "deferred_report"} <= second_names
    assert "deferred_report" in loop.history[3].content


def test_tool_pool_rebuild_preserves_loaded_eligible_tools():
    deferred = CatalogTool("deferred_report", ToolTier.DEFERRED)
    pool = TurnToolPool(ToolRegistry([ToolSearchTool(), deferred]))
    pool.load_tools(["deferred_report"])

    rebuilt = pool.rebuild(
        intent=None,
        runtime=RuntimeSnapshot(permission_mode=PermissionMode.AUTO_APPROVE),
    )

    assert rebuilt.is_loaded("deferred_report")
    assert "deferred_report" in rebuilt.loaded_names()


def test_tool_loaded_in_same_request_is_not_executable_until_next_step(tmp_path):
    deferred = CatalogTool("deferred_once", ToolTier.DEFERRED)
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[
            ToolCall("search-1", "tool_search", {"query": "deferred_once"}),
            ToolCall("deferred-1", "deferred_once", {}),
        ]),
        "完成",
    ])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([ToolSearchTool(), deferred]),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )

    assert asyncio.run(loop.run("测试同轮加载边界")) == "完成"
    assert deferred.calls == 0
    tool_results = [m.content for m in loop.history if m.role == "tool"]
    assert any("工具尚未加载" in text or "刚在本轮加载" in text
               for text in tool_results)


def test_tool_search_only_sees_tools_that_pass_all_runtime_filters():
    tools = [
        CatalogTool("core_read", ToolTier.CORE),
        CatalogTool("disabled_tool", ToolTier.DISCOVERABLE),
        CatalogTool("sdk_only", ToolTier.DISCOVERABLE),
        CatalogTool("feature_tool", ToolTier.DISCOVERABLE),
        CatalogTool("mutating_tool", ToolTier.DISCOVERABLE, read_only=False),
        CatalogTool("offline_remote", ToolTier.DEFERRED),
        CatalogTool("visible_tool", ToolTier.DISCOVERABLE),
    ]
    registry = ToolRegistry()
    registry.register(tools[0])
    registry.register(
        tools[1],
        ToolDescriptor(tier=ToolTier.DISCOVERABLE, read_only=True),
    )
    registry.register(
        tools[2],
        ToolDescriptor(
            tier=ToolTier.DISCOVERABLE,
            read_only=True,
            run_modes=frozenset({RunMode.SDK}),
        ),
    )
    registry.register(
        tools[3],
        ToolDescriptor(
            tier=ToolTier.DISCOVERABLE,
            read_only=True,
            required_feature="feature-x",
        ),
    )
    registry.register(
        tools[4],
        ToolDescriptor(tier=ToolTier.DISCOVERABLE, read_only=False),
    )
    registry.register(
        tools[5],
        ToolDescriptor(
            source=ToolSource.MCP,
            mcp_server="offline",
            tier=ToolTier.DEFERRED,
            read_only=True,
        ),
    )
    registry.register(tools[6])
    pool = TurnToolPool(registry, runtime=RuntimeSnapshot(
        run_mode=RunMode.REPL,
        permission_mode=PermissionMode.READ_ONLY,
        disabled_tools=frozenset({"disabled_tool"}),
        features=frozenset(),
        mcp_health_known=True,
        alive_mcp_servers=frozenset(),
    ))

    names = {item.name for item in pool.search_catalog("*", limit=10)}
    assert names == {"core_read", "visible_tool"}
    assert pool.load_tools(["disabled_tool", "offline_remote"]) == ()
    assert set(pool.names()) == {"core_read", "visible_tool"}


def test_mcp_and_plugin_descriptors_default_to_deferred():
    assert ToolDescriptor(source=ToolSource.MCP).tier is ToolTier.DEFERRED
    assert ToolDescriptor(source=ToolSource.PLUGIN).tier is ToolTier.DEFERRED


def test_tool_search_survives_allowlist_to_load_an_allowed_discoverable_tool():
    registry = ToolRegistry([
        ToolSearchTool(),
        CatalogTool("allowed_report", ToolTier.DISCOVERABLE),
        CatalogTool("blocked_core", ToolTier.CORE),
    ])
    pool = TurnToolPool(registry, runtime=RuntimeSnapshot(
        tool_allowlist=frozenset({"allowed_report"})))

    assert pool.names() == ["allowed_report", "tool_search"]
    assert [item.name for item in pool.search_catalog("allowed_report")] == [
        "allowed_report"]
    assert pool.load_tools(["allowed_report"]) == ("allowed_report",)
    assert [spec["name"] for spec in pool.tool_specs()] == [
        "allowed_report", "tool_search"]
    assert pool.load_tools(["blocked_core"]) == ()


@pytest.mark.parametrize(
    ("run_mode", "visible"),
    [
        (RunMode.REPL, False),
        (RunMode.PRINT, True),
        (RunMode.JSON, False),
        (RunMode.SUBAGENT, False),
    ],
)
def test_dynamic_catalog_applies_run_mode_filters(run_mode, visible):
    registry = ToolRegistry([CatalogTool("core_tool", ToolTier.CORE)])
    registry.register(
        CatalogTool("print_only", ToolTier.DISCOVERABLE),
        ToolDescriptor(
            tier=ToolTier.DISCOVERABLE,
            read_only=True,
            run_modes=frozenset({RunMode.PRINT}),
        ),
    )
    pool = TurnToolPool(registry, runtime=RuntimeSnapshot(run_mode=run_mode))

    assert ("print_only" in pool.names()) is visible
    assert ("print_only" in {
        item.name for item in pool.search_catalog("*", limit=10)
    }) is visible


def test_tools_command_reports_catalog_tier_loaded_and_excluded_states():
    registry = ToolRegistry([
        CatalogTool("core_tool", ToolTier.CORE),
        CatalogTool("compact_tool", ToolTier.DISCOVERABLE),
        CatalogTool("disabled_tool", ToolTier.DISCOVERABLE),
        ToolSearchTool(),
    ])
    pool = TurnToolPool(registry, runtime=RuntimeSnapshot(
        disabled_tools=frozenset({"disabled_tool"})))
    pool.load_tools(["compact_tool"])
    loop = SimpleNamespace(current_tool_pool=lambda: pool)
    context = CommandContext(
        request=CommandRequest(name="tools"),
        loop=loop,
        command_registry=CommandRegistry(),
        workspace_root=Path("."),
    )

    result = asyncio.run(handle_tools(context))
    text = "\n".join(result.output)
    assert "[core] core_tool:" in text
    assert "[loaded:discoverable] compact_tool:" in text
    assert "[excluded] disabled_tool:" in text
    assert "tool_search(query=..." in text
