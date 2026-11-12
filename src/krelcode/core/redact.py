"""密钥脱敏：把已解析的敏感值（如 LLM API key）从输出/落盘文本中替换为 ***。

使用方：ui.StreamRenderer（终端输出）、session.SessionRecorder（JSONL 落盘）、
以及 -m/--output-format json 的直接打印路径。

放在 core：纯字符串变换、零依赖，ui/session 两层都已依赖 core。
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

MASK = "***"
# 太短的秘密不脱敏：防止误把常见短词（如模型名/提示词片段）整体打码。
MIN_SECRET_LEN = 6


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
        if not text or not self._secrets:
            return text or ""
        out = text
        for s in self._secrets:
            if s in out:
                out = out.replace(s, MASK)
        return out
