"""敏感文件读保护与脱敏（Step 45）。

目标：堵住「文件侧」密钥泄漏——coding agent 的 read_file/grep 不应直接读出
`.env`/`krelcode.toml` 里的 API key、`*.key`/`*.pem`/`credentials*` 等凭据。

分类（classify_secret_path）：
- "block"      ：纯密钥/凭据文件 → read_file 拒读、grep 跳过
- "redact_env" ：.env 类 KEY=VALUE 文件 → 保留键名、隐藏值后返回
- "redact_toml"：krelcode.toml → 隐藏 api_key/token/password 等凭据值，
  保留 api_key_env、token_budget 等非秘密配置

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
    "*.pem", "*.key", "*.p12", "*.pfx", "*.p8",
    "*.jks", "*.keystore", "*.truststore", "*.kdb", "*.kdbx", "*.ovpn",
    "*.tfstate", "*.tfstate.*",
    "id_rsa*", "id_ed25519*",
    "credentials*", "secrets.*",
    ".npmrc", ".pypirc", ".netrc", ".pgpass", ".htpasswd",
    ".git-credentials", ".vault-token",
    "service-account*.json", "application_default_credentials.json",
    "token.json", "tokens.json", "client_secret*.json",
)

# 脱敏后返回的配置文件（保留结构、隐藏值）。
REDACT_ENV_GLOBS = (".env", ".env.*", ".envrc")
REDACT_TOML_GLOBS = ("krelcode.toml",)

# 目录级敏感：路径中任一段命中即整体 block。
BLOCK_DIRS = (
    ".git", ".krelcode", ".codex", ".ssh", ".aws", ".azure", ".kube",
    ".gnupg", ".docker", ".password-store", "keychains",
)
# 只对明确属于凭据存储的配置子目录做前缀拦截，避免误伤普通 `.config`。
BLOCK_PATH_PREFIXES = (
    ".config/gcloud",
    ".config/gh",
    ".config/hub",
    ".config/op",
    ".config/1password",
    ".config/doctl",
    ".config/rclone",
    ".config/sops",
)


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
    lowered_parts = [part.lower() for part in parts]
    for part in lowered_parts:
        if part in BLOCK_DIRS:
            return "block"
    lowered_rel = rel.lower()
    for prefix in BLOCK_PATH_PREFIXES:
        if lowered_rel == prefix or lowered_rel.startswith(f"{prefix}/"):
            return "block"

    base = lowered_parts[-1]
    for pattern in REDACT_ENV_GLOBS:
        if fnmatch.fnmatch(lowered_rel, pattern) or fnmatch.fnmatch(base, pattern):
            return "redact_env"
    for pattern in REDACT_TOML_GLOBS:
        if fnmatch.fnmatch(lowered_rel, pattern) or fnmatch.fnmatch(base, pattern):
            return "redact_toml"
    for pattern in BLOCK_GLOBS:
        if fnmatch.fnmatch(lowered_rel, pattern) or fnmatch.fnmatch(base, pattern):
            return "block"
    return None


# .env：KEY=VALUE / export KEY=VALUE / KEY = VALUE（保留注释与空行）
_ENV_LINE = re.compile(
    r"^(\s*)(export\s+)?([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(\S.*)$"
)

# krelcode.toml：隐藏常见凭据键，但保留 api_key_env 这类变量名配置。
_TOML_SECRET_KEYS = frozenset({
    "api_key", "apikey", "token", "access_token", "refresh_token",
    "id_token", "session_token", "secret", "client_secret", "secret_key",
    "password", "passwd", "credential", "credentials", "private_key",
    "access_key", "secret_access_key",
})
_TOML_SECRET_SUFFIXES = (
    "_api_key", "_token", "_secret", "_password", "_passwd",
    "_credential", "_credentials", "_private_key", "_access_key",
)
_TOML_ASSIGNMENT = re.compile(
    r'^(\s*)((?:[A-Za-z0-9_.-]+|"[^"]+"|\'[^\']+\')\s*=\s*)(.*)$')


def _is_secret_toml_key(key: str) -> bool:
    normalized = key.strip().strip("\"'").lower().replace("-", "_")
    if normalized.endswith("_env"):
        return False
    leaf = normalized.rsplit(".", 1)[-1]
    return leaf in _TOML_SECRET_KEYS or leaf.endswith(_TOML_SECRET_SUFFIXES)


def _redact_toml(text: str) -> str:
    out = []
    for line in text.splitlines():
        match = _TOML_ASSIGNMENT.match(line)
        if match is None:
            out.append(line)
            continue
        raw_key = match.group(2).split("=", 1)[0]
        if _is_secret_toml_key(raw_key):
            out.append(f'{match.group(1)}{match.group(2)}"***"')
        else:
            out.append(line)
    trailing = "\n" if text.endswith("\n") else ""
    return "\n".join(out) + trailing


def redact_secret_text(text: str, category: str) -> str:
    """按分类对文本脱敏；未知分类原样返回（block 场景调用方已拒读）。"""
    if category == "redact_env":
        out = []
        for line in text.splitlines():
            m = _ENV_LINE.match(line)
            out.append(f"{m.group(1)}{m.group(2) or ''}{m.group(3)}{m.group(4)}***" if m else line)
        trailing = "\n" if text.endswith("\n") else ""
        return "\n".join(out) + trailing
    if category == "redact_toml":
        return _redact_toml(text)
    return text
