"""Logical model aliases used by Claude Code-compatible built-in agents.

Built-in definitions name model tiers (``sonnet`` and ``haiku``) rather than a
provider-specific snapshot. Resolve those tiers without forcing a DeepSeek or
OpenAI-compatible installation to configure an unrelated Anthropic API key.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple


DEFAULT_AGENT_MODEL_ALIASES: Mapping[str, str] = {
    "sonnet": "anthropic/claude-sonnet-4-5",
    "haiku": "anthropic/claude-haiku-4-5",
    "opus": "anthropic/claude-opus-4-1",
}


def resolve_agent_model_spec(
    spec: str,
    *,
    parent_provider: str,
    config: Optional[Mapping[str, Any]] = None,
) -> Optional[Tuple[str, str]]:
    """Return ``(provider, model)`` or ``None`` when the parent should inherit.

    Resolution order is explicit ``[agent_models]`` override, built-in tier
    alias (only when the parent is already Anthropic), then the literal model
    spec against the parent provider. Non-Anthropic installations inherit the
    parent provider/model for tier aliases unless an explicit override exists.
    """
    raw = str(spec or "").strip()
    parent = str(parent_provider or "").strip() or "openai"
    if not raw or raw.casefold() in {"inherit", "parent", "default"}:
        return None

    configured_aliases = (config or {}).get("agent_models") or {}
    configured = (
        configured_aliases.get(raw)
        if isinstance(configured_aliases, Mapping)
        else None
    )
    if configured is not None:
        mapped = str(configured).strip()
        if not mapped or mapped.casefold() in {"inherit", "parent", "default"}:
            return None
    elif raw in DEFAULT_AGENT_MODEL_ALIASES:
        if parent != "anthropic":
            return None
        mapped = DEFAULT_AGENT_MODEL_ALIASES[raw]
    else:
        mapped = raw

    if "/" in mapped:
        provider, model = mapped.split("/", 1)
    else:
        provider, model = parent, mapped
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        raise ValueError(f"Agent Definition 模型别名无效: {raw!r}")
    return provider, model


__all__ = [
    "DEFAULT_AGENT_MODEL_ALIASES",
    "resolve_agent_model_spec",
]
