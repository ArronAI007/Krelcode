"""MCPTransport 端口：JSON-RPC 消息的双向管道抽象。

MCPClient 只面对本端口；stdio（子进程管道）与 SSE（HTTP 长连接）
是两个可替换实现。connect 回调是**同步函数**（SSE 实现从 reader
线程经 call_soon_threadsafe 调用，stdio 实现从 asyncio task 调用）。
"""

from __future__ import annotations

from typing import Callable, Dict, Any, Protocol

from ..core.errors import KrelcodeError


class MCPError(KrelcodeError):
    """MCP 通信/协议错误（client 与各 transport 共用同一类型）。"""


class MCPTransport(Protocol):
    kind: str  # "stdio" | "sse"

    async def connect(
        self,
        on_message: Callable[[Dict[str, Any]], None],
        on_disconnect: Callable[[str], None],
    ) -> None:
        """建立连接并启动读取循环。

        on_message: 收到一条 JSON-RPC 消息（响应/通知/服务器请求）
        on_disconnect: 连接终结（EOF/崩溃/远端关闭），带原因说明；只调用一次
        """
        ...

    async def send(self, payload: Dict[str, Any]) -> None:
        """发送一条 JSON-RPC 消息（请求或通知）。连接已断时抛 MCPError。"""
        ...

    async def close(self) -> None:
        """主动关闭（幂等）。"""
        ...

    @property
    def alive(self) -> bool:
        """连接是否存活（未关闭且未断开）。"""
        ...
