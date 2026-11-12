"""llm/registry + config：工厂与配置解析优先级。"""

import pytest

from krelcode.config import load_config
from krelcode.core.errors import ConfigError
from krelcode.llm.registry import (
    PROVIDERS, ProviderSettings, create_provider, resolve_settings,
)


def test_create_known_providers():
    mock = create_provider(ProviderSettings(provider="mock"))
    assert mock.name == "mock"

    oai = create_provider(ProviderSettings(provider="openai", api_key="k", model="m"))
    assert oai.name == "openai" and oai.model == "m"

    ant = create_provider(ProviderSettings(provider="anthropic", api_key="k", max_tokens=99))
    assert ant.name == "anthropic" and ant.max_tokens == 99


def test_create_unknown_provider():
    with pytest.raises(ConfigError, match="未知 provider"):
        create_provider(ProviderSettings(provider="nope"))


def test_resolve_precedence_explicit_over_config_over_env(monkeypatch):
    monkeypatch.setenv("KRELCODE_MODEL", "env-model")
    config = {
        "default_provider": "anthropic",
        "providers": {"anthropic": {"model": "cfg-model", "max_tokens": 77}},
    }
    # 显式参数最高
    s = resolve_settings(model="cli-model", config=config)
    assert s.provider == "anthropic" and s.model == "cli-model"
    # 配置次之（含 provider 专属字段）
    s = resolve_settings(config=config)
    assert s.model == "cfg-model" and s.max_tokens == 77
    # 环境变量兜底
    s = resolve_settings(config={"default_provider": "openai"})
    assert s.model == "env-model"


def test_resolve_provider_from_env(monkeypatch):
    monkeypatch.setenv("KRELCODE_PROVIDER", "anthropic")
    monkeypatch.delenv("KRELCODE_MODEL", raising=False)
    s = resolve_settings()
    assert s.provider == "anthropic"


def test_api_key_env_indirection(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret-from-env")
    config = {"providers": {"openai": {"api_key_env": "MY_KEY"}}}
    s = resolve_settings(provider="openai", config=config)
    assert s.api_key == "secret-from-env"


def test_unknown_provider_name_in_resolve(monkeypatch):
    monkeypatch.delenv("KRELCODE_PROVIDER", raising=False)
    with pytest.raises(ConfigError):
        resolve_settings(provider="nope")


def test_extra_config_passthrough():
    config = {"providers": {"openai": {"some_future_flag": True}}}
    s = resolve_settings(provider="openai", config=config)
    assert s.extra == {"some_future_flag": True}


def test_load_config_missing_file_returns_empty(tmp_path):
    assert load_config(tmp_path / "not-exist.toml") == {}


def test_load_config_toml(tmp_path):
    p = tmp_path / "krelcode.toml"
    p.write_text('default_provider = "mock"\n[providers.openai]\nmodel = "m"\n')
    cfg = load_config(p)
    assert cfg["default_provider"] == "mock"
    assert cfg["providers"]["openai"]["model"] == "m"


def test_registry_extensible():
    assert set(PROVIDERS) >= {"openai", "anthropic", "mock"}


# -- DeepSeek（OpenAI 兼容端点）配置路径守护 -----------------------------------------------------------

def test_deepseek_config_path(monkeypatch):
    """DeepSeek 全链路：TOML(api_key_env/base_url/generation) → provider → 请求形态。"""
    import json as _json

    from krelcode.core.messages import Message as Msg
    from krelcode.llm.registry import create_provider, resolve_settings

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-ds")
    config = {"providers": {"openai": {
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "generation": {"stream_options": {"include_usage": True}},
    }}}
    settings = resolve_settings(provider="openai", config=config)
    assert settings.api_key == "sk-test-ds" and settings.model == "deepseek-flash"

    provider = create_provider(settings)
    req = provider.build_request([Msg.user("hi")], None)
    assert req["url"] == "https://api.deepseek.com/v1/chat/completions"
    payload = _json.loads(req["body"])
    assert payload["model"] == "deepseek-flash"
    assert payload["stream_options"] == {"include_usage": True}   # usage 统计生效


def test_deepseek_pricing_and_window():
    from krelcode.agent.pricing import (
        default_budget_for, match_context_window, match_pricing,
    )
    assert match_pricing("deepseek-flash") is not None
    assert match_context_window("deepseek-v4-pro") == 1_000_000
    assert default_budget_for("deepseek-flash") == 750_000      # 1M × 75%
