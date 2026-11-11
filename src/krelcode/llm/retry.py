"""RetryingProvider：LLMProvider 的透明重试装饰器。

策略：
- 只重试**瞬态**错误：HTTP 408/409/425/429/5xx、网络错误、超时
  （即 LLMHTTPError 的可重试状态码，与非 HTTP 的 LLMError）
- 不重试：401/403/400 等确定性错误、ConfigError（非 LLMError 家族）
- **已开始产出事件后绝不重试**：TextDelta 已被消费，重放会造成重复输出；
  好在 HTTP 层错误（最常见的瞬态故障）都发生在首个事件之前
- 指数退避：base_delay * backoff^attempt；sleep 可注入（测试零等待）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Optional, Sequence

from ..core.errors import LLMError, LLMHTTPError
from .base import LLMEvent, ProviderCapabilities, ToolSpec
from ..core.messages import Message

RETRYABLE_STATUSES = frozenset({408, 409, 425, 429})
# Retry-After 超过该值则放弃重试（让用户等 2 分钟以上不如快速失败）
RETRY_AFTER_HARD_CAP = 120.0


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2      # 额外重试次数（总尝试 = 1 + max_retries）
    base_delay: float = 1.0
    backoff: float = 2.0
    max_delay: float = 30.0

    def delay_for(self, attempt: int) -> float:
        """attempt 从 0 计：第 1 次重试前 delay_for(0)=base，之后指数增长。"""
        return min(self.base_delay * (self.backoff ** attempt), self.max_delay)


def is_retryable(error: BaseException) -> bool:
    if isinstance(error, LLMHTTPError):
        return error.status in RETRYABLE_STATUSES or 500 <= error.status < 600
    if isinstance(error, LLMError):
        return True   # 网络错误/超时/流解析异常，按瞬态处理
    return False


class RetryingProvider:
    """包装任意 LLMProvider，透明重试瞬态失败。"""

    def __init__(
        self,
        inner,
        policy: Optional[RetryPolicy] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
    ) -> None:
        self._inner = inner
        self._policy = policy or RetryPolicy()
        self._sleep = sleep or asyncio.sleep
        self._on_retry = on_retry
        self.retry_count = 0  # 进程内累计（观测/测试用）

    # 端口属性透传
    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return getattr(self._inner, "model", "?")

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._inner.capabilities

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Optional[Sequence[ToolSpec]] = None,
    ) -> AsyncIterator[LLMEvent]:
        attempt = 0
        emitted = False
        while True:
            try:
                async for event in self._inner.stream(messages, tools):
                    emitted = True
                    yield event
                return
            except Exception as e:  # noqa: BLE001 —— 按策略分类处理
                retryable = (
                    not emitted
                    and attempt < self._policy.max_retries
                    and is_retryable(e)
                )
                if not retryable:
                    raise
                delay = self._policy.delay_for(attempt)
                retry_after = getattr(e, "retry_after", None)
                if retry_after is not None:
                    if retry_after > RETRY_AFTER_HARD_CAP:
                        raise   # 服务器要求等待过久，快速失败
                    delay = max(delay, retry_after)   # 尊重服务器指定等待
                attempt += 1
                self.retry_count += 1
                if self._on_retry is not None:
                    self._on_retry(attempt, delay, e)
                await self._sleep(delay)
