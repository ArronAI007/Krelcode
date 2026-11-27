"""Runtime-scoped long-lived resource ownership."""

from __future__ import annotations

from typing import Awaitable, Callable

from krelcode.contracts.runtime import (
    RuntimeResourcePort,
    RuntimeResourceRegistryPort,
)


class RuntimeResourceAdapter(RuntimeResourcePort):
    """Adapt an existing async close method to the runtime resource contract."""

    def __init__(
        self,
        name: str,
        closer: Callable[[], Awaitable[None]],
    ) -> None:
        self._name = str(name)
        self._closer = closer
        self._closed = False

    @property
    def resource_name(self) -> str:
        return self._name

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._closer()


class LocalRuntimeResourceRegistry(RuntimeResourceRegistryPort):
    """Register resources once and close them in reverse acquisition order."""

    def __init__(self) -> None:
        self._resources: list[RuntimeResourcePort] = []
        self._names: set[str] = set()
        self._closed = False
        self.last_errors: tuple[str, ...] = ()

    @property
    def resources(self) -> tuple[RuntimeResourcePort, ...]:
        return tuple(self._resources)

    def register(self, resource: RuntimeResourcePort) -> None:
        if self._closed:
            raise RuntimeError("RuntimeResourceRegistry 已关闭")
        name = resource.resource_name
        if name in self._names:
            return
        self._names.add(name)
        self._resources.append(resource)

    async def close_all(self) -> tuple[str, ...]:
        if self._closed:
            return self.last_errors
        self._closed = True
        errors: list[str] = []
        for resource in reversed(self._resources):
            try:
                await resource.close()
            except Exception as exc:
                errors.append(
                    f"{resource.resource_name}: {type(exc).__name__}: {exc}")
        self.last_errors = tuple(errors)
        return self.last_errors


__all__ = ["LocalRuntimeResourceRegistry", "RuntimeResourceAdapter"]
