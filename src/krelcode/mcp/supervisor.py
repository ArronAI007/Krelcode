"""MCPSupervisor：MCP 服务器集群的热重载管理。

职责：
- sync(servers_config)：配置 diff——新增的启动、删除的停止并注销工具、
  变更的（command/args/env/url/headers/timeout）先停后启
- check_and_restart()：把已断开（进程崩溃/SSE 断流）的服务器重启并重注册工具
- status()：给 REPL /mcp 展示
- close_all()：退出时优雅关闭

工具注册/注销都走 ToolRegistry（unregister_prefix 按 <server>__ 前缀清理），
AgentLoop 无需感知服务器的来去。单服务器失败只告警，不阻断其他服务器。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..tools.registry import ToolRegistry
from .client import MCPClient, MCPServerConfig
from .tools import discover_tools, server_prefix


def config_from_spec(name: str, spec: Any) -> Optional[MCPServerConfig]:
    """TOML [mcp_servers.<name>] → MCPServerConfig；非法配置返回 None。"""
    if not isinstance(spec, dict):
        return None
    command = spec.get("command")
    url = spec.get("url")
    if not command and not url:
        return None
    return MCPServerConfig(
        name=name,
        command=str(command) if command else None,
        args=[str(a) for a in spec.get("args", [])],
        env=dict(spec.get("env") or {}),
        url=str(url) if url else None,
        headers=dict(spec.get("headers") or {}),
        transport=spec.get("transport"),
        listener_retries=int(spec.get("listener_retries", 3)),
        listener_retry_delay=float(spec.get("listener_retry_delay", 1.0)),
        timeout=float(spec.get("timeout", 30.0)),
    )


class MCPSupervisor:
    def __init__(
        self,
        registry: ToolRegistry,
        log: Callable[[str], None] = print,
        sampler: Optional[Any] = None,
        roots: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.registry = registry
        self._log = log
        self._sampler = sampler      # 服务器 sampling 请求的回调（app 注入）
        self._roots = list(roots or [])
        self.clients: Dict[str, MCPClient] = {}
        self.configs: Dict[str, MCPServerConfig] = {}

    # -- 配置同步 -------------------------------------------------------------------

    async def sync(self, servers: Optional[Dict[str, Any]]) -> None:
        desired: Dict[str, MCPServerConfig] = {}
        for name, spec in (servers or {}).items():
            cfg = config_from_spec(name, spec)
            if cfg is None:
                self._log(f"[mcp] 跳过 {name}: 配置需要 command 或 url")
                continue
            desired[name] = cfg

        # 删除的服务器
        for name in list(self.clients):
            if name not in desired:
                await self._stop(name)
                self._log(f"[mcp] {name} 已停止（配置移除）")

        # 新增/变更的服务器
        for name, cfg in desired.items():
            if name in self.clients and self.configs.get(name) == cfg:
                continue  # 配置未变，保持连接
            if name in self.clients:
                await self._stop(name)
                self._log(f"[mcp] {name} 配置变更，重启中…")
            await self._start(name, cfg)

    async def _start(self, name: str, cfg: MCPServerConfig) -> bool:
        client = MCPClient(cfg, sampler=self._sampler, roots=self._roots)
        try:
            await client.start()
            tools = await discover_tools(client)
        except Exception as e:
            self._log(f"[mcp] 服务器 {name} 启动失败，已跳过: {type(e).__name__}: {e}")
            await client.close()
            return False
        self.clients[name] = client
        self.configs[name] = cfg
        for tool in tools:
            self.registry.register(tool)
        transport_kind = getattr(client._transport, "kind", "?")
        info = client.server_info.get("name", name)
        self._log(f"[mcp] {name}（{info}，{transport_kind}）已连接，{len(tools)} 个远程工具")
        return True

    async def _stop(self, name: str) -> None:
        client = self.clients.pop(name, None)
        self.configs.pop(name, None)
        removed = self.registry.unregister_prefix(server_prefix(name))
        if client is not None:
            await client.close()
        return removed

    # -- 热重启 ---------------------------------------------------------------------

    async def check_and_restart(self) -> List[str]:
        """把已断开的服务器重启并重注册工具；返回成功重启的名字列表。"""
        restarted: List[str] = []
        for name, client in list(self.clients.items()):
            if client.alive:
                continue
            cfg = self.configs.get(name)
            self.registry.unregister_prefix(server_prefix(name))
            self.clients.pop(name, None)
            await client.close()
            if cfg is not None and await self._start(name, cfg):
                restarted.append(name)
        return restarted

    # -- 观测 -----------------------------------------------------------------------

    def status(self) -> List[Dict[str, Any]]:
        out = []
        for name, client in self.clients.items():
            prefix = server_prefix(name)
            tool_count = len([n for n in self.registry.names() if n.startswith(prefix)])
            out.append({
                "name": name,
                "transport": getattr(client._transport, "kind", "?"),
                "alive": client.alive,
                "tools": tool_count,
                "server": client.server_info.get("name", ""),
            })
        return out

    async def close_all(self) -> None:
        for name in list(self.clients):
            client = self.clients.pop(name, None)
            self.configs.pop(name, None)
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
