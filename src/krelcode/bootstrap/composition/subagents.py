"""Subagent model resolver composition."""

from __future__ import annotations

import argparse
from typing import Any, Callable, Dict, Optional

from krelcode.core.redact import SecretRedactor
from krelcode.enterprise.policy import EnterprisePolicy
from krelcode.llm.base import LLMProvider
from krelcode.llm.registry import ProviderSettings, create_provider, resolve_settings
from krelcode.llm.retry import RetryingProvider, RetryPolicy
from krelcode.subagent.model_aliases import resolve_agent_model_spec


def build_subagent_llm_resolver(
    args: argparse.Namespace,
    config: Dict[str, Any],
    parent: LLMProvider | Callable[[], LLMProvider],
    redactor: Optional[SecretRedactor] = None,
    enterprise_policy: Optional[EnterprisePolicy] = None,
    *,
    settings_factory: Callable[..., ProviderSettings] | None = None,
    provider_factory: Callable[[ProviderSettings], LLMProvider] | None = None,
) -> Callable[[str], LLMProvider]:
    """Resolve Definition.model without changing the parent's provider.

    Logical tiers (``sonnet``/``haiku``/``opus``) map to Anthropic only when the
    parent already uses Anthropic. Other providers inherit the parent unless the
    user supplies an explicit ``[agent_models]`` override.
    """
    resolve_settings_factory = settings_factory or resolve_settings
    create_provider_factory = provider_factory or create_provider

    def resolve(spec: str) -> LLMProvider:
        parent_provider = parent() if callable(parent) else parent
        raw = str(spec or "").strip()
        if not raw:
            return parent_provider
        parent_name = str(getattr(parent_provider, "name", "openai") or "openai")
        resolved = resolve_agent_model_spec(
            raw, parent_provider=parent_name, config=config
        )
        if resolved is None:
            return parent_provider
        provider_name, model_name = resolved
        if enterprise_policy is not None:
            enterprise_policy.validate_provider(provider_name)
        if provider_name == "mock":
            raise ValueError("mock provider 不支持 Agent Definition 独立模型")
        settings = resolve_settings_factory(
            provider=provider_name,
            model=model_name,
            base_url=(args.base_url if provider_name == parent_name else None),
            config=config,
        )
        inner = create_provider_factory(settings)
        if getattr(inner, "api_key", None) is None:
            raise ValueError(
                f"Agent Definition 模型 {provider_name}/{model_name} 缺少 API key"
            )
        provider: LLMProvider = inner
        if args.retries > 0:
            provider = RetryingProvider(
                inner,
                RetryPolicy(max_retries=args.retries),
                on_retry=lambda attempt, delay, error: print(
                    f"[retry:subagent] LLM 调用失败"
                    f"（{type(error).__name__}），第 {attempt} 次重试，"
                    f"{delay:g}s 后…"
                ),
            )
        if redactor is not None:
            redactor.add(getattr(inner, "api_key", None))
        return provider

    return resolve
