"""Step 51 测试：统一 Provider 机制收尾。

覆盖四处长期缺口：
1. RetryingProvider 透明代理 api_key 等属性（否则脱敏层看不到 key）
2. AgentLoop 尊重 capabilities.supports_tools（不向无工具能力 provider advertise）
3. create_provider 不再按名字硬编码，provider 自描述 EXTRA_CONFIG_KEYS/STANDARD_CONFIG
4. /model 热切换后新 provider 的 key 也纳入 SecretRedactor
"""

import asyncio
from pathlib import Path

from krelcode.agent.loop import AgentLoop
from krelcode.agent.usage import UsageTracker
from krelcode.app import build_model_switcher, build_parser
from krelcode.core.messages import Message
from krelcode.core.redact import SecretRedactor
from krelcode.llm.base import ProviderCapabilities, StreamFinished
from krelcode.llm.providers.mock import MockProvider
from krelcode.llm.providers.openai import OpenAIProvider
from krelcode.llm.registry import (
    PROVIDERS, ProviderSettings, create_provider, list_providers,
)
from krelcode.llm.retry import RetryingProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry

CTX = ToolContext(workspace_root=Path("."))


class SimpleTool:
    name = "simple"
    description = "noop"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, args, ctx):
        return ToolResult(content="ok")


class NoToolsProvider:
    """声明不支持 tools 的最小 provider，记录 agent 传了什么 tools。"""
    name = "no-tools"
    model = "nt"
    capabilities = ProviderCapabilities(supports_tools=False)

    def __init__(self):
        self.tools_seen = []

    async def stream(self, messages, tools=None):
        self.tools_seen.append(tools)
        yield StreamFinished(
            message=Message.assistant("hi"), finish_reason="stop", usage={})


class CustomProvider:
    """带 EXTRA_CONFIG_KEYS 的扩展 provider（不修改 registry 的构造分支）。"""
    name = "custom"
    model = "m"
    capabilities = ProviderCapabilities()
    EXTRA_CONFIG_KEYS = ("foo",)

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def stream(self, messages, tools=None):
        yield StreamFinished(message=Message.assistant("x"), finish_reason="stop", usage={})


class NoStandardProvider:
    STANDARD_CONFIG = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs


# -- 1. RetryingProvider 透明委托 -------------------------------------------------------

def test_retrying_provider_proxies_api_key():
    inner = OpenAIProvider(api_key="sk-inner-key-123")
    wrapped = RetryingProvider(inner)
    assert wrapped.api_key == "sk-inner-key-123"
    assert wrapped.base_url == inner.base_url
    assert wrapped.timeout == inner.timeout


def test_redactor_sees_key_through_retrying_provider(monkeypatch):
    """关键回归：默认 --retries 包装后，redactor 仍能拿到真实 key。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-retry-secret-123")
    wrapped = RetryingProvider(OpenAIProvider())
    redactor = SecretRedactor()
    redactor.add(getattr(wrapped, "api_key", None))
    assert "sk-retry-secret-123" in redactor.secrets


# -- 2. capabilities 协商 ----------------------------------------------------------------

def test_agent_loop_skips_tools_when_capability_disabled():
    provider = NoToolsProvider()
    registry = ToolRegistry([SimpleTool()])
    loop = AgentLoop(
        llm=provider, registry=registry, tool_context=CTX,
        policy=AllowAllPolicy(), system_prompt="SYS",
    )
    asyncio.run(loop.run("hi"))
    assert provider.tools_seen == [None]


def test_agent_loop_passes_tools_when_capability_enabled():
    provider = MockProvider(scripted=[Message.assistant("hi")])
    registry = ToolRegistry([SimpleTool()])
    loop = AgentLoop(
        llm=provider, registry=registry, tool_context=CTX,
        policy=AllowAllPolicy(), system_prompt="SYS",
    )
    asyncio.run(loop.run("hi"))
    assert provider.tools_seen[0] is not None


# -- 3. create_provider generic 组装 -----------------------------------------------------

def test_create_provider_uses_declared_extra_keys(monkeypatch):
    monkeypatch.setitem(PROVIDERS, "custom", CustomProvider)
    p = create_provider(ProviderSettings(
        provider="custom", model="m", api_key="k", extra={"foo": 42}))
    assert p.kwargs["foo"] == 42
    assert p.kwargs["model"] == "m" and p.kwargs["api_key"] == "k"


def test_standard_config_false_skips_wire_keys(monkeypatch):
    monkeypatch.setitem(PROVIDERS, "nostd", NoStandardProvider)
    p = create_provider(ProviderSettings(provider="nostd", model="m", api_key="k"))
    assert p.kwargs == {}


def test_anthropic_extras_still_passed():
    p = create_provider(ProviderSettings(
        provider="anthropic", api_key="k", max_tokens=99,
        extra={"prompt_cache": False, "thinking_budget": 2048}))
    assert p.max_tokens == 99
    assert p.prompt_cache is False
    assert p.thinking_budget == 2048


def test_list_providers_reports_env_names():
    providers = list_providers()
    assert {"openai", "anthropic", "gemini", "mock"} <= set(providers)
    assert providers["openai"] == "OPENAI_API_KEY"
    assert providers["mock"] == ""


# -- 4. /model 热切换同步脱敏集合 -------------------------------------------------------

def test_model_switch_registers_new_key_with_redactor(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-switch-secret-123")
    args = build_parser().parse_args(
        ["--mock", "--workspace", str(tmp_path), "--retries", "0"])
    loop = AgentLoop(
        llm=MockProvider(), registry=ToolRegistry(), tool_context=ToolContext(
            workspace_root=tmp_path),
        policy=AllowAllPolicy(), system_prompt="SYS",
    )
    redactor = SecretRedactor()
    switch = build_model_switcher(args, {}, loop, UsageTracker(model="mock-model"),
                                  redactor=redactor)
    msg = switch("openai/gpt-4o-mini")
    assert "已切换" in msg
    assert "sk-switch-secret-123" in redactor.secrets
