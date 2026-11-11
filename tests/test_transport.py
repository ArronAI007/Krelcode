"""llm/transport 测试：asyncio 桥 + SSE 解析器（不碰网络）。"""

import asyncio

import pytest

from fakes import FakeResponse, sse
from krelcode.core.errors import LLMError
from krelcode.llm.transport import aiter_lines, aread, parse_json_event, sse_data_events


def collect_sse(body: bytes):
    async def go():
        resp = FakeResponse(body)
        return [d async for d in sse_data_events(resp)]
    return asyncio.run(go())


def test_sse_basic_events():
    body = sse('{"a": 1}', '{"b": 2}')
    assert collect_sse(body) == ['{"a": 1}', '{"b": 2}']


def test_sse_multiline_data_joined():
    body = b"data: line1\ndata: line2\n\n"
    assert collect_sse(body) == ["line1\nline2"]


def test_sse_ignores_comments_and_event_fields():
    body = b": heartbeat\n\nevent: message\ndata: x\nid: 3\n\n"
    assert collect_sse(body) == ["x"]


def test_sse_done_terminates_stream():
    body = b'data: first\n\ndata: [DONE]\n\ndata: after\n\n'
    assert collect_sse(body) == ["first"]


def test_sse_flush_without_trailing_blank_line():
    body = b"data: tail"
    assert collect_sse(body) == ["tail"]


def test_aiter_lines_and_aread():
    resp = FakeResponse(b"l1\nl2\n")
    lines = asyncio.run(_collect(aiter_lines(resp)))
    assert [l.decode() for l in lines] == ["l1\n", "l2\n"]
    assert asyncio.run(aread(FakeResponse(b"all"))) == b"all"


async def _collect(aiter):
    return [x async for x in aiter]


def test_parse_json_event_error():
    with pytest.raises(LLMError, match="非法 JSON"):
        asyncio.run(parse_json_event("{oops", "test"))
