"""HTTP transport for explicitly enabled remote telemetry."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Mapping, Protocol, Sequence

from .config import validate_endpoint
from .privacy import TELEMETRY_SCHEMA_VERSION


class TelemetryTransportError(RuntimeError):
    """A remote telemetry batch could not be delivered."""


class TelemetryTransport(Protocol):
    async def send(self, events: Sequence[Mapping[str, Any]]) -> None:
        ...


class HttpTelemetryTransport:
    """Small zero-dependency JSON batch sender."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 10.0,
        user_agent: str = "krelcode-telemetry/1",
    ) -> None:
        self.endpoint = validate_endpoint(endpoint)
        self.timeout = max(1.0, float(timeout))
        self.user_agent = user_agent

    async def send(self, events: Sequence[Mapping[str, Any]]) -> None:
        if not events:
            return
        payload = {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "events": [dict(event) for event in events],
        }
        await asyncio.to_thread(self._send_sync, payload)

    def _send_sync(self, payload: Mapping[str, Any]) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
                response.read(4096)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TelemetryTransportError(
                f"telemetry 上传失败: {type(exc).__name__}") from exc
        if not 200 <= status < 300:
            raise TelemetryTransportError(
                f"telemetry 上传返回 HTTP {status}")


__all__ = [
    "HttpTelemetryTransport",
    "TelemetryTransport",
    "TelemetryTransportError",
]
