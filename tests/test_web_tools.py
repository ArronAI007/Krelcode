from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from krelcode.tools.base import ToolContext
from krelcode.web.http import (
    RawHttpResponse,
    SafeHttpClient,
    UnsafeUrlError,
    WebResponseError,
)
from krelcode.web.tools import (
    BraveSearchBackend,
    WebFetchTool,
    WebOptions,
    WebRuntime,
    WebSearchService,
    WebSearchTool,
)


GLOBAL = "93.184.216.34"


def run(coro):
    return asyncio.run(coro)


def client(transport, **kwargs):
    return SafeHttpClient(
        resolver=lambda host, port: [GLOBAL],
        transport=transport,
        **kwargs,
    )


def response(url: str, body: bytes = b"<html><head><title>T</title></head>"
             b"<body><h1>Hello</h1><script>bad()</script><p>World</p></body></html>",
             status: int = 200, headers=None):
    return RawHttpResponse(
        status=status,
        reason="OK" if status < 400 else "Error",
        headers=headers or {"Content-Type": "text/html; charset=utf-8"},
        body=body,
        url=url,
    )


def test_fetch_rejects_private_localhost_and_unsafe_schemes():
    async def go():
        http = SafeHttpClient(resolver=lambda host, port: ["127.0.0.1"])
        for url in (
            "http://example.com", "http://localhost", "file:///etc/passwd",
            "data:text/plain,hello", "http://127.0.0.1/",
            "https://example.com:8443/",
        ):
            with pytest.raises(UnsafeUrlError):
                await http.fetch(url)
    run(go())


def test_fetch_extracts_html_text_and_strips_scripts():
    async def go():
        http = client(lambda *args: response(args[0].url))
        fetched = await http.fetch("https://example.com/page")
        assert "Hello" in fetched.text
        assert "World" in fetched.text
        assert "bad()" in fetched.text
        tool = WebFetchTool(http)
        result = await tool.execute(
            {"url": "https://example.com/page"}, ToolContext(
                Path.cwd(), sandbox_enabled=False))
        assert not result.is_error
        assert "<web_fetch trust=\"untrusted\"" in result.content
        assert "Hello" in result.content
        assert "bad()" not in result.content
    run(go())


def test_redirect_is_revalidated_and_response_size_is_bounded():
    async def go():
        def transport(validated, headers, max_bytes, timeout):
            if validated.hostname == "first.example":
                return RawHttpResponse(
                    302, "Found", {"Location": "https://second.example/final"},
                    b"", validated.url)
            return response(validated.url, b"x" * 20)
        http = client(transport, max_bytes=10)
        with pytest.raises(WebResponseError, match="大小"):
            await http.fetch("https://first.example/start")
    run(go())


def test_cross_origin_redirect_strips_sensitive_request_headers():
    async def go():
        captured = {}

        def transport(validated, headers, max_bytes, timeout):
            captured[validated.hostname] = dict(headers)
            if validated.hostname == "first.example":
                return RawHttpResponse(
                    302, "Found", {"Location": "https://second.example/final"},
                    b"", validated.url)
            return response(
                validated.url, b"ok",
                headers={"Content-Type": "text/plain; charset=utf-8"})

        http = client(transport)
        await http.fetch(
            "https://first.example/start",
            headers={
                "X-Subscription-Token": "very-secret",
                "X-API-Key": "also-secret",
                "Accept": "application/json",
            },
        )

        assert captured["first.example"]["X-Subscription-Token"] == "very-secret"
        assert captured["first.example"]["X-API-Key"] == "also-secret"
        assert captured["second.example"]["Accept"] == "application/json"
        lowered = {key.lower() for key in captured["second.example"]}
        assert "x-subscription-token" not in lowered
        assert "x-api-key" not in lowered
    run(go())


def test_search_backend_sends_key_only_in_header_and_formats_results():
    captured = {}

    def transport(validated, headers, max_bytes, timeout):
        captured.update(headers)
        payload = {"web": {"results": [{
            "title": "DeepSeek", "url": "https://example.com/deepseek",
            "description": "A <b>model</b>",
        }]}}
        return RawHttpResponse(
            200, "OK", {"Content-Type": "application/json"},
            json.dumps(payload).encode(), validated.url)

    async def go():
        http = client(transport)
        backend = BraveSearchBackend(
            http, api_key_env="TEST_SEARCH_KEY",
            environ={"TEST_SEARCH_KEY": "very-secret"},
        )
        results = await backend.search("deepseek", 3)
        assert results[0]["title"] == "DeepSeek"
        assert results[0]["description"] == "A model"
        assert captured["X-Subscription-Token"] == "very-secret"
        assert "Authorization" not in captured

        service = WebSearchService(backend=backend, provider="brave")
        tool = WebSearchTool(service)
        result = await tool.execute(
            {"query": "deepseek", "max_results": 1},
            ToolContext(Path.cwd(), sandbox_enabled=False),
        )
        assert not result.is_error
        assert "https://example.com/deepseek" in result.content
        assert "very-secret" not in result.content
    run(go())


def test_runtime_fails_search_closed_without_configured_backend():
    runtime = WebRuntime.from_options(WebOptions(
        search_provider="unknown", search_enabled=True,
        search_api_key_env="MISSING_TEST_KEY",
    ))
    tool = WebSearchTool(runtime.search)
    result = run(tool.execute(
        {"query": "x"}, ToolContext(Path.cwd(), sandbox_enabled=False)))
    assert result.is_error
    assert "未配置" in result.content
