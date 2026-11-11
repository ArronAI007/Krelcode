"""core 层测试：规范化消息（wire-free）与事件总线。

wire format 互转的测试在各 provider 测试文件里（test_provider_*.py）。
"""

import asyncio

from krelcode.core.events import EventBus, RunStarted
from krelcode.core.messages import INVALID_ARGUMENTS_KEY, Message, ToolCall


def test_message_constructors():
    assert Message.system("s") == Message("system", "s")
    assert Message.user("u").role == "user"
    assert Message.assistant("a").content == "a"
    tc = ToolCall("c1", "t", {"x": 1})
    assert Message.assistant(tool_calls=[tc]).tool_calls == [tc]
    assert Message.tool_result("c1", "r").tool_call_id == "c1"


def test_toolcall_invalid_arguments_flag():
    assert not ToolCall("c", "t", {"a": 1}).has_invalid_arguments
    assert ToolCall("c", "t", {INVALID_ARGUMENTS_KEY: "{oops"}).has_invalid_arguments


def test_event_bus_sync_and_async_handlers_and_unsubscribe():
    bus = EventBus()
    seen_sync, seen_async = [], []

    def sync_handler(ev):
        seen_sync.append(ev)

    async def async_handler(ev):
        seen_async.append(ev)

    unsub = bus.subscribe(sync_handler)
    bus.subscribe(async_handler)

    ev1, ev2 = RunStarted("hi"), RunStarted("bye")
    asyncio.run(bus.emit(ev1))
    unsub()
    asyncio.run(bus.emit(ev2))

    assert seen_sync == [ev1]        # 退订后不再收到
    assert seen_async == [ev1, ev2]  # 异步 handler 正常 await
    assert bus.handler_count == 1
