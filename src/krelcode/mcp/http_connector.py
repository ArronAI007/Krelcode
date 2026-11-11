"""HTTPConnector 端口：StreamableHTTPTransport 的底层 HTTP 抽象。

同步接口（transport 在线程里调用）；测试注入 Fake 即可零网络。
响应对象同时支持整体读取（JSON 模式）与逐行迭代（SSE 模式）。
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional, Protocol

from .transport import MCPError

Headers = Dict[str, str]


class HTTPResponse(Protocol):
    status: int
    headers: Headers  # 键已小写

    def read(self) -> bytes: ...
    def iter_lines(self) -> Iterator[bytes]: ...
    def close(self) -> None: ...


class HTTPConnector(Protocol):
    def request(
        self, method: str, url: str, headers: Headers,
        body: Optional[bytes], timeout: float,
    ) -> HTTPResponse: ...

    def close_stream(self) -> None:
        """关闭仍打开的流式响应（GET 监听流），幂等。"""
        ...


@dataclass
class BytesHTTPResponse:
    """内存响应（HTTP 错误体 / 测试 Fake 复用）。"""
    status: int
    headers: Headers
    body: bytes = b""

    def read(self) -> bytes:
        return self.body

    def iter_lines(self) -> Iterator[bytes]:
        return iter(self.body.splitlines(keepends=True))

    def close(self) -> None:
        pass


class UrllibHTTPConnector:
    """默认实现：标准库 urllib；4xx/5xx 也作为响应返回（可读到错误体）。"""

    def __init__(self) -> None:
        self._open: Optional[Any] = None

    def request(self, method, url, headers, body, timeout) -> HTTPResponse:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read()
            except Exception:
                err_body = b""
            return BytesHTTPResponse(
                e.code, _lower(getattr(e, "headers", None)), err_body)
        except urllib.error.URLError as e:
            raise MCPError(f"HTTP 连接失败: {e.reason}") from e
        except TimeoutError as e:
            raise MCPError("HTTP 请求超时") from e
        if method == "GET":
            self._open = resp  # 监听流：留引用供 close_stream
        return _UrllibResponse(resp)

    def close_stream(self) -> None:
        resp, self._open = self._open, None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


@dataclass
class _UrllibResponse:
    _resp: Any = field(repr=False)

    @property
    def status(self) -> int:
        return self._resp.status

    @property
    def headers(self) -> Headers:
        return _lower(self._resp.headers)

    def read(self) -> bytes:
        try:
            return self._resp.read()
        finally:
            self.close()

    def iter_lines(self) -> Iterator[bytes]:
        return iter(self._resp.readline, b"")

    def close(self) -> None:
        try:
            self._resp.close()
        except Exception:
            pass


def _lower(headers: Any) -> Headers:
    if not headers:
        return {}
    return {k.lower(): v for k, v in headers.items()}
