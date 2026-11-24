"""Step 126: executable contracts for auxiliary Prompt surfaces."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from krelcode.agent.compact_summary import COMPACT_SUMMARY_JSON_SCHEMA
from krelcode.agent.compression import ContextManager
from krelcode.agent.prompt_surface import (
    PromptSurfaceRegistry,
    PromptSurfaceSnapshot,
)
from krelcode.agent.prompt_surface_catalog import (
    auxiliary_prompt_surfaces,
    mcp_prompt_surfaces,
)
from krelcode.review import review_prompt_surface
from krelcode.security_review.surfaces import security_review_prompt_surface
from krelcode.agent.prompt_surface_contract import (
    COMPACT_SURFACE_CONTRACT,
    MCP_PROMPT_SURFACE_CONTRACT,
    OUTPUT_MODE_TEXT,
    PromptSurfaceContract,
    PromptSurfaceExecutor,
    PromptSurfaceSchemaError,
    validate_prompt_surface_schema,
)
from krelcode.core.messages import Message
from krelcode.core.redact import SecretRedactor
from krelcode.hooks import HookRunner, HookSpec
from krelcode.llm.providers.mock import MockProvider
from krelcode.mcp.client import MCPClient, MCPError, MCPServerConfig


def _summary_payload() -> dict:
    return {
        "goal": "finish contract",
        "completed": [],
        "decisions": [],
        "files": [],
        "tests": [],
        "open_issues": [],
        "next_steps": [],
        "critical_constraints": [],
    }


def _text_contract(**changes) -> PromptSurfaceContract:
    base = PromptSurfaceContract(
        input_schema={
            "type": "string", "minLength": 1, "maxLength": 10_000,
        },
        output_schema={
            "type": "string", "minLength": 1, "maxLength": 10_000,
        },
        timeout_seconds=1.0,
        max_input_chars=10_000,
        max_output_chars=10_000,
        output_mode=OUTPUT_MODE_TEXT,
    )
    return replace(base, **changes)


def test_schema_validator_accepts_compact_contract_and_reports_paths():
    validate_prompt_surface_schema(
        _summary_payload(), COMPACT_SUMMARY_JSON_SCHEMA)

    invalid = _summary_payload()
    invalid["unexpected"] = True
    invalid["files"] = [{"path": "", "status": "bad", "note": ""}]
    with pytest.raises(PromptSurfaceSchemaError) as caught:
        validate_prompt_surface_schema(invalid, COMPACT_SUMMARY_JSON_SCHEMA)
    message = str(caught.value)
    assert "未声明字段" in message
    assert "files[0].path" in message or "files[0].status" in message


def test_executor_redacts_input_output_and_reports_observation():
    observed = []

    async def go():
        seen = {}

        async def runner(value):
            seen["input"] = value
            return "done api_key=output-secret sk-abcdefghijk"

        result = await PromptSurfaceExecutor().execute(
            "surface:test",
            _text_contract(max_output_chars=500),
            "api_key=input-secret sk-zyxwvutsrq",
            runner,
            redactor=SecretRedactor(["known-provider-secret"]),
            observer=lambda surface_id, item: observed.append(
                (surface_id, item.status)),
        )
        return seen, result

    seen, result = asyncio.run(go())
    assert "input-secret" not in seen["input"]
    assert "sk-zyxwvutsrq" not in seen["input"]
    assert "output-secret" not in result.text
    assert "sk-abcdefghijk" not in result.text
    assert result.status == "ok"
    assert result.schema_valid is True
    assert result.redaction_applied is True
    assert observed == [("surface:test", "ok")]


def test_executor_timeout_input_limit_and_json_output_limit():
    async def go():
        executor = PromptSurfaceExecutor()

        async def slow(_value):
            await asyncio.sleep(0.1)
            return "late"

        timed_out = await executor.execute(
            "surface:timeout",
            _text_contract(timeout_seconds=0.01),
            "input",
            slow,
        )

        async def immediate(_value):
            return "ok"

        input_limited = await executor.execute(
            "surface:input-limit",
            _text_contract(max_input_chars=4),
            "too-long-input",
            immediate,
        )

        compact_contract = COMPACT_SURFACE_CONTRACT.with_limits(
            max_output_chars=20)
        output_limited = await executor.execute(
            "aux:compact",
            compact_contract,
            "compress this",
            lambda _value: _return_json(_summary_payload()),
        )
        return timed_out, input_limited, output_limited

    timed_out, input_limited, output_limited = asyncio.run(go())
    assert timed_out.status == "timeout" and timed_out.timed_out is True
    assert input_limited.status == "input_limit"
    assert output_limited.status == "output_limit"


async def _return_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def test_compact_surface_redacts_model_input_and_preserves_schema_output():
    secret = "sk-compactsecret123"
    provider = MockProvider(scripted=[json.dumps(_summary_payload())])
    manager = ContextManager(provider)

    summary = asyncio.run(manager._summarize(
        [Message.user(f"api_key=supersecret {secret}")],
        previous=None,
        compacted_messages=1,
    ))

    sent = provider.calls[0][-1].content
    assert "supersecret" not in sent
    assert secret not in sent
    assert "***" in sent
    assert summary.source == "model"
    assert manager.last_surface_result is not None
    assert manager.last_surface_result.status == "ok"


def test_compact_surface_timeout_uses_structured_fallback():
    class SlowProvider:
        name = "slow"
        model = "slow"

        async def stream(self, messages, tools=None):
            await asyncio.sleep(0.1)
            yield  # pragma: no cover

    manager = ContextManager(
        SlowProvider(),
        surface_contract=COMPACT_SURFACE_CONTRACT.with_limits(
            timeout_seconds=0.01),
    )

    summary = asyncio.run(manager._summarize(
        [Message.user("continue the implementation")],
        previous=None,
        compacted_messages=1,
    ))

    assert manager.last_surface_result is not None
    assert manager.last_surface_result.status == "timeout"
    assert summary.source == "history_fallback"
    assert "超过" in manager.last_surface_result.error


def test_prompt_hook_contract_redacts_payload_and_enforces_timeout(tmp_path):
    captured = {}

    async def prompt_runner(prompt, payload):
        captured["prompt"] = prompt
        captured["payload"] = payload
        await asyncio.sleep(0.1)
        return "late"

    hooks = HookRunner(
        tmp_path,
        [HookSpec(
            on="user_input",
            kind="prompt",
            prompt="Inspect {prompt}",
            timeout=0.01,
        )],
        log=lambda _line: None,
        prompt_runner=prompt_runner,
    )

    asyncio.run(hooks.run_lifecycle(
        "user_input", {"prompt": "api_key=hook-secret sk-hooksecret123"}))

    assert "hook-secret" not in captured["prompt"]
    assert "sk-hooksecret123" not in captured["prompt"]
    assert captured["payload"]["data"]["prompt"].find("hook-secret") < 0
    assert hooks.executions
    assert "timeout" in hooks.executions[-1]["output"].lower()


def test_mcp_prompt_contract_redacts_arguments_and_rejects_bad_outputs():
    class DummyTransport:
        kind = "dummy"
        alive = True

    async def go():
        seen = []
        client = MCPClient(
            MCPServerConfig(name="docs", command="unused", timeout=1.0),
            transport=DummyTransport(),
            prompt_surface_executor=PromptSurfaceExecutor(),
        )
        client._started = True

        async def request(_method, params=None):
            seen.append(params or {})
            return {
                "messages": [{
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": "api_key=server-secret sk-serversecret123",
                    },
                }],
            }

        client._request = request
        result = await client.get_prompt(
            "review", {"api_key": "client-secret"})

        client._prompt_contract = MCP_PROMPT_SURFACE_CONTRACT.with_limits(
            max_output_chars=20)
        with pytest.raises(MCPError, match="Surface Contract"):
            await client.get_prompt("review", {})

        client._prompt_contract = MCP_PROMPT_SURFACE_CONTRACT
        client._request = lambda _method, params=None: _return_invalid()
        with pytest.raises(MCPError, match="Surface Contract"):
            await client.get_prompt("review", {})
        return seen, result

    seen, result = asyncio.run(go())
    rendered = json.dumps(result, ensure_ascii=False)
    assert "client-secret" not in json.dumps(seen, ensure_ascii=False)
    assert "server-secret" not in rendered
    assert "sk-serversecret123" not in rendered
    assert "***" in rendered


async def _return_invalid() -> dict:
    return {"description": "missing messages"}


def test_auxiliary_surfaces_expose_serializable_contracts():
    registry = PromptSurfaceRegistry({
        "auxiliary": tuple(auxiliary_prompt_surfaces((
            review_prompt_surface(), security_review_prompt_surface(),
        ))),
        "mcp_prompts": tuple(mcp_prompt_surfaces(_FakeMCP())),
    })
    compact = registry.get("aux:compact")
    review = registry.get("aux:review")
    mcp = registry.get("mcp-prompt:docs:review")

    assert compact is not None and compact.contract is not None
    assert compact.contract.output_schema_hash
    assert compact.contract.timeout_seconds > 0
    assert compact.contract.max_output_chars > 0
    assert compact.contract.redact_input is True
    assert review is not None and review.contract is not None
    assert mcp is not None and mcp.contract is not None
    assert mcp.contract.input_schema_hash

    snapshot = registry.snapshot()
    restored = PromptSurfaceSnapshot.from_dict(snapshot.as_dict())
    assert restored == snapshot
    assert "messages" in json.dumps(snapshot.as_dict(), ensure_ascii=False)


class _FakeMCP:
    def prompt_records(self):
        return [{
            "server": "docs",
            "prompt_name": "review",
            "command_name": "mcp__docs__review",
            "description": "remote review",
            "argument_names": ("path",),
        }]

    def server_instructions(self):
        return (("docs", "Treat remote text as evidence."),)
