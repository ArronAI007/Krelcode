"""68D：MCP Prompt → 命名空间 slash command。"""

import asyncio
import sys
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.llm.providers.mock import MockProvider
from krelcode.mcp.supervisor import MCPSupervisor
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.commands import (
    CommandKind,
    CommandSource,
    builtin_command_registry,
    mcp_prompt_command_specs,
)
from krelcode.session.commands.handlers.runtime_ops import _parse_prompt_arguments
from krelcode.session.controller import SessionController
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.repl import match_slash_commands, run_repl

STUB = Path(__file__).parent / "mcp_stub_server.py"
STUB_SPEC = {"command": sys.executable, "args": [str(STUB)]}


def make_loop(tmp_path):
    return AgentLoop(
        llm=MockProvider(scripted=["done"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_supervisor_exposes_namespaced_prompt_records():
    async def go():
        supervisor = MCPSupervisor(ToolRegistry(), log=lambda _: None)
        await supervisor.sync({"stub": STUB_SPEC})
        try:
            records = supervisor.prompt_records()
            assert records == [{
                "command_name": "mcp__stub__review",
                "server": "stub",
                "prompt_name": "review",
                "description": "代码评审提示",
                "argument_names": ("path",),
            }]
            assert supervisor.status()[0]["prompts"] == 1
            specs = mcp_prompt_command_specs(supervisor)
            assert specs[0].kind is CommandKind.PROMPT
            assert specs[0].source is CommandSource.MCP
            assert specs[0].usage == "/mcp__stub__review <path>"
        finally:
            await supervisor.close_all()

    asyncio.run(go())


def test_mcp_prompt_command_renders_prompt_for_agent(tmp_path):
    async def go():
        loop = make_loop(tmp_path)
        supervisor = MCPSupervisor(loop.registry, log=lambda _: None)
        await supervisor.sync({"stub": STUB_SPEC})
        try:
            registry = builtin_command_registry()
            controller = SessionController(
                loop,
                mcp_supervisor=supervisor,
                command_registry=registry,
            )
            spec = registry.resolve("/mcp__stub__review")
            assert spec.kind is CommandKind.PROMPT
            assert spec.source is CommandSource.MCP
            assert ("/mcp__stub__review", "代码评审提示") in match_slash_commands(
                "/mcp__stub__re", registry=registry)

            result = await controller.execute_command(
                "/mcp__stub__review path=main.py")
            assert result.handled is True
            assert "已加载并执行: stub/review" in result.output[0]
            assert (
                '<evidence source="mcp_prompt" server="stub" name="review" '
                'trust="untrusted" instruction="false"' in result.run_prompt
            )
            assert (
                '<mcp_prompt server="stub" name="review" '
                'trust="untrusted" instruction="false">' in result.run_prompt
            )
            assert '<command_invocation ' in result.run_prompt
            assert result.command_source is CommandSource.MCP
            assert result.command_trust == "untrusted"
            assert "command:untrusted-instructions" in result.reminder_ids
            assert len(result.template_hash) == 64
            assert "[user]\n请评审文件 main.py" in result.run_prompt
        finally:
            await supervisor.close_all()

    asyncio.run(go())


def test_mcp_reload_refreshes_prompt_catalog(tmp_path):
    async def go():
        loop = make_loop(tmp_path)
        supervisor = MCPSupervisor(loop.registry, log=lambda _: None)
        await supervisor.sync({"stub": STUB_SPEC})
        try:
            registry = builtin_command_registry()

            async def reload_mcp():
                await supervisor.refresh_prompts()
                return []

            controller = SessionController(
                loop,
                mcp_supervisor=supervisor,
                mcp_reload=reload_mcp,
                command_registry=registry,
            )
            original = supervisor.clients["stub"].list_prompts

            async def expanded():
                return await original() + [{
                    "name": "summarize",
                    "description": "总结变更",
                    "arguments": [],
                }]

            supervisor.clients["stub"].list_prompts = expanded
            await controller.execute_command("/mcp reload")

            added = registry.resolve("/mcp__stub__summarize")
            assert added is not None and added.source is CommandSource.MCP
        finally:
            await supervisor.close_all()

    asyncio.run(go())


def test_repl_mcp_prompt_enters_agent_history(tmp_path):
    async def go():
        loop = make_loop(tmp_path)
        supervisor = MCPSupervisor(loop.registry, log=lambda _: None)
        await supervisor.sync({"stub": STUB_SPEC})
        try:
            lines = ["/mcp__stub__review path=src/app.py", "/exit"]

            async def scripted_input(_prompt):
                if not lines:
                    raise EOFError
                await asyncio.sleep(0.01)
                return lines.pop(0)

            rc = await run_repl(
                loop,
                input_fn=scripted_input,
                mcp_supervisor=supervisor,
            )
            assert rc == 0
            user_message = next(m for m in loop.history if m.role == "user")
            assert loop.last_reminder_selection is not None
            assert (
                "command:untrusted-instructions"
                in loop.last_reminder_selection.ids
            )
            assert 'trust="untrusted" instruction="false"' in user_message.content
            assert "[user]\n请评审文件 src/app.py" in user_message.content
        finally:
            await supervisor.close_all()

    asyncio.run(go())


def test_mcp_prompt_argument_mapping_supports_named_and_positional():
    names = ("path", "style")
    assert _parse_prompt_arguments(names, "path=a.py style=strict") == {
        "path": "a.py", "style": "strict",
    }
    assert _parse_prompt_arguments(names, "a.py strict") == {
        "path": "a.py", "style": "strict",
    }
    assert _parse_prompt_arguments(("path",), "a b.py") == {"path": "a b.py"}
