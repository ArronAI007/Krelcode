"""MCPSupervisor 测试：配置 diff 同步、工具注册/注销、崩溃热重启。"""

import asyncio
import sys
from pathlib import Path

import pytest

from krelcode.mcp.supervisor import MCPSupervisor, config_from_spec
from krelcode.mcp.tools import server_prefix
from krelcode.tools.registry import ToolRegistry

STUB = Path(__file__).parent / "mcp_stub_server.py"
STUB_SPEC = {"command": sys.executable, "args": [str(STUB)], "timeout": 15.0}


def run(coro):
    return asyncio.run(coro)


def make_sup():
    registry = ToolRegistry()
    logs = []
    sup = MCPSupervisor(registry, log=logs.append)
    return sup, registry, logs


# -- config_from_spec --------------------------------------------------------------------

def test_config_from_spec_stdio_and_url_and_invalid():
    cfg = config_from_spec("a", {"command": "x", "args": ["1"]})
    assert cfg.command == "x" and cfg.args == ["1"] and cfg.url is None
    cfg2 = config_from_spec("b", {"url": "http://h/sse", "headers": {"K": "V"}})
    assert cfg2.url == "http://h/sse" and cfg2.headers == {"K": "V"}
    assert config_from_spec("c", {}) is None
    assert config_from_spec("d", "not a dict") is None


# -- sync：增 / 删 / 改 ---------------------------------------------------------------------

def test_sync_add_registers_tools_and_status():
    async def go():
        sup, registry, logs = make_sup()
        await sup.sync({"stub": STUB_SPEC})
        try:
            names = registry.names()
            assert len(names) == 7 and all(n.startswith("stub__") for n in names)
            status = sup.status()[0]
            assert status["alive"] and status["transport"] == "stdio"
            assert status["tools"] == 7 and status["server"] == "stub-server"
            assert any("已连接" in l for l in logs)
        finally:
            await sup.close_all()
    run(go())


def test_sync_idempotent_same_config_keeps_client():
    async def go():
        sup, registry, _ = make_sup()
        await sup.sync({"stub": STUB_SPEC})
        client1 = sup.clients["stub"]
        await sup.sync({"stub": dict(STUB_SPEC)})  # 相同配置
        assert sup.clients["stub"] is client1      # 未重启
        await sup.close_all()
    run(go())


def test_sync_remove_unregisters_tools_and_closes():
    async def go():
        sup, registry, logs = make_sup()
        await sup.sync({"stub": STUB_SPEC})
        client = sup.clients["stub"]
        await sup.sync({})                          # 配置移除
        assert registry.names() == []
        assert not client.alive
        assert sup.clients == {}
        assert any("已停止" in l for l in logs)
    run(go())


def test_sync_changed_config_restarts():
    async def go():
        sup, registry, _ = make_sup()
        await sup.sync({"stub": STUB_SPEC})
        client1 = sup.clients["stub"]
        changed = dict(STUB_SPEC, timeout=12.0)     # 配置变更
        await sup.sync({"stub": changed})
        client2 = sup.clients["stub"]
        assert client2 is not client1
        assert not client1.alive and client2.alive
        assert len([n for n in registry.names() if n.startswith("stub__")]) == 7
        await sup.close_all()
    run(go())


def test_sync_skips_invalid_and_broken_servers():
    async def go():
        sup, registry, logs = make_sup()
        await sup.sync({
            "invalid": {},
            "broken": {"command": "/nonexistent/mcp-server"},
        })
        assert registry.names() == []
        assert sup.clients == {}
        assert any("跳过 invalid" in l for l in logs)
        assert any("broken 启动失败" in l for l in logs)
    run(go())


# -- 崩溃热重启 -------------------------------------------------------------------------------

def test_check_and_restart_recovers_crashed_server():
    async def go():
        sup, registry, _ = make_sup()
        await sup.sync({"stub": STUB_SPEC})
        client = sup.clients["stub"]
        client._transport._proc.kill()      # 模拟崩溃
        await asyncio.sleep(0.3)
        assert not client.alive

        restarted = await sup.check_and_restart()
        assert restarted == ["stub"]
        new_client = sup.clients["stub"]
        assert new_client is not client and new_client.alive
        # 工具重注册且可调用
        assert len(registry.names()) == 7
        echo = registry.get("stub__echo")
        from krelcode.tools.base import ToolContext
        result = await echo.execute({"text": "复活"}, ToolContext(workspace_root=Path(".")))
        assert result.content == "复活"
        await sup.close_all()
    run(go())


def test_check_and_restart_noop_when_healthy():
    async def go():
        sup, _, _ = make_sup()
        await sup.sync({"stub": STUB_SPEC})
        assert await sup.check_and_restart() == []
        await sup.close_all()
    run(go())


# -- registry 前缀注销 ---------------------------------------------------------------------------

def test_registry_unregister_and_prefix():
    from krelcode.mcp.tools import MCPTool

    class DummyClient:
        name = "srv"
    t1 = MCPTool(DummyClient(), {"name": "a"})
    t2 = MCPTool(DummyClient(), {"name": "b"})

    class Local:
        name = "local"
        description = ""
        parameters = {}
    registry = ToolRegistry([t1, t2, Local()])
    assert server_prefix("srv") == "srv__"
    assert registry.unregister_prefix("srv__") == 2
    assert registry.names() == ["local"]
    assert registry.unregister("local") is True
    assert registry.unregister("ghost") is False
    assert registry.names() == []
