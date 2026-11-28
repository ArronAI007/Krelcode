"""Agent gateway task routing and tenant lifecycle."""

from __future__ import annotations

import asyncio

from krelcode.contracts.gateway import (
    AgentGatewayRequest,
    GatewayContext,
    GatewayLayer,
    TenantQuota,
)
from krelcode.contracts.application import ApplicationRequestFactory
from krelcode.contracts.runtime import (
    RuntimeTask,
    RuntimeTaskExecutorPort,
    RuntimeTaskHandle,
    RuntimeTaskRuntimePort,
    RuntimeTaskState,
    RuntimeTaskStatus,
)
from krelcode.gateway.agent import AgentGateway
from krelcode.gateway.quota import InMemoryTenantQuota


class FakeExecutor(RuntimeTaskExecutorPort):
    async def execute(self, task: RuntimeTask) -> str:
        return f"done:{task.message}"


class FakeHandle(RuntimeTaskHandle):
    def __init__(self, task_id: str, turn_id: str) -> None:
        self._task_id = task_id
        self._turn_id = turn_id

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def turn_id(self) -> str:
        return self._turn_id

    @property
    def state(self) -> RuntimeTaskState:
        return RuntimeTaskState(
            self._task_id,
            self._turn_id,
            RuntimeTaskStatus.RUNNING,
        )

    @property
    def done(self) -> bool:
        return False

    async def wait(self) -> str:
        return ""

    async def cancel(self) -> None:
        return None

    async def pause(self) -> bool:
        return True

    async def resume(self) -> bool:
        return True


class FakeRuntime(RuntimeTaskRuntimePort):
    def __init__(self) -> None:
        self.task: RuntimeTask | None = None
        self.executor: RuntimeTaskExecutorPort | None = None

    def submit(
        self,
        task: RuntimeTask,
        executor: RuntimeTaskExecutorPort,
    ) -> RuntimeTaskHandle:
        self.task = task
        self.executor = executor
        return FakeHandle("task-1", task.turn_id)

    async def cancel(self, task_id: str) -> bool:
        return True

    async def pause(self, task_id: str) -> bool:
        return True

    async def resume(self, task_id: str) -> bool:
        return True

    def inspect(self, task_id: str) -> RuntimeTaskState:
        raise KeyError(task_id)

    def state_ref(self, task_id: str):
        raise KeyError(task_id)

    def checkpoint(self, task_id: str):
        return None

    def record_checkpoint(self, task_id: str, checkpoint: object):
        raise KeyError(task_id)


def test_agent_gateway_submits_task_and_releases_tenant_slot() -> None:
    runtime = FakeRuntime()
    quota = InMemoryTenantQuota(TenantQuota(
        max_concurrent=1,
        max_requests_per_minute=10,
        max_tokens_per_minute=10,
    ))
    gateway = AgentGateway(
        runtime,
        default_executor=FakeExecutor(),
        allowed_agents=("default",),
        quota=quota,
    )

    async def run() -> None:
        result = await gateway.submit(AgentGatewayRequest(
            context=GatewayContext(
                request_id="req-1",
                tenant_id="tenant",
            ),
            message="implement",
            session_id="session-1",
            application=ApplicationRequestFactory.from_input_source(
                "sdk", goal="implement"),
        ))
        assert result.accepted
        assert result.task_id == "task-1"
        assert runtime.task is not None
        assert runtime.task.metadata["gateway_request_id"] == "req-1"
        assert quota.snapshot(
            "tenant", layer=GatewayLayer.AGENT)["active"] == 1
        assert runtime.executor is not None
        assert await runtime.executor.execute(runtime.task) == "done:implement"
        assert quota.snapshot(
            "tenant", layer=GatewayLayer.AGENT)["active"] == 0

    asyncio.run(run())


def test_agent_gateway_rejects_disallowed_definition() -> None:
    gateway = AgentGateway(
        FakeRuntime(),
        default_executor=FakeExecutor(),
        allowed_agents=("review",),
    )

    result = asyncio.run(gateway.submit(AgentGatewayRequest(
        context=GatewayContext(request_id="req-1"),
        message="implement",
        agent_definition="implementer",
    )))

    assert not result.accepted
    assert "允许列表" in result.reason
