"""StdioTransport：子进程 stdin/stdout，换行分隔 JSON-RPC（原 MCPClient 内置逻辑）。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from ..safety.environment import scrub_environment
from .transport import MCPError


def build_stdio_environment(overlay: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """从宿主环境剥离秘密，再合并用户在 MCP 配置中显式声明的环境变量。"""
    env = scrub_environment(os.environ)
    env.update(dict(overlay or {}))
    return env


class StdioTransport:
    kind = "stdio"

    def __init__(self, command: str, args: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None) -> None:
        self.command = command
        self.args = list(args or [])
        self.env_overlay = dict(env or {})
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._closed = False
        self._disconnected = False

    async def connect(self, on_message, on_disconnect) -> None:
        env = build_stdio_environment(self.env_overlay)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command, *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except (OSError, FileNotFoundError) as e:
            raise MCPError(f"无法启动 MCP 服务器进程: {e}") from e
        self._reader_task = asyncio.create_task(
            self._read_loop(on_message, on_disconnect))

    async def _read_loop(self, on_message, on_disconnect) -> None:
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
                    continue  # 忽略非 JSON 噪音
                on_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._disconnected = True
            on_disconnect(f"stdio 读取异常: {e}")
            return
        self._disconnected = True
        if not self._closed:
            on_disconnect("MCP 服务器进程已退出（EOF）")

    async def send(self, payload: Dict[str, Any]) -> None:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise MCPError("stdio 传输未连接")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            raise MCPError(f"stdio 管道已断开: {e}") from e

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

    @property
    def alive(self) -> bool:
        return (not self._closed and not self._disconnected
                and self._proc is not None and self._proc.returncode is None)
