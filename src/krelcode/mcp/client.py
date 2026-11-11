"""MCP stdio 客户端：子进程 + 换行分隔 JSON-RPC 2.0。

生命周期：start()（spawn + initialize 握手）→ list_tools()/call_tool() → close()。
失败安全：服务器 EOF/崩溃时，所有 pending 请求立即以 MCPError 失败，
后续调用直接报错，不悬挂。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .. import __version__
from ..core.errors import KrelcodeError

PROTOCOL_VERSION = "2024-11-05"


class MCPError(KrelcodeError):
    """MCP 通信/协议错误。"""


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0


class MCPClient:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.name = config.name
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._closed = False
        self.server_info: Dict[str, Any] = {}

    # -- 生命周期 ---------------------------------------------------------------

    async def start(self) -> None:
        env = dict(os.environ)
        env.update(self.config.env or {})
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.config.command, *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except (OSError, FileNotFoundError) as e:
            raise MCPError(f"无法启动 MCP 服务器 {self.name!r}: {e}") from e
        self._reader_task = asyncio.create_task(self._read_loop())
        result = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "krelcode", "version": __version__},
        })
        self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        self._notify("notifications/initialized")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.close()
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError, OSError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        self._fail_all_pending("MCP 客户端已关闭")

    # -- JSON-RPC ----------------------------------------------------------------

    async def _request(self, method: str, params: Optional[dict] = None) -> Any:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise MCPError(f"MCP 服务器 {self.name!r} 未连接")
        self._next_id += 1
        rid = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
            ensure_ascii=False,
        ) + "\n"
        try:
            self._proc.stdin.write(payload.encode("utf-8"))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            self._pending.pop(rid, None)
            raise MCPError(f"MCP 服务器 {self.name!r} 管道已断开: {e}") from e
        try:
            return await asyncio.wait_for(future, timeout=self.config.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise MCPError(f"MCP 服务器 {self.name!r} 响应超时（{self.config.timeout:g}s, method={method}）") from None

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        payload = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params or {}},
            ensure_ascii=False,
        ) + "\n"
        try:
            self._proc.stdin.write(payload.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # 通知失败不致命

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break  # EOF：服务器退出
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue  # 忽略非 JSON 噪音行
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._fail_all_pending(f"MCP 服务器 {self.name!r} 已退出")

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        rid = msg.get("id")
        if rid is None:
            return  # 服务器通知：当前忽略
        future = self._pending.pop(rid, None)
        if future is None or future.done():
            return
        if "error" in msg:
            err = msg["error"]
            future.set_exception(
                MCPError(f"MCP {self.name!r} 错误 {err.get('code')}: {err.get('message')}")
            )
        else:
            future.set_result(msg.get("result"))

    def _fail_all_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPError(reason))
        self._pending.clear()

    # -- MCP 高层 API --------------------------------------------------------------

    async def list_tools(self) -> List[Dict[str, Any]]:
        result = await self._request("tools/list")
        tools = (result or {}).get("tools", [])
        if not isinstance(tools, list):
            raise MCPError(f"MCP 服务器 {self.name!r} 返回了非法 tools 结构")
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MCPError(f"MCP 服务器 {self.name!r} 返回了非法 tools/call 结果")
        return result
