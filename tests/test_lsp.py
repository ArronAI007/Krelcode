from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from krelcode.lsp.client import LSPClient
from krelcode.lsp.manager import LSPManager, LSPOptions, LSPServerConfig
from krelcode.lsp.tools import (
    LSPDefinitionTool,
    LSPDiagnosticsTool,
    LSPHoverTool,
    LSPReferencesTool,
)
from krelcode.tools.base import ToolContext


ROOT = Path(__file__).parent.parent
FAKE = ROOT / "tests" / "fixtures" / "lsp" / "fake_server.py"


def options() -> LSPOptions:
    return LSPOptions(
        enabled=True,
        startup_timeout=5,
        request_timeout=3,
        servers=(LSPServerConfig(
            "python", (sys.executable, str(FAKE)), (".py",), "python",
        ),),
    )


def run(coro):
    return asyncio.run(coro)


def test_lsp_client_framing_and_diagnostics(tmp_path):
    (tmp_path / "sample.py").write_text("value = 1\n")

    async def go():
        manager = LSPManager(tmp_path, options=options())
        try:
            result, client, uri, server = await manager.diagnostics_for(
                tmp_path / "sample.py")
            assert server.name == "python"
            assert client.server_capabilities["hoverProvider"] is True
            assert uri.endswith("sample.py")
            assert result["diagnostics"][0]["message"] == "fake warning"
            assert manager.server_for_path(tmp_path / "sample.py").name == "python"
        finally:
            await manager.close()

    run(go())


def test_lsp_tools_return_narrow_workspace_results(tmp_path):
    (tmp_path / "sample.py").write_text("value = 1\n")

    async def go():
        manager = LSPManager(tmp_path, options=options())
        ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)
        try:
            diagnostics = await LSPDiagnosticsTool(manager).execute(
                {"path": "sample.py"}, ctx)
            assert not diagnostics.is_error
            assert "fake warning" in diagnostics.content

            hover = await LSPHoverTool(manager).execute(
                {"path": "sample.py", "line": 1, "character": 1}, ctx)
            assert "fake hover" in hover.content

            definition = await LSPDefinitionTool(manager).execute(
                {"path": "sample.py", "line": 1, "character": 1}, ctx)
            assert "sample.py:2:3" in definition.content

            references = await LSPReferencesTool(manager).execute(
                {"path": "sample.py", "line": 1, "character": 1}, ctx)
            assert "workspace" not in references.content
            assert references.data["outside_workspace"] == 1
        finally:
            await manager.close()

    run(go())


def test_lsp_rejects_sensitive_and_outside_files(tmp_path):
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=secret\n")
    (tmp_path / "safe.py").write_text("x = 1\n")

    async def go():
        manager = LSPManager(tmp_path, options=options())
        ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)
        try:
            blocked = await LSPHoverTool(manager).execute(
                {"path": ".env", "line": 1, "character": 1}, ctx)
            assert blocked.is_error
            outside = await LSPHoverTool(manager).execute(
                {"path": str(tmp_path.parent / "other.py"),
                 "line": 1, "character": 1}, ctx)
            assert outside.is_error
        finally:
            await manager.close()

    run(go())


def test_lsp_disabled_does_not_expose_failure_as_crash(tmp_path):
    manager = LSPManager(tmp_path)
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)
    result = asyncio.run(LSPDiagnosticsTool(manager).execute(
        {"path": "sample.py"}, ctx))
    assert result.is_error
    assert "未启用" in result.content
