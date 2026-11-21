"""P0 extension ecosystem: layered config, Plugin v2 and provenance."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from krelcode.app import (
    build_loop,
    build_parser,
    compose_hook_specs,
    compose_mcp_config,
)
from krelcode.config import load_config_layers
from krelcode.core.errors import ConfigError
from krelcode.doctor import STATUS_FAIL, check_config_layers
from krelcode.extensions import (
    ExtensionKind,
    ExtensionRecord,
    ExtensionRegistry,
    collect_extension_records,
)
from krelcode.hooks import HookRunner, parse_hooks
from krelcode.plugins import PluginManager, PluginManifestError, parse_manifest
from krelcode.session.controller import SessionController


TOOL_SOURCE = '''
from krelcode.tools.base import ToolResult

class EchoTool:
    name = "echo"
    description = "Echo"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    async def execute(self, args, ctx):
        return ToolResult(content="ok")

def tools():
    return [EchoTool()]
'''


def write_v2_plugin(root: Path, *, version: str = "1.0.0") -> Path:
    plugin = root / "demo"
    (plugin / "skills" / "review").mkdir(parents=True, exist_ok=True)
    (plugin / "agents").mkdir(exist_ok=True)
    (plugin / "commands").mkdir(exist_ok=True)
    (plugin / "plugin.py").write_text(TOOL_SOURCE, encoding="utf-8")
    (plugin / "plugin.toml").write_text(
        f'''manifest_version = 2
name = "demo"
version = "{version}"
description = "Plugin v2 demo"

[tools]
entrypoint = "plugin:tools"

[skills]
paths = ["skills"]

[agents]
paths = ["agents"]

[commands]
paths = ["commands"]

[hooks]
paths = ["hooks.toml"]

[mcp]
paths = ["mcp.toml"]
''',
        encoding="utf-8",
    )
    (plugin / "skills" / "review" / "SKILL.md").write_text(
        '''---
name: review
description: Review code carefully
---
Review the requested code.
''',
        encoding="utf-8",
    )
    (plugin / "agents" / "helper.md").write_text(
        '''---
name: helper
description: A plugin-provided helper
allowed-tools: read_file
---
Help with the requested task.
''',
        encoding="utf-8",
    )
    (plugin / "commands" / "deploy.md").write_text(
        '''---
name: deploy
description: Deploy a target
argument-hint: <target>
allowed-tools: read_file
---
Deploy $ARGUMENTS from the plugin.
''',
        encoding="utf-8",
    )
    (plugin / "hooks.toml").write_text(
        '''[[hooks]]
on = "session_start"
command = 'printf "%s|%s" "$KRELCODE_HOOK_SOURCE" "$KRELCODE_PLUGIN_ROOT"'
''',
        encoding="utf-8",
    )
    (plugin / "mcp.toml").write_text(
        '''[servers.local]
command = "python3"
args = ["-m", "demo"]
''',
        encoding="utf-8",
    )
    return plugin


def test_layered_config_deep_merge_and_explicit_is_exclusive(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    managed = tmp_path / "managed.toml"
    (home / ".krelcode").mkdir(parents=True)
    workspace.mkdir()
    (home / ".krelcode" / "config.toml").write_text(
        '''default_provider = "user"
[providers.openai]
model = "user-model"
timeout = 10
[skills]
enabled = true
paths = ["user"]
''', encoding="utf-8")
    (workspace / "krelcode.toml").write_text(
        '''default_provider = "project"
[providers.openai]
timeout = 20
[skills]
paths = ["project"]
''', encoding="utf-8")
    (workspace / "krelcode.local.toml").write_text(
        '''default_provider = "local"
[providers.openai]
model = "local-model"
[skills]
paths = ["local"]
''', encoding="utf-8")
    managed.write_text(
        '''default_provider = "managed"
[providers.openai]
timeout = 30
''', encoding="utf-8")

    bundle = load_config_layers(
        workspace,
        home=home,
        environ={"KRELCODE_MANAGED_CONFIG": str(managed)},
    )
    assert bundle.data["default_provider"] == "managed"
    assert bundle.data["providers"]["openai"] == {
        "model": "local-model", "timeout": 30}
    assert bundle.data["skills"]["paths"] == ["local"]
    assert [layer.scope for layer in bundle.layers if layer.exists] == [
        "user", "project", "local", "managed"]

    explicit = tmp_path / "explicit.toml"
    explicit.write_text('default_provider = "explicit"\n', encoding="utf-8")
    exclusive = load_config_layers(
        workspace, explicit_path=explicit,
        environ={"KRELCODE_MANAGED_CONFIG": str(managed)})
    assert exclusive.data == {"default_provider": "explicit"}
    assert [layer.scope for layer in exclusive.layers if layer.exists] == [
        "explicit"]

    explicit.write_text("broken = [", encoding="utf-8")
    with pytest.raises(ConfigError, match="显式配置"):
        load_config_layers(workspace, explicit_path=explicit, environ={})


def test_optional_config_error_is_visible_and_managed_error_fails_closed(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    (home / ".krelcode").mkdir(parents=True)
    workspace.mkdir()
    (home / ".krelcode" / "config.toml").write_text("broken = [", encoding="utf-8")
    (workspace / "krelcode.toml").write_text(
        'default_provider = "mock"\n', encoding="utf-8")
    bundle = load_config_layers(workspace, home=home, environ={})
    assert bundle.data["default_provider"] == "mock"
    assert bundle.errors and bundle.errors[0].scope == "user"

    managed = tmp_path / "managed.toml"
    managed.write_text("broken = [", encoding="utf-8")
    with pytest.raises(ConfigError, match="managed"):
        load_config_layers(
            workspace, home=home,
            environ={"KRELCODE_MANAGED_CONFIG": str(managed)})


def test_plugin_components_merge_with_explicit_config(tmp_path):
    plugin_hook = {
        "_source": "plugin:demo@1.0.0",
        "_plugin_root": str(tmp_path / "demo"),
        "on": "session_start",
        "command": "printf plugin",
    }
    hooks = compose_hook_specs(
        {"hooks": [{"on": "run_finished", "command": "printf user"}]},
        (plugin_hook,),
    )
    assert [hook.source for hook in hooks] == ["config", "plugin:demo@1.0.0"]

    merged = compose_mcp_config(
        {"mcp_servers": {
            "plugin__demo__remote": {"url": "https://user.example/mcp"},
            "user": {"command": "user-server"},
        }},
        {
            "plugin__demo__remote": {"url": "https://plugin.example/mcp"},
            "plugin__demo__local": {"command": "plugin-server"},
        },
    )
    assert merged["plugin__demo__remote"]["url"] == "https://user.example/mcp"
    assert merged["plugin__demo__local"]["command"] == "plugin-server"
    assert merged["user"]["command"] == "user-server"


def test_doctor_reports_layered_config_error(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    (home / ".krelcode").mkdir(parents=True)
    workspace.mkdir()
    (home / ".krelcode" / "config.toml").write_text("broken = [", encoding="utf-8")
    (workspace / "krelcode.toml").write_text(
        'default_provider = "mock"\n', encoding="utf-8")
    bundle = load_config_layers(workspace, home=home, environ={})
    result = check_config_layers(bundle)
    assert result.status == STATUS_FAIL
    assert "user" in result.detail


def test_extension_registry_tracks_provenance_conflicts_and_disabled():
    registry = ExtensionRegistry()
    low = ExtensionRecord(
        "demo", ExtensionKind.SKILL, "demo", "user", enabled=True, priority=10)
    high = ExtensionRecord(
        "demo", ExtensionKind.SKILL, "demo", "managed", enabled=False, priority=20)
    assert registry.register(low)
    assert registry.register(high)
    assert registry.get(ExtensionKind.SKILL, "demo") is high
    assert registry.conflicts[0].shadowed is low
    assert registry.list(include_disabled=False) == ()
    assert "disabled" in "\n".join(registry.status_lines())


def test_plugin_v2_manifest_loads_all_six_component_types(tmp_path):
    root = tmp_path / "plugins"
    plugin = write_v2_plugin(root)
    manager = PluginManager(
        tmp_path / "workspace", home=tmp_path / "home",
        extra_paths=[root], state_path=tmp_path / "state.json")

    records = asyncio.run(manager.load())

    assert len(records) == 1
    record = records[0]
    assert record.manifest_version == 2
    assert (
        record.tools_count, record.skills_count, record.agents_count,
        record.commands_count, record.hooks_count, record.mcp_servers_count,
    ) == (1, 1, 1, 1, 1, 1)
    assert record.components_count == 6
    assert manager.tools[0].name == "plugin__demo__echo"
    assert manager.commands[0].name == "plugin__demo__deploy"
    assert manager.skill_paths[0] == (plugin / "skills").resolve()
    assert manager.agent_paths[0] == (plugin / "agents").resolve()
    assert manager.mcp_servers["plugin__demo__local"]["_plugin"] == "demo"


def test_build_loop_registers_plugin_skills_agents_commands_and_extensions(tmp_path):
    root = tmp_path / "plugins"
    write_v2_plugin(root)
    args = build_parser().parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context",
        "--plugins-dir", str(root),
    ])

    loop, _ = asyncio.run(build_loop(args, {}))

    assert "plugin__demo__echo" in loop.registry.names()
    assert loop.skill_manager.get("review") is not None
    assert loop.agent_definitions.get("helper").name == "helper"
    controller = SessionController(loop)
    spec = controller.command_registry.resolve("/plugin__demo__deploy")
    assert spec is not None and spec.source.value == "plugin"
    result = asyncio.run(controller.execute_command(
        "/plugin__demo__deploy production"))
    assert "Deploy production" in (result.run_prompt or "")
    assert result.tool_allowlist == ("read_file",)
    assert result.command_source.value == "plugin"
    assert result.command_trust in {"user", "project"}
    assert "command:scoped-instructions" in result.reminder_ids
    assert "command:tool-scope" in result.reminder_ids

    records = collect_extension_records(
        plugins=loop.plugin_manager,
        skills=loop.skill_manager,
        agents=loop.agent_definitions,
        tools=loop.registry,
        commands=controller.command_registry,
        hooks=parse_hooks({"hooks": list(loop.plugin_manager.hook_definitions)}),
        mcp_servers=loop.plugin_manager.mcp_servers,
    )
    assert {record.plugin for record in records if record.kind is not ExtensionKind.PLUGIN} >= {"demo"}
    kinds = {record.kind for record in records}
    assert {
        ExtensionKind.PLUGIN, ExtensionKind.TOOL, ExtensionKind.SKILL,
        ExtensionKind.AGENT, ExtensionKind.COMMAND, ExtensionKind.HOOK,
        ExtensionKind.MCP,
    } <= kinds
    shown = asyncio.run(controller.execute_command("/extensions plugin"))
    assert "demo" in "\n".join(shown.output)


def test_plugin_hook_source_and_plugin_root_are_exported(tmp_path):
    root = tmp_path / "plugins"
    plugin = write_v2_plugin(root)
    manager = PluginManager(
        tmp_path / "workspace", home=tmp_path / "home",
        extra_paths=[root], state_path=tmp_path / "state.json")
    asyncio.run(manager.load())
    specs = parse_hooks({"hooks": list(manager.hook_definitions)})
    assert specs[0].source == "plugin:demo@1.0.0"
    assert specs[0].plugin_root == str(plugin)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = HookRunner(workspace, specs)
    text = asyncio.run(runner.run_lifecycle("session_start")).context
    assert text == (f"plugin:demo@1.0.0|{plugin}",)
    assert runner.executions[-1]["source"] == "plugin:demo@1.0.0"


def test_plugin_lifecycle_install_update_disable_enable_remove(tmp_path):
    source_root = tmp_path / "source"
    source = write_v2_plugin(source_root, version="1.0.0")
    manager = PluginManager(
        tmp_path / "workspace", home=tmp_path / "home")

    installed = asyncio.run(manager.install(source, scope="user"))
    assert installed.action == "install"
    asyncio.run(manager.load())
    assert manager.records()[0].version == "1.0.0"

    asyncio.run(manager.set_enabled("demo", False))
    asyncio.run(manager.load())
    assert manager.records()[0].status == "disabled"
    assert manager.tools == ()

    asyncio.run(manager.set_enabled("demo", True))
    asyncio.run(manager.load())
    assert manager.records()[0].status == "loaded"

    write_v2_plugin(source_root, version="2.0.0")
    updated = asyncio.run(manager.update(source, scope="user"))
    assert updated.action == "update"
    asyncio.run(manager.load())
    assert manager.records()[0].version == "2.0.0"

    removed = asyncio.run(manager.remove("demo"))
    assert removed.action == "remove"
    assert removed.path.exists()
    asyncio.run(manager.load())
    assert manager.records() == ()


def test_plugin_lifecycle_rejects_path_escape_symlinks_and_oversized_tree(
    tmp_path, monkeypatch,
):
    escaped = tmp_path / "escaped"
    escaped.mkdir()
    (escaped / "plugin.toml").write_text(
        '''manifest_version = 2
name = "escaped"
version = "1"
[commands]
paths = ["../outside"]
''', encoding="utf-8")
    with pytest.raises(PluginManifestError, match="相对路径"):
        parse_manifest(escaped / "plugin.toml")

    source = write_v2_plugin(tmp_path / "source")
    (source / "link").symlink_to(tmp_path / "outside.txt")
    manager = PluginManager(tmp_path / "workspace", home=tmp_path / "home")
    with pytest.raises(ValueError, match="符号链接"):
        asyncio.run(manager.install(source, scope="user"))

    (source / "link").unlink()
    import krelcode.plugins.manager as manager_module
    monkeypatch.setattr(manager_module, "MAX_INSTALL_FILES", 1)
    with pytest.raises(ValueError, match="文件过多"):
        asyncio.run(manager.install(source, scope="user"))


def test_plugins_command_reload_and_toggle_refreshes_runtime_and_commands(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "plugins"
    root.mkdir()
    args = build_parser().parse_args([
        "--mock", "--workspace", str(tmp_path), "--no-context",
        "--plugins-dir", str(root),
    ])
    loop, _ = asyncio.run(build_loop(args, {}))
    controller = SessionController(loop)
    assert "plugin__demo__echo" not in loop.registry.names()

    write_v2_plugin(root)
    reloaded = asyncio.run(controller.execute_command("/plugins reload"))
    assert "已重新扫描" in "\n".join(reloaded.output)
    assert "plugin__demo__echo" in loop.registry.names()
    assert loop.skill_manager.get("review") is not None
    assert loop.agent_definitions.get("helper") is not None
    assert controller.command_registry.resolve("/plugin__demo__deploy") is not None

    disabled = asyncio.run(controller.execute_command("/plugins disable demo"))
    assert "disabled demo" in "\n".join(disabled.output)
    assert "plugin__demo__echo" not in loop.registry.names()
    assert controller.command_registry.resolve("/plugin__demo__deploy") is None
    assert loop.skill_manager.get("review") is None
    assert not [r for r in loop.extension_registry.list(ExtensionKind.TOOL)
                if r.plugin == "demo"]

    enabled = asyncio.run(controller.execute_command("/plugins enable demo"))
    assert "enabled demo" in "\n".join(enabled.output)
    assert "plugin__demo__echo" in loop.registry.names()
    assert controller.command_registry.resolve("/plugin__demo__deploy") is not None
