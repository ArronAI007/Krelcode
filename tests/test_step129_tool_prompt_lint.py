"""Step 129: Tool Prompt lint and complete model-facing examples."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.app import default_tools
from krelcode.browser.runtime import BrowserOptions, BrowserRuntime
from krelcode.browser.tools import (
    BrowserClickTool,
    BrowserCloseTool,
    BrowserFillTool,
    BrowserOpenTool,
    BrowserScreenshotTool,
    BrowserSnapshotTool,
)
from krelcode.llm.providers.mock import MockProvider
from krelcode.lsp.tools import build_lsp_tools
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.controller import SessionController
from krelcode.skills.tools import build_skill_tools
from krelcode.subagent.tools import build_subagent_tools
from krelcode.tools.base import (
    ToolContext,
    ToolDescriptor,
    ToolResult,
    ToolSource,
)
from krelcode.tools.builtin.database_migration import (
    MigrationCreateTool,
    MigrationInspectTool,
    MigrationOptions,
    MigrationValidateTool,
)
from krelcode.session.memory_tool import MemorySearchTool
from krelcode.tools.builtin.notebook import (
    NotebookCreateTool,
    NotebookEditTool,
    NotebookInspectTool,
    NotebookOptions,
)
from krelcode.agent.prompt_lab_tool import PromptLabTool
from krelcode.tools.prompt import ToolPromptExample, ToolPromptSpec
from krelcode.tools.prompt_lint import (
    PromptLintSeverity,
    lint_tool_prompt_spec,
    lint_tool_registry,
)
from krelcode.tools.registry import ToolRegistry
from krelcode.web.tools import WebFetchTool, WebSearchTool


def _parameters(**properties: object) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": ["value"] if properties else [],
        "additionalProperties": False,
    }


def _valid_spec(**overrides: object) -> ToolPromptSpec:
    values: dict[str, object] = {
        "name": "inspect_value",
        "description": "Inspect one value.",
        "parameters": _parameters(value={"type": "string", "minLength": 1}),
        "instructions": "Read the single requested value and report evidence.",
        "when_to_use": ("A known value must be inspected.",),
        "when_not_to_use": ("The value location is unknown; search first.",),
        "examples": (
            ToolPromptExample(
                title="Inspect a known value",
                input={"value": "widget"},
                description="Pass only the value selected by the task.",
            ),
        ),
        "source": "builtin",
    }
    values.update(overrides)
    return ToolPromptSpec(**values)


def _codes(spec: ToolPromptSpec, *, source: str = "builtin") -> set[str]:
    return {
        issue.code for issue in lint_tool_prompt_spec(spec, source=source)
    }


def _all_known_tools():
    manager = object()
    browser = BrowserRuntime.from_options(BrowserOptions())
    notebook = NotebookOptions()
    migration = MigrationOptions()
    return [
        *default_tools(),
        MemorySearchTool(lambda: None),
        PromptLabTool(),
        *build_subagent_tools(manager),
        *build_lsp_tools(manager),
        WebFetchTool(manager),
        WebSearchTool(manager),
        *build_skill_tools(manager),
        BrowserOpenTool(browser),
        BrowserSnapshotTool(browser),
        BrowserClickTool(browser),
        BrowserFillTool(browser),
        BrowserScreenshotTool(browser),
        BrowserCloseTool(browser),
        NotebookInspectTool(notebook),
        NotebookEditTool(notebook),
        NotebookCreateTool(notebook),
        MigrationInspectTool(migration),
        MigrationValidateTool(migration),
        MigrationCreateTool(migration),
    ]


def test_valid_tool_prompt_spec_has_no_errors_or_warnings():
    report = lint_tool_registry(ToolRegistry([_ExampleTool()]))

    assert report.tool_count == 1
    assert report.error_count == 0
    assert report.warning_count == 0
    assert report.clean


def test_missing_guidance_fields_are_errors():
    codes = _codes(_valid_spec(
        instructions="",
        when_to_use=(),
        when_not_to_use=(),
    ))

    assert {"TPL002", "TPL003", "TPL004"} <= codes


def test_schema_shape_rejects_malformed_required_fields():
    codes = _codes(_valid_spec(parameters={
        "properties": {"value": {"type": "string"}},
        "required": ["value", "missing", "value", 3],
    }))

    assert {"TPL020", "TPL024", "TPL025", "TPL026"} <= codes


def test_example_must_match_schema_required_and_declared_fields():
    missing_required = _codes(_valid_spec(examples=(
        ToolPromptExample(
            title="Missing required value",
            input={},
            description="This example intentionally omits the required field.",
        ),
    )))
    unknown = _codes(_valid_spec(examples=(
        ToolPromptExample(
            title="Unknown field",
            input={"value": "widget", "extra": "not declared"},
            description="This example intentionally includes an unknown field.",
        ),
    )))

    assert "TPL015" in missing_required
    assert "TPL019" in missing_required
    assert "TPL014" in unknown


def test_example_must_satisfy_type_enum_and_range_constraints():
    spec = _valid_spec(
        parameters={
            "type": "object",
            "properties": {
                "value": {"type": "string", "enum": ["a", "b"]},
                "count": {"type": "integer", "minimum": 2, "maximum": 4},
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        examples=(
            ToolPromptExample(
                title="Invalid values",
                input={"value": "c", "count": 1},
                description="Both fields violate their declared constraints.",
            ),
        ),
    )

    codes = _codes(spec)

    assert "TPL015" in codes


def test_duplicate_example_inputs_are_rejected_even_with_different_titles():
    codes = _codes(_valid_spec(examples=(
        ToolPromptExample(
            title="First call",
            input={"value": "widget"},
            description="First explanation.",
        ),
        ToolPromptExample(
            title="Second call",
            input={"value": "widget"},
            description="Second explanation.",
        ),
    )))

    assert "TPL010" in codes


def test_credential_shaped_example_values_are_rejected_without_echoing_value():
    secret = "sk-" + "a" * 30
    issues = lint_tool_prompt_spec(_valid_spec(examples=(
        ToolPromptExample(
            title="Unsafe credential placeholder",
            input={"value": secret},
            description="No real credential may be embedded in Prompt examples.",
        ),
    )), source="builtin")
    rendered = "\n".join(issue.as_record()["message"] for issue in issues)

    assert "TPL016" in {issue.code for issue in issues}
    assert secret not in rendered


def test_external_tool_missing_examples_is_warning_not_runtime_blocker():
    class ExternalTool:
        name = "external_inspect"
        description = "External inspect tool."
        parameters = _parameters(value={"type": "string"})
        prompt = "Read only the requested externally provided value."
        when_to_use = ("An external capability is required.",)
        when_not_to_use = ("A local tool can perform the operation.",)

        async def execute(self, args, ctx):
            return ToolResult(content="ok")

    registry = ToolRegistry()
    registry.register(
        ExternalTool(),
        descriptor=ToolDescriptor(source=ToolSource.MCP),
    )

    report = lint_tool_registry(registry)

    assert report.error_count == 0
    assert report.warning_count == 1
    assert report.issues[0].code == "TPL008"
    assert report.issues[0].severity == PromptLintSeverity.WARNING


def test_all_known_model_visible_tools_have_schema_valid_examples():
    tools = _all_known_tools()
    registry = ToolRegistry(tools)

    report = lint_tool_registry(registry)

    assert len(tools) == 51
    assert report.tool_count == len(tools)
    assert report.error_count == 0
    assert report.warning_count == 0
    assert all(
        ToolPromptSpec.from_tool(tool, source="canonical").examples
        for tool in tools
    )


def test_lint_record_and_lines_are_content_light_and_order_is_stable():
    spec = _valid_spec(
        description="PROMPT_BODY_MUST_NOT_LEAK",
        instructions="",
        parameters={
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "enum": ["SCHEMA_VALUE_MUST_NOT_LEAK"],
                },
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        examples=(
            ToolPromptExample(
                title="Schema mismatch",
                input={"value": "EXAMPLE_VALUE_MUST_NOT_LEAK"},
                description="This intentionally violates the enum constraint.",
            ),
        ),
    )
    report = lint_tool_registry(ToolRegistry([_ExampleTool(spec=spec)]))
    record = report.as_record()
    serialized = json.dumps(record, ensure_ascii=False, sort_keys=True)
    rendered = "\n".join(report.lines())

    assert record["content_light"] is True
    assert "PROMPT_BODY_MUST_NOT_LEAK" not in serialized + rendered
    assert "SCHEMA_VALUE_MUST_NOT_LEAK" not in serialized + rendered
    assert "EXAMPLE_VALUE_MUST_NOT_LEAK" not in serialized + rendered
    assert report.lines() == report.lines()
    assert report.as_record() == report.as_record()


def test_prompts_lint_command_is_read_only_and_content_light(tmp_path: Path):
    loop = AgentLoop(
        llm=MockProvider(scripted=["done"]),
        registry=ToolRegistry([_ExampleTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYSTEM_PROMPT_BODY_MUST_NOT_LEAK",
        today_provider=lambda: "2026-11-21",
    )
    controller = SessionController(loop, workspace_root=tmp_path)

    output = "\n".join(asyncio.run(
        controller.execute_command("/prompts lint")).output)
    focused = "\n".join(asyncio.run(
        controller.execute_command("/prompts lint inspect_value")).output)
    missing = "\n".join(asyncio.run(
        controller.execute_command("/prompts lint unknown_tool")).output)

    assert "tools=1" in output
    assert "errors=0" in output
    assert "warnings=0" in output
    assert "content-light=true" in output
    assert "scope= all" in output
    assert "tool=inspect_value" in focused
    assert "未找到工具" in missing
    assert "Read the single requested value" not in output + focused
    assert "SYSTEM_PROMPT_BODY_MUST_NOT_LEAK" not in output + focused


class _ExampleTool:
    def __init__(self, *, spec: ToolPromptSpec | None = None) -> None:
        self._spec = spec or _valid_spec()

    name = "inspect_value"
    description = "Inspect one value."
    parameters = _parameters(value={"type": "string", "minLength": 1})
    prompt = "Read the single requested value and report evidence."
    when_to_use = ("A known value must be inspected.",)
    when_not_to_use = ("The value location is unknown; search first.",)
    prompt_examples = _valid_spec().examples

    @property
    def prompt_spec(self) -> ToolPromptSpec:
        return self._spec

    async def execute(self, args, ctx) -> ToolResult:
        return ToolResult(content="ok")
