"""密钥脱敏：把已解析的敏感值（如 LLM API key）从输出/落盘文本中替换为 ***。

使用方：ui.StreamRenderer（终端输出）、session.SessionRecorder（JSONL 落盘）、
以及 -m/--output-format json 的直接打印路径。

放在 core：纯字符串变换、零依赖，ui/session 两层都已依赖 core。
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

MASK = "***"
# 太短的秘密不脱敏：防止误把常见短词（如模型名/提示词片段）整体打码。
MIN_SECRET_LEN = 6

_COOKIE_HEADER_RE = re.compile(
    r"(?i)\b((?:set-)?cookie\s*:\s*).*?"
    r"(?=(?:\s+(?:authorization|proxy-authorization|api[_-]?key|"
    r"access[_-]?token|auth[_-]?token|secret|password)\s*[:=])|$)"
)
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|secret|password|passwd|cookie|set-cookie|authorization|"
    r"proxy-authorization|private[_-]?key)"
    r"(\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?([^\s,;]+)"
)
_TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}\b"),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def redact_obvious_secrets(text: Optional[str]) -> str:
    """Mask common credential shapes without requiring a known secret value."""
    value = str(text or "")
    if not value:
        return ""
    value = _COOKIE_HEADER_RE.sub(
        lambda match: match.group(1) + MASK, value)
    value = _KEY_VALUE_SECRET_RE.sub(
        lambda match: match.group(1) + match.group(2) + MASK, value)
    for pattern in _TOKEN_PATTERNS:
        value = pattern.sub(MASK, value)
    return value


class SecretRedactor:
    """持有少量敏感明文，统一做 replace 脱敏；永不打印/导出这些明文。"""

    def __init__(self, secrets: Optional[Iterable[str]] = None) -> None:
        self._secrets: List[str] = []
        for s in secrets or ():
            self.add(s)

    def add(self, secret: Optional[str]) -> None:
        if secret and len(secret) >= MIN_SECRET_LEN and secret not in self._secrets:
            self._secrets.append(secret)

    @property
    def secrets(self) -> Tuple[str, ...]:
        """只读副本（元组），供测试/诊断；调用方不得打印明文。"""
        return tuple(self._secrets)

    def redact(self, text: Optional[str]) -> str:
        out = redact_obvious_secrets(text)
        if not self._secrets:
            return out
        for s in self._secrets:
            if s in out:
                out = out.replace(s, MASK)
        return out
