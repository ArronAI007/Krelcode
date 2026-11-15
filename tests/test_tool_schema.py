"""工具参数 JSON Schema 校验与执行前失败关闭。"""

import asyncio
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry
from krelcode.tools.schema import validate_tool_arguments


def test_nested_object_required_type_enum_and_numeric_bounds():
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "mode": {"enum": ["read", "write"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "uniqueItems": True,
            },
            "meta": {
                "type": "object",
                "properties": {"enabled": {"type": "boolean"}},
                "required": ["enabled"],
                "additionalProperties": False,
            },
        },
        "required": ["path", "mode", "limit", "meta"],
        "additionalProperties": False,
    }

    valid = {
        "path": "src/a.py",
        "mode": "read",
        "limit": 3,
        "tags": ["a", "b"],
        "meta": {"enabled": True},
    }
    assert validate_tool_arguments(schema, valid) == []

    invalid = {
        "path": "",
        "mode": "delete",
        "limit": 11,
        "tags": ["a", "a"],
        "meta": {"enabled": "yes", "extra": 1},
    }
    rendered = "\n".join(
        error.render() for error in validate_tool_arguments(schema, invalid))
    assert "字符串长度" in rendered
    assert "枚举" in rendered
    assert "不能大于 10" in rendered
    assert "必须唯一" in rendered
    assert "$.meta.enabled" in rendered
    assert "额外字段" in rendered


def test_ref_combinators_and_array_items():
    schema = {
        "$defs": {
            "coordinate": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                },
                "required": ["x", "y"],
                "additionalProperties": False,
            },
        },
        "type": "object",
        "properties": {
            "points": {
                "type": "array",
                "items": {
                    "allOf": [
                        {"$ref": "#/$defs/coordinate"},
                        {"anyOf": [{"properties": {"x": {"minimum": 0}}},
                                   {"properties": {"y": {"minimum": 0}}}]},
                    ],
                },
            },
        },
        "required": ["points"],
    }
    assert validate_tool_arguments(schema, {"points": [{"x": 1, "y": 2}]}) == []
    errors = validate_tool_arguments(schema, {"points": [{"x": -1, "y": -2}]})
    assert any("anyOf" in error.message for error in errors)


class StrictTool:
    name = "strict"
    description = "strict schema"
    parameters = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 1},
        },
        "required": ["count"],
        "additionalProperties": False,
    }

    def __init__(self):
        self.executed = 0

    async def execute(self, args, ctx):
        self.executed += 1
        return ToolResult(content=str(args["count"]))


class SpyPolicy:
    def __init__(self):
        self.checked = 0

    async def check(self, tool_call, ctx):
        from krelcode.safety.policy import ALLOW
        self.checked += 1
        return ALLOW


def test_invalid_arguments_fail_before_policy_and_execute(tmp_path):
    tool = StrictTool()
    policy = SpyPolicy()
    bad_call = ToolCall("bad", "strict", {"count": 0, "extra": True})
    provider = MockProvider(scripted=[
        Message.assistant(tool_calls=[bad_call]),
        "recovered",
    ])
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry([tool]),
        tool_context=ToolContext(
            workspace_root=Path(tmp_path), sandbox_enabled=False),
        policy=policy,
        system_prompt="SYS",
    )

    assert asyncio.run(loop.run("validate")) == "recovered"
    assert tool.executed == 0
    assert policy.checked == 0
    result = loop.history[3].content
    assert "工具参数校验失败" in result
    assert "不能小于 1" in result
    assert "额外字段" in result
