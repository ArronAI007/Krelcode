"""第三阶段：轻量目录插件生态。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from krelcode.app import build_loop, build_parser
from krelcode.plugins import (
    PluginManager,
    PluginManifestError,
    parse_manifest,
)
from krelcode.session.commands import builtin_command_registry
from krelcode.session.controller import SessionController
from krelcode.tools.base import ToolSource


PLUGIN_SOURCE = '''
from krelcode.tools.base import ToolResult

class EchoTool:
    name = "echo"
    description = "Echo from plugin"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
    read_only = True

    async def execute(self, args, ctx):
        return ToolResult(content=f"echo:{args.get('text', '')}")

def tools():
    return [EchoTool()]
'''


def write_plugin(
    root: Path,
    name: str,
    *,
    source: str = PLUGIN_SOURCE,
    version: str = "1.0.0",
    enabled: bool = True,
) -> Path:
    plugin = root / name
    plugin.mkdir(parents=True)
    (plugin / "plugin.toml").write_text(
        f'''name = "{name}"\nversion = "{version}"\nentrypoint = "plugin:tools"\n'''
        f'''description = "{name} plugin"\nenabled = {str(enabled).lower()}\n''',
        encoding="utf-8",
    )
    (plugin / "plugin.py").write_text(source, encoding="utf-8")
    return plugin


def test_manifest_validation_and_invalid_entrypoint(tmp_path):
    plugin = write_plugin(tmp_path, "demo")
    manifest = parse_manifest(plugin / "plugin.toml")
    assert manifest.name == "demo"
    assert manifest.entrypoint == "plugin:tools"

    (plugin / "plugin.toml").write_text(
        'name = "bad name"\nversion = "1"\nentrypoint = "plugin:tools"\n',
        encoding="utf-8",
    )
    with pytest.raises(PluginManifestError, match="name"):
        parse_manifest(plugin / "plugin.toml")


def test_plugin_manager_namespaces_and_executes_existing_tool_protocol(tmp_path):
    workspace = tmp_path / "workspace"
    write_plugin(workspace / ".krelcode" / "plugins", "demo")
    manager = PluginManager(workspace, home=tmp_path / "home")

    records = asyncio.run(manager.load())

    assert [(r.name, r.status, r.tools_count) for r in records] == [
        ("demo", "loaded", 1)]
    assert [tool.name for tool in manager.tools] == ["plugin__demo__echo"]
    tool = manager.tools[0]
    assert tool.read_only is True
    assert tool.description.startswith("[plugin:demo@1.0.0]")
    result = asyncio.run(tool.execute({"text": "ok"}, None))
    assert result.content == "echo:ok"


def test_bad_plugin_is_isolated_and_can_be_reloaded(tmp_path):
    root = tmp_path / "plugins"
    write_plugin(root, "good")
    broken = write_plugin(root, "broken", source="raise RuntimeError('boom')\n")
    manager = PluginManager(tmp_path, home=tmp_path / "home", extra_paths=[root])

    records = asyncio.run(manager.load())
    status = {r.name: r.status for r in records}
    assert status == {"good": "loaded", "broken": "error"}
    assert [tool.name for tool in manager.tools] == ["plugin__good__echo"]
    assert any("boom" in issue.message for issue in manager.issues)

    (broken / "plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    records = asyncio.run(manager.reload())
    assert {r.name: r.status for r in records} == {
        "good": "loaded", "broken": "loaded"}


def test_later_plugin_root_wins_name_conflict(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    write_plugin(home / ".krelcode" / "plugins", "demo", version="1.0.0")
    write_plugin(workspace / ".krelcode" / "plugins", "demo", version="2.0.0")
    manager = PluginManager(workspace, home=home)

    asyncio.run(manager.load())

    assert [(r.name, r.version) for r in manager.records()] == [("demo", "2.0.0")]
    assert any("覆盖" in issue.message for issue in manager.issues)


def test_build_loop_registers_plugins_and_no_plugins_skips_them(tmp_path):
    plugin_root = tmp_path / "plugins"
    write_plugin(plugin_root, "demo")
    parser = build_parser()
    args = parser.parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context",
        "--plugins-dir", str(plugin_root),
    ])

    loop, _ = asyncio.run(build_loop(args, {}))
    try:
        assert "plugin__demo__echo" in loop.registry.names()
        descriptor = loop.registry.descriptor("plugin__demo__echo")
        assert descriptor.source is ToolSource.PLUGIN
        assert descriptor.plugin_name == "demo"
        write_plugin(plugin_root, "second")
        asyncio.run(loop.plugin_reload())
        assert "plugin__second__echo" in loop.registry.names()
    finally:
        asyncio.run(loop.plugin_manager.reload())

    disabled_args = parser.parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context",
        "--plugins-dir", str(plugin_root), "--no-plugins",
    ])
    disabled_loop, _ = asyncio.run(build_loop(disabled_args, {}))
    assert "plugin__demo__echo" not in disabled_loop.registry.names()


def test_plugins_command_status_and_reload(tmp_path):
    plugin_root = tmp_path / "plugins"
    write_plugin(plugin_root, "demo")
    manager = PluginManager(
        tmp_path, home=tmp_path / "home", extra_paths=[plugin_root])
    asyncio.run(manager.load())
    loop, _ = asyncio.run(build_loop(
        build_parser().parse_args([
            "--mock", "--workspace", str(tmp_path), "--no-context"]), {}))
    loop.plugin_manager = manager

    async def reload():
        await manager.reload()
        return manager.records()

    loop.plugin_reload = reload
    controller = SessionController(loop)
    result = asyncio.run(controller.execute_command("/plugins"))
    assert "demo@1.0.0 [loaded]" in "\n".join(result.output)
    reloaded = asyncio.run(controller.execute_command("/plugins reload"))
    assert "已重新扫描" in "\n".join(reloaded.output)
    shown = asyncio.run(controller.execute_command("/plugins show demo"))
    assert "入口: plugin:tools" in "\n".join(shown.output)
    assert builtin_command_registry().resolve("/plugins") is not None
