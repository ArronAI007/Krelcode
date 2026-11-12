"""敏感文件读保护与脱敏（Step 45）。

目标：堵住「文件侧」密钥泄漏——coding agent 的 read_file/grep 不应直接读出
`.env`/`krelcode.toml` 里的 API key、`*.key`/`*.pem`/`credentials*` 等凭据。

分类（classify_secret_path）：
- "block"      ：纯密钥/凭据文件 → read_file 拒读、grep 跳过
- "redact_env" ：.env 类 KEY=VALUE 文件 → 保留键名、隐藏值后返回
- "redact_toml"：krelcode.toml → 只隐藏 api_key 的值，其余配置照常可见

与 safety/policy.py 的写保护（PROTECTED_GLOBS）互补：那里是「写前 ASK」，
这里是「读时拦截/脱敏」。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Optional

# 直接拒读/跳过的纯密钥或凭据文件（内容整体即秘密）。
BLOCK_GLOBS = (
    "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*",
    "credentials*", "secrets.*",
    ".npmrc", ".netrc", ".pgpass", ".htpasswd",
)

# 脱敏后返回的配置文件（保留结构、隐藏值）。
REDACT_ENV_GLOBS = (".env", ".env.*")
REDACT_TOML_GLOBS = ("krelcode.toml",)

# 目录级敏感：路径中任一段命中即整体 block。
BLOCK_DIRS = (".git", ".krelcode")


def classify_secret_path(path, workspace_root) -> Optional[str]:
    """判定工作区内路径是否敏感；返回 "block"/"redact_env"/"redact_toml" 或 None。"""
    root = Path(workspace_root)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        rel = candidate.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        rel = candidate.as_posix()

    parts = rel.split("/")
    for part in parts:
        if part in BLOCK_DIRS:
            return "block"

    base = parts[-1]
    for pattern in REDACT_ENV_GLOBS:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(base, pattern):
            return "redact_env"
    for pattern in REDACT_TOML_GLOBS:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(base, pattern):
            return "redact_toml"
    for pattern in BLOCK_GLOBS:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(base, pattern):
            return "block"
    return None


# .env：KEY=VALUE / export KEY=VALUE / KEY = VALUE（保留注释与空行）
_ENV_LINE = re.compile(
    r"^(\s*)(export\s+)?([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(\S.*)$"
)

# krelcode.toml：只隐藏 api_key 的值（api_key_env 只是变量名，不是秘密）
_TOML_API_KEY = re.compile(r"^(\s*api_key\s*=\s*)\S.*$", re.MULTILINE)


def redact_secret_text(text: str, category: str) -> str:
    """按分类对文本脱敏；未知分类原样返回（block 场景调用方已拒读）。"""
    if category == "redact_env":
        out = []
        for line in text.splitlines():
            m = _ENV_LINE.match(line)
            out.append(f"{m.group(1)}{m.group(2) or ''}{m.group(3)}{m.group(4)}***" if m else line)
        return "\n".join(out)
    if category == "redact_toml":
        return _TOML_API_KEY.sub(lambda m: f'{m.group(1)}"***"', text)
    return text
