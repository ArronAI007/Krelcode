"""REPL 审批 broker：非阻塞 rendezvous、fail-closed 与策略集成。"""

import asyncio
from pathlib import Path

import pytest

from krelcode.core.messages import ToolCall
from krelcode.safety.policy import SandboxPolicy
from krelcode.tools.base import ToolContext
from krelcode.ui.approval import ALWAYS, NO, YES, ApprovalBroker


def test_broker_hands_request_to_repl_and_resolves_answer():
    broker = ApprovalBroker()
    call = ToolCall("c1", "danger", {"value": "x"})

    async def scenario():
        policy_task = asyncio.create_task(broker.request(call, "需要批准"))
        request = await asyncio.wait_for(broker.next_request(), 1)
        assert request is not None
        assert request.tool_call is call
        assert request.reason == "需要批准"
        assert broker.resolve("always") == ALWAYS
        assert await policy_task == ALWAYS
        assert broker.pending is None

    asyncio.run(scenario())


def test_broker_cancel_all_fails_closed_for_pending_and_queued():
    broker = ApprovalBroker()
    first = ToolCall("c1", "one", {})
    second = ToolCall("c2", "two", {})

    async def scenario():
        one = asyncio.create_task(broker.request(first, "one"))
        two = asyncio.create_task(broker.request(second, "two"))
        await broker.next_request()
        broker.cancel_all()
        assert await one == NO
        assert await two == NO
        assert broker.pending is None

    asyncio.run(scenario())


def test_broker_close_rejects_late_requests():
    broker = ApprovalBroker()
    broker.close()
    assert asyncio.run(broker.request(ToolCall("c1", "x", {}), "late")) == NO


def test_broker_accepts_request_id_response_before_approver_registers():
    broker = ApprovalBroker()
    call = ToolCall("call-early", "unknown_tool", {})

    async def scenario():
        broker.expect("call-early")
        assert broker.resolve_for("call-early", "yes") == YES
        assert await asyncio.wait_for(
            broker.request(call, "需要批准"), timeout=1) == YES
        assert broker.resolve_for("call-early", "no") is None

    asyncio.run(scenario())


def test_broker_rejects_unexpected_permission_id():
    broker = ApprovalBroker()
    assert broker.resolve_for("forged", "yes") is None


def test_sandbox_policy_uses_broker_without_stdin(tmp_path: Path):
    broker = ApprovalBroker()
    policy = SandboxPolicy(approver=broker.request)
    call = ToolCall("c1", "unknown_tool", {"path": "x"})
    ctx = ToolContext(workspace_root=tmp_path)

    async def scenario():
        decision_task = asyncio.create_task(policy.check(call, ctx))
        request = await asyncio.wait_for(broker.next_request(), 1)
        assert request is not None
        broker.resolve("y")
        return await decision_task

    decision = asyncio.run(scenario())
    assert decision.allowed is True
    assert "用户批准" in decision.reason


def test_broker_cancelled_policy_wait_does_not_leave_live_future():
    broker = ApprovalBroker()
    call = ToolCall("c1", "unknown_tool", {})

    async def scenario():
        task = asyncio.create_task(broker.request(call, "ask"))
        request = await broker.next_request()
        assert request is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        broker.cancel_all()

    asyncio.run(scenario())


def test_broker_rejects_late_approver_after_cancel_all():
    broker = ApprovalBroker()
    call = ToolCall("call-late", "danger", {})

    async def scenario():
        broker.expect("call-late")
        broker.cancel_all()
        assert await asyncio.wait_for(
            broker.request(call, "取消后到达"), timeout=1) == NO

    asyncio.run(scenario())


def test_broker_discards_early_response_after_cancel_all():
    broker = ApprovalBroker()
    call = ToolCall("call-early-cancel", "danger", {})

    async def scenario():
        broker.expect("call-early-cancel")
        assert broker.resolve_for("call-early-cancel", "yes") == YES
        broker.cancel_all()
        assert await asyncio.wait_for(
            broker.request(call, "竞态取消"), timeout=1) == NO

    asyncio.run(scenario())
