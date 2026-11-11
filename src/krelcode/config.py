"""TOML 配置加载（标准库 tomllib）。

查找顺序（第一个存在的生效）：
1. 显式传入的 path
2. ./krelcode.toml
3. ~/.krelcode/config.toml

示例：
    default_provider = "anthropic"

    [providers.openai]
    model = "gpt-4o-mini"
    base_url = "https://api.openai.com/v1"
    api_key_env = "OPENAI_API_KEY"

    [providers.anthropic]
    model = "claude-sonnet-4-5"
    max_tokens = 8192
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_PATHS = (Path("krelcode.toml"), Path.home() / ".krelcode" / "config.toml")


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    candidates = [path] if path else list(DEFAULT_PATHS)
    for candidate in candidates:
        if candidate and candidate.is_file():
            with open(candidate, "rb") as f:
                return tomllib.load(f)
    return {}
