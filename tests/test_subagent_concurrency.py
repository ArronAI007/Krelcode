"""Global subagent concurrency slots and waiting lifecycle."""

from __future__ import annotations

import asyncio
from pathlib import Path

from krelcode.core.messages import Message
from krelcode.llm.base import (
    ProviderCapabilities,
    StreamFinished,
)
from krelcode.safety.policy import AllowAllPolicy
from krelcode.subagent.manager import SubagentManager
from krelcode.tools.base import ToolContext


class BlockingProvider:
    name = "blocking"
    model = "blocking-model"
    capabilities = ProviderCapabilities()

    def __init__(self, release: asyncio.Event) -> None:
        self.release = release
        self.calls = 0

    async def stream(self, messages, tools=None):
        self.calls += 1
        await self.release.wait()
        yield StreamFinished(
            Message.assistant("done"),
            finish_reason="stop",
            usage={},
        )


def test_subagent_global_max_concurrency_blocks_second_spawn(
    tmp_path: Path,
) -> None:
    async def go() -> None:
        release = asyncio.Event()
        provider = BlockingProvider(release)
        manager = SubagentManager(
            llm=provider,
            tool_factory=lambda: [],
            tool_context=ToolContext(
                workspace_root=tmp_path,
                sandbox_enabled=False,
            ),
            policy=AllowAllPolicy(),
            max_concurrent=1,
        )
        first = await manager.spawn("first")
        second_spawn = asyncio.create_task(manager.spawn("second"))
        await asyncio.sleep(0.02)
        assert not second_spawn.done()
        release.set()
        second = await asyncio.wait_for(second_spawn, timeout=2)
        await manager.wait(first.id, timeout=2)
        await manager.wait(second.id, timeout=2)
        assert provider.calls == 2
        await manager.shutdown()

    asyncio.run(go())
