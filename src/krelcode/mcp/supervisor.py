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

import asyncio
import os
import re
from typing import Any, Callable, Dict, List, Optional

from ..tools.base import ToolDescriptor, ToolSource
from ..tools.registry import ToolRegistry
from .client import MCPClient, MCPServerConfig
from .tools import discover_tools, sanitize, server_prefix


_ENV_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_VARIABLE.sub(
            lambda match: os.environ.get(match.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_expand_env(item) for item in value)
    if isinstance(value, dict):
        return {str(key): _expand_env(item) for key, item in value.items()}
    return value


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
        headers=dict(_expand_env(spec.get("headers") or {})),
        oauth=dict(_expand_env(spec.get("oauth") or {})),
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
        self.prompts: Dict[str, tuple[Dict[str, Any], ...]] = {}
        self.resources: Dict[str, tuple[Dict[str, Any], ...]] = {}
        self.resource_templates: Dict[str, tuple[Dict[str, Any], ...]] = {}
        self.on_prompts_changed: Optional[Callable[[], Any]] = None
        self._refresh_locks: Dict[str, asyncio.Lock] = {}

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
        for name in list(self.configs):
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
        self.configs[name] = cfg
        client = MCPClient(
            cfg,
            sampler=self._sampler,
            roots=self._roots,
            on_notification=(
                lambda method, params, server=name:
                self._handle_notification(server, method, params)),
            log=self._log,
        )
        try:
            await client.start()
            tools = await discover_tools(client)
            prompts = await self._load_prompts(name, client)
            resources = await self._load_resources(name, client)
            templates = await self._load_resource_templates(name, client)
        except Exception as e:
            self._log(f"[mcp] 服务器 {name} 启动失败，已跳过: {type(e).__name__}: {e}")
            await client.close()
            return False
        self.clients[name] = client
        self.prompts[name] = prompts
        self.resources[name] = resources
        self.resource_templates[name] = templates
        for tool in tools:
            self.registry.register(
                tool,
                ToolDescriptor.from_tool(
                    tool, source=ToolSource.MCP, mcp_server=name),
            )
        transport_kind = getattr(client._transport, "kind", "?")
        info = client.server_info.get("name", name)
        self._log(f"[mcp] {name}（{info}，{transport_kind}）已连接，{len(tools)} 个远程工具")
        return True

    async def _stop(self, name: str) -> None:
        client = self.clients.pop(name, None)
        self.configs.pop(name, None)
        self.prompts.pop(name, None)
        self.resources.pop(name, None)
        self.resource_templates.pop(name, None)
        self._refresh_locks.pop(name, None)
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

    async def refresh_prompts(self) -> None:
        """配置未变时也重新拉取 prompts/list（供 /mcp reload）。"""
        for name, client in list(self.clients.items()):
            if client.alive:
                self.prompts[name] = await self._load_prompts(name, client)
        await self._notify_prompts_changed()

    async def refresh_metadata(self, name: str, kind: Optional[str] = None) -> None:
        """Refresh one server after list_changed or /mcp reload.

        kind is one of tools/resources/prompts; None refreshes every discoverable
        category. Tool replacement is validated before registry mutation.
        """
        client = self.clients.get(name)
        if client is None or not client.alive:
            return
        lock = self._refresh_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if kind in (None, "tools") and client.supports("tools"):
                tools = await discover_tools(client)
                self._replace_server_tools(name, tools)
            if kind in (None, "resources") and client.supports("resources"):
                self.resources[name] = await self._load_resources(name, client)
                self.resource_templates[name] = (
                    await self._load_resource_templates(name, client))
            if kind in (None, "prompts") and client.supports("prompts"):
                self.prompts[name] = await self._load_prompts(name, client)
                await self._notify_prompts_changed()

    def _replace_server_tools(self, name: str, tools: list[Any]) -> None:
        names = [str(tool.name) for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError(f"MCP 服务器 {name!r} 返回了重复工具名")
        prefix = server_prefix(name)
        conflicting = [
            tool_name for tool_name in names
            if not tool_name.startswith(prefix)
            or (self.registry.get(tool_name) is not None
                and not tool_name.startswith(prefix))
        ]
        if conflicting:
            raise ValueError(
                f"MCP 服务器 {name!r} 工具命名冲突: {', '.join(conflicting)}")
        self.registry.unregister_prefix(prefix)
        for tool in tools:
            self.registry.register(
                tool,
                ToolDescriptor.from_tool(
                    tool, source=ToolSource.MCP, mcp_server=name),
            )
        self._log(f"[mcp] {name} 工具列表已刷新：{len(tools)} 个")

    async def _handle_notification(
        self, name: str, method: str, _params: Dict[str, Any],
    ) -> None:
        kind = {
            "notifications/tools/list_changed": "tools",
            "notifications/resources/list_changed": "resources",
            "notifications/prompts/list_changed": "prompts",
        }.get(method)
        if kind is None:
            return
        try:
            await self.refresh_metadata(name, kind)
        except Exception as exc:
            self._log(
                f"[mcp] {name} {kind}/list_changed 刷新失败: "
                f"{type(exc).__name__}: {exc}")

    async def _notify_prompts_changed(self) -> None:
        if self.on_prompts_changed is None:
            return
        try:
            pending = self.on_prompts_changed()
            if asyncio.iscoroutine(pending):
                await pending
        except Exception as exc:
            self._log(
                f"[mcp] Prompt 命令刷新失败: {type(exc).__name__}: {exc}")

    async def authorize(self, name: str) -> None:
        """Run the interactive OAuth flow and restart the server if needed."""
        cfg = self.configs.get(name)
        if cfg is None:
            raise KeyError(f"未知 MCP 服务器: {name}")
        if cfg.oauth == {}:
            raise ValueError(f"MCP 服务器 {name!r} 未配置 OAuth")
        existing = self.clients.get(name)
        client = existing or MCPClient(
            cfg,
            sampler=self._sampler,
            roots=self._roots,
            log=self._log,
        )
        if client.oauth is None:
            raise ValueError(f"MCP 服务器 {name!r} 的 OAuth 配置不可用")
        await client.oauth.authorize_interactive()
        if existing is not None:
            await self._stop(name)
            self.configs[name] = cfg
            if not await self._start(name, cfg):
                raise RuntimeError(f"MCP 服务器 {name!r} OAuth 后重启失败")
        elif not await self._start(name, cfg):
            raise RuntimeError(f"MCP 服务器 {name!r} OAuth 后启动失败")

    async def _load_prompts(
        self, name: str, client: MCPClient,
    ) -> tuple[Dict[str, Any], ...]:
        if not client.supports("prompts"):
            return ()
        try:
            return tuple(await client.list_prompts())
        except Exception as exc:
            self._log(
                f"[mcp] {name} prompts/list 失败，继续使用其他能力: "
                f"{type(exc).__name__}: {exc}")
            return ()

    async def _load_resources(
        self, name: str, client: MCPClient,
    ) -> tuple[Dict[str, Any], ...]:
        if not client.supports("resources"):
            return ()
        try:
            return tuple(await client.list_resources())
        except Exception as exc:
            self._log(
                f"[mcp] {name} resources/list 失败，继续使用其他能力: "
                f"{type(exc).__name__}: {exc}")
            return ()

    async def _load_resource_templates(
        self, name: str, client: MCPClient,
    ) -> tuple[Dict[str, Any], ...]:
        if not client.supports("resources"):
            return ()
        try:
            return tuple(await client.list_resource_templates())
        except Exception as exc:
            self._log(
                f"[mcp] {name} resources/templates/list 失败，"
                f"继续使用其他能力: {type(exc).__name__}: {exc}")
            return ()

    def prompt_records(self) -> List[Dict[str, Any]]:
        """命令层可消费的 Prompt 元数据快照。"""
        records: List[Dict[str, Any]] = []
        for server, prompts in self.prompts.items():
            for prompt in prompts:
                remote_name = str(prompt.get("name", "")).strip()
                if not remote_name:
                    continue
                arguments = prompt.get("arguments") or []
                argument_names = tuple(
                    str(item.get("name"))
                    for item in arguments
                    if isinstance(item, dict) and item.get("name")
                )
                records.append({
                    "command_name": f"mcp__{sanitize(server)}__{sanitize(remote_name)}",
                    "server": server,
                    "prompt_name": remote_name,
                    "description": str(prompt.get("description") or ""),
                    "argument_names": argument_names,
                })
        return records

    async def get_prompt(
        self, server: str, name: str, arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        client = self.clients.get(server)
        if client is None:
            raise RuntimeError(f"MCP 服务器 {server!r} 未连接")
        return await client.get_prompt(name, arguments)

    # -- 观测 -----------------------------------------------------------------------

    def status(self) -> List[Dict[str, Any]]:
        out = []
        for name, cfg in self.configs.items():
            client = self.clients.get(name)
            prefix = server_prefix(name)
            tool_count = len([n for n in self.registry.names() if n.startswith(prefix)])
            transport_name = (
                getattr(client._transport, "kind", "?")
                if client is not None else
                (cfg.transport or ("sse" if cfg.url else "stdio"))
            )
            out.append({
                "name": name,
                "transport": transport_name,
                "alive": bool(client is not None and client.alive),
                "tools": tool_count,
                "prompts": len(self.prompts.get(name, ())),
                "resources": len(self.resources.get(name, ())),
                "resource_templates": len(self.resource_templates.get(name, ())),
                "oauth": bool(
                    client is not None and client.oauth is not None),
                "server": (
                    client.server_info.get("name", "")
                    if client is not None else ""),
                "instructions": bool(
                    client is not None and client.instructions.strip()),
            })
        return out

    def server_instructions(self) -> tuple[tuple[str, str], ...]:
        """Return connected server guidance without exposing it in status logs."""
        entries = []
        for name, client in self.clients.items():
            if not client.alive:
                continue
            text = str(client.instructions or "").strip()
            if text:
                entries.append((name, text))
        return tuple(sorted(entries))

    async def close_all(self) -> None:
        for name in list(self.clients):
            client = self.clients.pop(name, None)
            self.configs.pop(name, None)
            self.prompts.pop(name, None)
            self.resources.pop(name, None)
            self.resource_templates.pop(name, None)
            self._refresh_locks.pop(name, None)
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
