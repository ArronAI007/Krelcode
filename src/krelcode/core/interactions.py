"""交互能力的核心端口。

AgentLoop 只依赖“提交请求并等待结果”的窄协议；具体 broker、前台和传输
实现位于外层。这样模型工具请求输入时不会把 EventBus、stdin 或终端类型
泄漏进工具实现。
"""

from __future__ import annotations

from typing import Optional, Protocol, Tuple


class UserInputExchangePort(Protocol):
    """一次待完成的用户输入请求。"""

    @property
    def request_id(self) -> str: ...

    async def wait(self) -> Optional[str]: ...

    def cancel(self) -> None: ...


class UserInputPort(Protocol):
    """AgentLoop 可注入的前台输入端口。"""

    def submit(
        self,
        prompt: str,
        options: Tuple[str, ...] = (),
        *,
        allow_free_text: bool = True,
        request_id: str = "",
    ) -> UserInputExchangePort: ...
