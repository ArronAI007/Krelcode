"""共享测试替身：FakeTransport（离线驱动真 provider 适配器）。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers: Optional[Dict[str, str]] = None):
        self.status = status
        self.headers = headers or {}
        self._body = body
        self.closed = False

    def read(self) -> bytes:
        self.closed = True
        return self._body

    def iter_lines(self) -> Iterator[bytes]:
        try:
            for line in self._body.splitlines(keepends=True):
                yield line
        finally:
            self.closed = True

    def close(self) -> None:
        self.closed = True


@dataclass
class CapturedRequest:
    method: str
    url: str
    headers: Dict[str, str]
    body: Optional[bytes]
    timeout: float

    @property
    def json(self) -> Any:
        return json.loads((self.body or b"{}").decode("utf-8"))


class FakeTransport:
    def __init__(self, body: bytes = b"", status: int = 200):
        self.body = body
        self.status = status
        self.requests: List[CapturedRequest] = []
        self.responses: List[FakeResponse] = []

    def send(self, method, url, headers, body, timeout) -> FakeResponse:
        self.requests.append(CapturedRequest(method, url, headers, body, timeout))
        resp = FakeResponse(self.body, self.status)
        self.responses.append(resp)
        return resp

    @property
    def last(self) -> CapturedRequest:
        return self.requests[-1]


def sse(*datas: str, done: bool = True) -> bytes:
    """构造 SSE 响应体。datas 为每项 data 字段内容（dict 会自动序列化）。"""
    out = b""
    for d in datas:
        text = d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
        out += f"data: {text}\n\n".encode("utf-8")
    if done:
        out += b"data: [DONE]\n\n"
    return out
