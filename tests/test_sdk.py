"""Python SDK：选项、协议消费、子进程生命周期与端到端 mock。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from krelcode.sdk import (
    ImageInput,
    KrelcodeClient,
    KrelcodeMessage,
    KrelcodeOptions,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    SDKError,
    SDKProcessError,
    _scrub_secrets,
)


ROOT = Path(__file__).parent.parent


def sdk_options(tmp_path: Path, **overrides):
    values = {
        "workspace": tmp_path,
        "env": {"PYTHONPATH": str(ROOT / "src")},
        "mock": True,
        "no_session": True,
        "no_context": True,
        "no_mcp": True,
        "no_skills": True,
        "no_checkpoint": True,
    }
    values.update(overrides)
    return KrelcodeOptions(**values)


def test_options_build_complete_stream_json_command(tmp_path):
    options = KrelcodeOptions(
        workspace=tmp_path,
        provider="openai",
        model="deepseek-flash",
        base_url="https://example.invalid/v1",
        permission_mode="read-only",
        auto_approve=True,
        no_sandbox=True,
        sandbox_backend="static",
        sandbox_network=True,
        no_context=True,
        no_skills=True,
        skills_dirs=("a", "b"),
        no_mcp=True,
        no_session=True,
        no_checkpoint=True,
        auto_commit=True,
        no_compress=True,
        max_steps=7,
        repeat_failure_limit=4,
        token_budget=1234,
        retries=0,
        context_tokens=9000,
        resume="latest",
        extra_args=("--verbose-placeholder",),
    )
    command = options.command()
    assert command[0] == sys.executable
    assert command[1:3] == ["-m", "krelcode"]
    assert "--provider" in command and "openai" in command
    assert "--model" in command and "deepseek-flash" in command
    for flag in (
        "--permission-mode", "--auto-approve", "--no-sandbox",
        "--sandbox-backend", "--sandbox-network", "--no-context",
        "--no-skills", "--skills-dir", "--no-mcp", "--no-session",
        "--no-checkpoint", "--auto-commit", "--no-compress",
        "--max-steps", "--repeat-failure-limit", "--token-budget",
        "--retries", "--context-tokens", "--resume",
    ):
        assert flag in command
    assert command[-4:] == [
        "--input-format", "stream-json", "--output-format", "stream-json",
    ]


def test_options_reject_protocol_overrides_and_invalid_timeouts(tmp_path):
    with pytest.raises(ValueError, match="保留参数"):
        KrelcodeOptions(workspace=tmp_path, extra_args=("--output-format", "text"))
    with pytest.raises(ValueError, match="必须大于 0"):
        KrelcodeOptions(workspace=tmp_path, startup_timeout=0)
    with pytest.raises(ValueError, match="64 KiB"):
        KrelcodeOptions(workspace=tmp_path, max_line_bytes=1024)


def test_options_merge_environment_without_putting_key_in_command(tmp_path):
    secret = "custom-secret-value-without-prefix"
    options = KrelcodeOptions(workspace=tmp_path, env={"DEEPSEEK_API_KEY": secret})
    command = " ".join(options.command())
    assert secret not in command
    assert secret not in repr(options)
    assert options.environment()["DEEPSEEK_API_KEY"] == secret


def test_image_input_and_message_accessors(tmp_path):
    image = ImageInput("image/png", "YWJj", "/tmp/a.png")
    assert image.to_block() == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "YWJj",
            "path": "/tmp/a.png",
        },
    }
    message = KrelcodeMessage({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "ok",
        "request_id": "r1",
        "session_id": "s1",
        "turn_id": "t1",
    })
    assert message.is_terminal and not message.is_error
    assert message.request_id == "r1"
    assert message.session_id == "s1"
    assert message.turn_id == "t1"


def test_secret_scrubber_removes_common_key_shapes():
    text = (
        "key=sk-abcdefghijk\n"
        "Authorization: Bearer abcdefghijklmnop\n"
        "api_key=verysecret123\n"
    )
    scrubbed = _scrub_secrets(text)
    assert "sk-abcdefghijk" not in scrubbed
    assert "abcdefghijklmnop" not in scrubbed
    assert "verysecret123" not in scrubbed
    assert "***" in scrubbed


def test_sdk_e2e_streams_query_and_reuses_session(tmp_path):
    async def go():
        async with KrelcodeClient(options=sdk_options(tmp_path)) as client:
            assert client.session_id is None
            assert client.init_message is not None
            assert client.init_message.get("protocol") == PROTOCOL_NAME
            assert client.init_message.get("protocol_version") == PROTOCOL_VERSION

            messages = [
                message async for message in client.query(
                    "hello", request_id="test-request")
            ]
            assert messages[0].type == "system"
            assert messages[0].subtype == "turn_started"
            assert messages[0].request_id == "test-request"
            assert any(
                message.type == "assistant"
                and message.subtype == "text_delta"
                for message in messages
            )
            result = messages[-1]
            assert result.is_terminal and not result.is_error
            assert result.get("result")
            assert result.get("usage")["calls"] >= 1

            second = await client.run("second")
            assert "[mock]" in second
            assert client.process_id is not None
        assert client.process_id is None

    asyncio.run(go())


def test_sdk_supports_concurrent_query_routing(tmp_path):
    async def collect(client, prompt):
        return [item async for item in client.query(prompt)]

    async def go():
        async with KrelcodeClient(options=sdk_options(tmp_path)) as client:
            first, second = await asyncio.gather(
                collect(client, "first"),
                collect(client, "second"),
            )
            first_result = first[-1]
            second_result = second[-1]
            assert first_result.is_terminal and second_result.is_terminal
            assert first_result.request_id != second_result.request_id
            assert first_result.turn_id != second_result.turn_id

    asyncio.run(go())


def test_sdk_reports_missing_executable_as_process_error(tmp_path):
    options = KrelcodeOptions(
        workspace=tmp_path,
        executable=str(tmp_path / "missing-python"),
    )
    async def go():
        with pytest.raises(SDKProcessError, match="无法启动"):
            async with KrelcodeClient(options=options):
                pass

    asyncio.run(go())


def test_sdk_rejects_empty_query_and_invalid_image(tmp_path):
    async def go():
        async with KrelcodeClient(options=sdk_options(tmp_path)) as client:
            with pytest.raises(ValueError, match="不能同时为空"):
                async for _ in client.query():
                    pass
            with pytest.raises(TypeError, match="ImageInput"):
                async for _ in client.query("x", images=[object()]):
                    pass

    asyncio.run(go())


def test_sdk_stderr_tail_is_bounded_and_redacted(tmp_path):
    async def go():
        options = sdk_options(tmp_path, env={
            "PYTHONPATH": str(ROOT / "src"),
            "DEEPSEEK_API_KEY": "custom-secret-value-without-prefix",
        })
        client = KrelcodeClient(options=options)
        await client.start()
        client._stderr_lines.clear()
        client._stderr_lines.append("custom-secret-value-without-prefix\n")
        client._stderr_lines.append("token key=sk-abcdefghijk\n")
        try:
            tail = client.stderr_tail(limit=1000)
            assert "custom-secret-value-without-prefix" not in tail
            assert "sk-abcdefghijk" not in tail
        finally:
            await client.close()
            await client.close()

    asyncio.run(go())


def test_sdk_interrupt_emits_control_message_when_process_is_running(tmp_path):
    from types import SimpleNamespace

    async def go():
        client = KrelcodeClient(options=sdk_options(tmp_path))
        sent = []

        async def fake_write(payload):
            sent.append(payload)

        client._process = SimpleNamespace(returncode=None, stdin=object())
        client._write_json = fake_write
        assert await client.interrupt() is True
        assert sent == [{"type": "control", "subtype": "interrupt"}]
        client._process.returncode = 0
        assert await client.interrupt() is False

    asyncio.run(go())
