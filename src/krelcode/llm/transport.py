"""HTTP 传输抽象：同步实现（urllib）+ asyncio 桥 + SSE 解析。

传输层被锁在 llm 包内部；adapter 只面对 TransportResponse / aiter_lines /
sse_data_events 三个统一入口。测试注入 FakeTransport 即可完全离线。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Iterator, Optional, Protocol

from ..core.errors import LLMError

_SENTINEL = object()


# ---------------------------------------------------------------------------
# 传输抽象
# ---------------------------------------------------------------------------

class TransportResponse(Protocol):
    status: int
    headers: Dict[str, str]

    def read(self) -> bytes: ...
    def iter_lines(self) -> Iterator[bytes]: ...
    def close(self) -> None: ...


class Transport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> TransportResponse: ...


@dataclass
class UrllibResponse:
    status: int
    headers: Dict[str, str]
    _resp: Any = field(repr=False)

    def read(self) -> bytes:
        try:
            return self._resp.read()
        finally:
            self.close()

    def iter_lines(self) -> Iterator[bytes]:
        try:
            for line in self._resp:
                yield line
        finally:
            self.close()

    def close(self) -> None:
        try:
            self._resp.close()
        except Exception:
            pass


class UrllibTransport:
    """默认传输：标准库 urllib。HTTP 错误(4xx/5xx)也作为响应返回，
    由 adapter 决定如何抛错（能读到错误响应体，便于诊断）。"""

    def send(self, method, url, headers, body, timeout) -> TransportResponse:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            return UrllibResponse(resp.status, _lower(resp.headers), resp)
        except urllib.error.HTTPError as e:
            raw_body = b""
            try:
                raw_body = e.read()
            except Exception:
                pass
            return _BytesResponse(e.code, _lower(getattr(e, "headers", None)), raw_body)
        except urllib.error.URLError as e:
            raise LLMError(f"LLM 网络错误: {e.reason}") from e
        except TimeoutError as e:
            raise LLMError("LLM 请求超时") from e


@dataclass
class _BytesResponse:
    """内存响应（HTTP 错误体 / 测试 FakeTransport 复用）。"""

    status: int
    headers: Dict[str, str]
    body: bytes

    def read(self) -> bytes:
        return self.body

    def iter_lines(self) -> Iterator[bytes]:
        return iter(self.body.splitlines(keepends=True))

    def close(self) -> None:
        pass


def _lower(headers: Any) -> Dict[str, str]:
    if not headers:
        return {}
    return {k.lower(): v for k, v in headers.items()}


# ---------------------------------------------------------------------------
# asyncio 桥：同步阻塞 IO → 协程
# ---------------------------------------------------------------------------

async def asend(
    transport: Transport,
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    timeout: float,
) -> TransportResponse:
    return await asyncio.to_thread(transport.send, method, url, headers, body, timeout)


async def aread(resp: TransportResponse) -> bytes:
    return await asyncio.to_thread(resp.read)


async def aiter_lines(resp: TransportResponse) -> AsyncIterator[bytes]:
    it = resp.iter_lines()
    while True:
        line = await asyncio.to_thread(next, it, _SENTINEL)
        if line is _SENTINEL:
            return
        yield line


# ---------------------------------------------------------------------------
# SSE 解析（OpenAI 与 Anthropic 的流式都是 SSE，帧结构一致，data 语义不同）
# ---------------------------------------------------------------------------

class SSEDone(Exception):
    """收到 data: [DONE]（OpenAI 风格终止标记）。"""


async def sse_data_events(resp: TransportResponse) -> AsyncIterator[str]:
    """解析 SSE 流，逐事件 yield data 字段内容（多行 data 用 \\n 连接）。

    - 空行 = 事件边界
    - 忽略 event: / id: / retry: 行（adapter 用 data 内的 type 字段分发）
    - data: [DONE] → 结束
    - 非法 JSON 在这里统一抛 LLMError
    """
    data_lines: list = []
    async for raw in aiter_lines(resp):
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data_lines:
                data = "\n".join(data_lines)
                data_lines = []
                if data.strip() == "[DONE]":
                    return
                yield data
            continue
        if line.startswith(":"):  # SSE 注释/心跳
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
        # 其他字段行（event:/id:/retry:）忽略
    if data_lines:
        yield "\n".join(data_lines)


async def parse_json_event(data: str, provider: str) -> Dict[str, Any]:
    try:
        return json.loads(data)
    except json.JSONDecodeError as e:
        raise LLMError(f"[{provider}] SSE data 非法 JSON: {data[:200]!r} ({e})") from e
