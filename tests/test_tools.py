"""tools 层测试：注册表 schema 生成、get_time 工具（不碰 LLM）。"""

import asyncio
import re
from pathlib import Path

from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.time_tool import GetTimeTool
from krelcode.tools.registry import ToolRegistry

CTX = ToolContext(workspace_root=Path("."))


def test_registry_unified_tool_spec():
    reg = ToolRegistry([GetTimeTool()])
    # 统一 ToolSpec：不带任何 provider 专属包装，转换是 adapter 的职责
    assert reg.tool_specs() == [{
        "name": "get_time",
        "description": GetTimeTool.description,
        "parameters": GetTimeTool.parameters,
    }]
    assert reg.names() == ["get_time"]
    assert reg.get("get_time") is not None
    assert reg.get("nope") is None


def test_registry_rejects_duplicate():
    reg = ToolRegistry()
    reg.register(GetTimeTool())
    try:
        reg.register(GetTimeTool())
        assert False, "应抛出重复注册错误"
    except ValueError:
        pass


def test_get_time_returns_iso_string():
    result = asyncio.run(GetTimeTool().execute({}, CTX))
    assert not result.is_error
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", result.content)


def test_get_time_with_timezone_and_bad_timezone():
    ok = asyncio.run(GetTimeTool().execute({"timezone": "UTC"}, CTX))
    assert not ok.is_error and ok.data["tz"] == "UTC"

    bad = asyncio.run(GetTimeTool().execute({"timezone": "Mars/Olympus"}, CTX))
    assert bad.is_error and "未知时区" in bad.content
