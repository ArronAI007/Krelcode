"""单轮工具池：来源元数据、意图过滤和动态注册边界。"""

from pathlib import Path

from krelcode.agent.intent import MODE_EXPLAIN_FILES, TurnIntent
from krelcode.agent.tool_pool import TurnToolPool
from krelcode.tools.base import (
    PermissionMode,
    RunMode,
    RuntimeSnapshot,
    ToolDescriptor,
    ToolResult,
    ToolSource,
)
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.builtin.time_tool import GetTimeTool
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
