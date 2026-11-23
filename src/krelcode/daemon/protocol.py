"""Line-delimited JSON protocol shared by the daemon and its clients."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
DEFAULT_RPC_TIMEOUT = 30.0


class ProtocolError(RuntimeError):
    def __init__(self, message: str, *, code: str = "protocol_error") -> None:
        super().__init__(message)
        self.code = code


class RPCError(RuntimeError):
    def __init__(self, message: str, *, code: str = "rpc_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RPCRequest:
    id: str
    operation: str
    params: Dict[str, Any]
    token: str = ""

    @classmethod
    def create(
        cls, operation: str, params: Optional[Dict[str, Any]] = None, *, token: str = "",
    ) -> "RPCRequest":
        return cls(
            id=uuid.uuid4().hex,
            operation=str(operation or "").strip(),
            params=dict(params or {}),
            token=str(token or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": PROTOCOL_VERSION,
            "id": self.id,
            "operation": self.operation,
            "params": self.params,
            "token": self.token,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "RPCRequest":
        if int(raw.get("schema") or 0) != PROTOCOL_VERSION:
            raise ProtocolError(
                f"不支持 daemon 协议版本: {raw.get('schema')}", code="version")
        request_id = str(raw.get("id") or "").strip()
        operation = str(raw.get("operation") or "").strip()
        params = raw.get("params") or {}
        if not request_id or not operation:
            raise ProtocolError("request id/operation 不能为空")
        if not isinstance(params, dict):
            raise ProtocolError("request params 必须是 object")
        return cls(
            id=request_id,
            operation=operation,
            params=dict(params),
            token=str(raw.get("token") or ""),
        )


@dataclass(frozen=True)
class RPCResponse:
    id: str
    ok: bool
    result: Any = None
    error: Optional[Dict[str, str]] = None

    @classmethod
    def success(cls, request_id: str, result: Any = None) -> "RPCResponse":
        return cls(request_id, True, result=result)

    @classmethod
    def failure(
        cls, request_id: str, message: str, *, code: str = "rpc_error",
    ) -> "RPCResponse":
        return cls(
            id=request_id,
            ok=False,
            error={"code": str(code), "message": str(message)},
        )

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "schema": PROTOCOL_VERSION,
            "id": self.id,
            "ok": self.ok,
        }
        if self.ok:
            value["result"] = self.result
        else:
            value["error"] = dict(self.error or {
                "code": "rpc_error", "message": "unknown daemon error"})
        return value

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "RPCResponse":
        if int(raw.get("schema") or 0) != PROTOCOL_VERSION:
            raise ProtocolError("daemon 响应协议版本不匹配")
        request_id = str(raw.get("id") or "").strip()
        if not request_id:
            raise ProtocolError("daemon 响应缺少 request id")
        error = raw.get("error")
        if error is not None and not isinstance(error, dict):
            raise ProtocolError("daemon 响应 error 必须是 object")
        return cls(
            id=request_id,
            ok=bool(raw.get("ok")),
            result=raw.get("result"),
            error=(
                {str(k): str(v) for k, v in error.items()}
                if isinstance(error, dict) else None
            ),
        )


def encode_message(value: Any) -> bytes:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str,
    ).encode("utf-8")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ProtocolError("daemon 消息超过大小上限", code="message_too_large")
    return encoded + b"\n"


async def read_message(
    reader: asyncio.StreamReader,
    *,
    timeout: float = DEFAULT_RPC_TIMEOUT,
) -> Dict[str, Any]:
    try:
        raw = await asyncio.wait_for(
            reader.readline(), timeout=max(0.05, float(timeout)))
    except asyncio.TimeoutError as exc:
        raise ProtocolError("daemon 请求读取超时", code="timeout") from exc
    if not raw:
        raise ProtocolError("daemon 连接在请求前关闭", code="eof")
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ProtocolError("daemon 请求超过大小上限", code="message_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"daemon 请求不是有效 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("daemon 请求必须是 JSON object")
    return value


async def write_message(writer: asyncio.StreamWriter, value: Any) -> None:
    writer.write(encode_message(value))
    await writer.drain()


__all__ = [
    "DEFAULT_RPC_TIMEOUT",
    "MAX_MESSAGE_BYTES",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "RPCError",
    "RPCRequest",
    "RPCResponse",
    "encode_message",
    "read_message",
    "write_message",
]
