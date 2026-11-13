"""Provider 注册表与工厂：统一机制的入口。

- PROVIDERS: 名字 → provider 类
- resolve_settings(): 按「显式参数 > TOML 配置 > 环境变量 > 默认值」解析
- create_provider(): 按名字构造 provider 实例

新增 provider 只需实现 LLMProvider 端口并 register()，其他任何层不改：
- 默认接收标准配置 model/api_key/base_url/timeout/generation
- `STANDARD_CONFIG = False` 可声明「不接收标准配置」（如 mock）
- `EXTRA_CONFIG_KEYS = (...)` 声明构造函数额外接受的 TOML 键
  （如 anthropic 的 max_tokens/prompt_cache/thinking_budget）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from ..core.errors import ConfigError
from .base import LLMProvider
from .providers.anthropic import AnthropicProvider
from .providers.gemini import GeminiProvider
from .providers.mock import MockProvider
from .providers.openai import OpenAIProvider

PROVIDERS: Dict[str, Callable[..., LLMProvider]] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "mock": MockProvider,
}

# 各 provider 的 api key 环境变量约定（Gemini 另有 GOOGLE_API_KEY 兜底，在 provider 内处理）
API_KEY_ENV: Dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mock": "",
}


def register(name: str, factory: Callable[..., LLMProvider]) -> None:
    """扩展点：注册新 provider。"""
    PROVIDERS[name] = factory


def list_providers() -> Dict[str, str]:
    """返回已注册 provider → 默认 api_key 环境变量名（无约定则为空串）。

    供 doctor/补全/诊断统一展示当前支持的全部 provider。
    """
    return {name: API_KEY_ENV.get(name, "") for name in sorted(PROVIDERS)}


@dataclass
class ProviderSettings:
    provider: str = "openai"
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    timeout: float = 120.0
    max_tokens: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def resolve_settings(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> ProviderSettings:
    """解析优先级：显式参数 > TOML [providers.NAME] > 环境变量 > 默认。"""
    config = config or {}
    name = (
        provider
        or os.environ.get("KRELCODE_PROVIDER")
        or config.get("default_provider")
        or "openai"
    )
    if name not in PROVIDERS:
        raise ConfigError(
            f"未知 provider: {name}（可用: {', '.join(list_providers())}）"
        )
    section: Dict[str, Any] = dict((config.get("providers") or {}).get(name) or {})

    resolved_model = model or section.get("model") or os.environ.get("KRELCODE_MODEL")
    resolved_base = base_url or section.get("base_url")

    resolved_key = api_key or section.get("api_key")
    if resolved_key is None:
        key_env = section.get("api_key_env") or API_KEY_ENV.get(name, "")
        if key_env:
            resolved_key = os.environ.get(key_env)

    known = {"provider", "model", "base_url", "api_key", "api_key_env", "timeout", "max_tokens"}
    extra = {k: v for k, v in section.items() if k not in known}

    return ProviderSettings(
        provider=name,
        model=resolved_model,
        base_url=resolved_base,
        api_key=resolved_key,
        timeout=float(section.get("timeout", 120.0)),
        max_tokens=section.get("max_tokens"),
        extra=extra,
    )


def create_provider(settings: ProviderSettings, **overrides: Any) -> LLMProvider:
    """按 settings 构造 provider；overrides 供测试注入 transport 等。

    不按 provider 名字分支：标准字段 + provider 用 `EXTRA_CONFIG_KEYS`
    声明的专属键，统一 generic 组装，新增 provider 无需改本函数。
    """
    factory = PROVIDERS.get(settings.provider)
    if factory is None:
        raise ConfigError(
            f"未知 provider: {settings.provider}（可用: {', '.join(list_providers())}）"
        )
    kwargs: Dict[str, Any] = {}
    if getattr(factory, "STANDARD_CONFIG", True):
        kwargs.update(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout,
        )
        generation = settings.extra.get("generation")
        if isinstance(generation, dict) and generation:
            kwargs["generation"] = generation
        for key in getattr(factory, "EXTRA_CONFIG_KEYS", ()):
            if key == "max_tokens":
                value = settings.max_tokens
            else:
                value = settings.extra.get(key)
            if value is not None:
                kwargs[key] = value
    kwargs.update(overrides)
    return factory(**kwargs)  # type: ignore[operator]
