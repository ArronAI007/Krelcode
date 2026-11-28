"""Daemon-backed client for connection-independent runtime tasks."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from ..contracts.application import ApplicationRequest
from .client_transport import DaemonClient
from .protocol import DEFAULT_RPC_TIMEOUT


class DaemonTaskClient:
    def __init__(
        self,
        workspace_root: Path,
        *,
        client: Optional[DaemonClient] = None,
        idle_timeout: Optional[float] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.client = client or DaemonClient(self.workspace_root)
        self.idle_timeout = idle_timeout

    async def submit(
        self,
        message: str,
        *,
        application: Optional[ApplicationRequest] = None,
        task_id: str = "",
        parent_task_id: str = "",
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        return cast(Dict[str, Any], await self._request("task.submit", {
            "message": message,
            "application": (
                application.to_dict() if application is not None else None),
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "idempotency_key": idempotency_key,
        }))

    async def attach(self, task_id: str) -> Dict[str, Any]:
        return cast(
            Dict[str, Any],
            await self._request("task.attach", {"task_id": task_id}),
        )

    async def inspect(self, task_id: str) -> Dict[str, Any]:
        return cast(
            Dict[str, Any],
            await self._request("task.inspect", {"task_id": task_id}),
        )

    async def events(
        self,
        task_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1_000,
    ) -> List[Dict[str, Any]]:
        data = await self._request("task.events", {
            "task_id": task_id,
            "after_sequence": max(0, int(after_sequence)),
            "limit": max(1, int(limit)),
        })
        rows = data.get("events")
        return [dict(item) for item in rows] if isinstance(rows, list) else []

    async def list(self) -> List[Dict[str, Any]]:
        data = await self._request("task.list", {})
        return [dict(item) for item in data] if isinstance(data, list) else []

    async def cancel(self, task_id: str) -> bool:
        data = await self._request("task.cancel", {"task_id": task_id})
        return bool(data.get("cancelled"))

    async def pause(self, task_id: str) -> bool:
        data = await self._request("task.pause", {"task_id": task_id})
        return bool(data.get("paused"))

    async def resume(self, task_id: str) -> bool:
        data = await self._request("task.resume", {"task_id": task_id})
        return bool(data.get("resumed"))

    async def recover(self, task_id: str) -> Dict[str, Any]:
        return cast(
            Dict[str, Any],
            await self._request(
                "task.recover", {"task_id": task_id}, timeout=60.0),
        )

    async def compensate(
        self,
        task_id: str,
        effect_id: str,
        *,
        succeeded: bool = True,
        compensation: str = "",
        evidence: Optional[Dict[str, Any]] = None,
    ) -> bool:
        data = await self._request("task.compensate", {
            "task_id": task_id,
            "effect_id": effect_id,
            "succeeded": succeeded,
            "compensation": compensation,
            "evidence": evidence or {},
        })
        return bool(data.get("updated"))

    async def _request(
        self,
        operation: str,
        params: Dict[str, Any],
        *,
        timeout: float = DEFAULT_RPC_TIMEOUT,
    ) -> Any:
        await self.client.ensure_started(idle_timeout=self.idle_timeout)
        return await self.client.request(operation, params, timeout=timeout)


__all__ = ["DaemonTaskClient"]
