"""MCP conformance matrix：版本、传输、能力和诚实降级报告。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from krelcode.mcp.conformance import (
    STATUS_NOT_APPLICABLE,
    STATUS_PARTIAL,
    STATUS_SUPPORTED,
    STATUS_UNSUPPORTED,
    build_conformance_report,
    format_conformance_report,
)
from krelcode.mcp.supervisor import MCPSupervisor
from krelcode.session.commands import builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers.runtime_ops import handle_mcp
from krelcode.session.commands.types import CommandRequest
from krelcode.tools.registry import ToolRegistry

STUB = Path(__file__).parent / "mcp_stub_server.py"
STUB_SPEC = {"command": sys.executable, "args": [str(STUB)]}


async def noop_elicitor(_request):
    return {"action": "decline"}


def test_report_is_executable_and_does_not_overclaim():
    async def go():
        supervisor = MCPSupervisor(
            ToolRegistry(), log=lambda _message: None,
            elicitor=noop_elicitor)
        await supervisor.sync({"stub": STUB_SPEC})
        try:
            report = supervisor.conformance_reports("stub")[0]
            assert report.server == "stub"
            assert report.transport == "stdio"
            assert report.requested_protocol_version == "2025-06-18"
            assert report.negotiated_protocol_version == "2025-06-18"
            assert report.check("capability:tools").status == STATUS_SUPPORTED
            assert report.check("reverse:sampling").status == STATUS_NOT_APPLICABLE
            assert report.check("reverse:elicitation").status == STATUS_SUPPORTED
            assert report.check("spec:completion").status == STATUS_UNSUPPORTED
            assert report.check("robustness:oversized_payload").status == STATUS_PARTIAL
            assert dict(report.loaded)["tools"] >= 1
        finally:
            await supervisor.close_all()

    asyncio.run(go())


def test_headless_report_marks_elicitation_partial_not_supported():
    async def go():
        supervisor = MCPSupervisor(ToolRegistry(), log=lambda _message: None)
        await supervisor.sync({"stub": STUB_SPEC})
        try:
            report = supervisor.conformance_reports("stub")[0]
            elicitation = report.check("reverse:elicitation")
            assert elicitation.status == STATUS_PARTIAL
            assert "decline" in elicitation.detail
        finally:
            await supervisor.close_all()

    asyncio.run(go())


def test_old_protocol_marks_elicitation_not_applicable():
    async def go():
        supervisor = MCPSupervisor(
            ToolRegistry(), log=lambda _message: None,
            elicitor=noop_elicitor)
        spec = dict(STUB_SPEC, env={"KRELCODE_STUB_PROTOCOL_VERSION": "2025-03-26"})
        await supervisor.sync({"stub": spec})
        try:
            report = supervisor.conformance_reports("stub")[0]
            assert report.negotiated_protocol_version == "2025-03-26"
            assert report.check("reverse:elicitation").status == STATUS_NOT_APPLICABLE
        finally:
            await supervisor.close_all()

    asyncio.run(go())


def test_conformance_formatter_is_content_light():
    async def go():
        supervisor = MCPSupervisor(
            ToolRegistry(), log=lambda _message: None,
            elicitor=noop_elicitor)
        await supervisor.sync({"stub": STUB_SPEC})
        try:
            report = build_conformance_report(supervisor.clients["stub"])
            lines = format_conformance_report(report)
            output = "\n".join(lines)
            assert "[mcp:conformance] stub" in output
            assert "reverse:elicitation" in output
            assert "spec:completion" in output
            assert "content-light=true" in output
            assert "inputSchema" not in output
        finally:
            await supervisor.close_all()

    asyncio.run(go())


def test_mcp_conformance_command(tmp_path):
    async def go():
        supervisor = MCPSupervisor(
            ToolRegistry(), log=lambda _message: None,
            elicitor=noop_elicitor)
        await supervisor.sync({"stub": STUB_SPEC})
        try:
            context = CommandContext(
                request=CommandRequest(
                    "mcp", "conformance stub", "/mcp conformance stub"),
                loop=None,
                command_registry=builtin_command_registry(),
                workspace_root=tmp_path,
                mcp_supervisor=supervisor,
            )
            result = await handle_mcp(context)
            assert result.handled is True
            output = "\n".join(result.output)
            assert "[mcp:conformance] stub" in output
            assert "[supported] reverse:elicitation" in output
            assert "[unsupported] spec:completion" in output
        finally:
            await supervisor.close_all()

    asyncio.run(go())
