"""Unified runtime resource ownership."""

from __future__ import annotations

import asyncio

from krelcode.contracts.runtime import RuntimeResourcePort
from krelcode.runtime.execution import LocalRuntimeResourceRegistry


class Resource(RuntimeResourcePort):
    def __init__(self, name: str, closes: list[str], *, fail: bool = False):
        self._name = name
        self._closes = closes
        self._fail = fail
        self.calls = 0

    @property
    def resource_name(self) -> str:
        return self._name

    async def close(self) -> None:
        self.calls += 1
        self._closes.append(self._name)
        if self._fail:
            raise RuntimeError("close failed")


def test_resource_registry_closes_in_reverse_order_and_is_idempotent() -> None:
    closes: list[str] = []
    registry = LocalRuntimeResourceRegistry()
    registry.register(Resource("first", closes))
    registry.register(Resource("second", closes))
    registry.register(Resource("second", closes))

    async def scenario() -> None:
        assert await registry.close_all() == ()
        assert await registry.close_all() == ()

    asyncio.run(scenario())
    assert closes == ["second", "first"]


def test_resource_registry_closes_remaining_resources_after_failure() -> None:
    closes: list[str] = []
    registry = LocalRuntimeResourceRegistry()
    registry.register(Resource("first", closes))
    registry.register(Resource("second", closes, fail=True))

    errors = asyncio.run(registry.close_all())

    assert closes == ["second", "first"]
    assert len(errors) == 1
    assert errors[0].startswith("second: RuntimeError")
