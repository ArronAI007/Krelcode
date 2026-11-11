"""内核错误类型。所有层的可预期异常都派生自 KrelcodeError。"""

from __future__ import annotations

from typing import Optional


class KrelcodeError(Exception):
    """基类：可预期的业务错误（用于向用户/LLM 展示，而非崩溃）。"""


class ConfigError(KrelcodeError):
    """配置问题：缺少 API key、未知 provider、非法配置等。"""


class LLMError(KrelcodeError):
    """LLM 调用失败（网络、HTTP 错误、响应格式非法等）。"""


class LLMHTTPError(LLMError):
    """LLM 端点返回非 2xx：带 status 与响应体，便于诊断。"""

    def __init__(self, status: int, body: str, provider: str = "") -> None:
        prefix = f"[{provider}] " if provider else ""
        super().__init__(f"{prefix}LLM HTTP {status}: {body[:2000]}")
        self.status = status
        self.body = body
        self.provider = provider


class ToolNotFoundError(KrelcodeError):
    """LLM 请求了未注册的工具。"""
