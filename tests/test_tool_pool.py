"""单轮工具池：来源元数据、意图过滤和动态注册边界。"""

from pathlib import Path

from krelcode.agent.intent import MODE_EXPLAIN_FILES, TurnIntent
from krelcode.agent.tool_pool import TurnToolPool
from krelcode.tools.base import ToolDescriptor, ToolResult, ToolSource
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.builtin.time_tool import GetTimeTool
from krelcode.tools.registry import ToolRegistry


class DummyMcpTool:
    name = "srv__echo"
    description = "echo"
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
    registry.register(
        ReadFileTool(),
        ToolDescriptor(source=ToolSource.SKILL),
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
