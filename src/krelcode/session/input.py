"""用户输入统一化：规范化、命令分类、附件展开和可选 Hook 上下文。

输出结构化的 ``InputAction``，由 REPL/API 前台消费。本模块不执行命令，
也不启动 Agent；这样输入语义可以脱离终端独立测试和复用。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ..core.messages import ImageRef
from .attachments import expand_at_files


class InputKind(str, Enum):
    EMPTY = "empty"
    PROMPT = "prompt"
    COMMAND = "command"
    UI_COMMAND = "ui_command"
    CONTROL = "control"


CONTROL_COMMANDS = {"/exit", "/stop"}
UI_COMMANDS = {"/thinking", "/tool-output"}


@dataclass(frozen=True)
class InputEnvelope:
    """尚未解释的原始输入；source 为 terminal/api/ide 等来源。"""

    raw_text: str
    source: str = "terminal"


@dataclass(frozen=True)
class InputAction:
    """完成分类和附件展开后的标准输入动作。"""

    kind: InputKind
    text: str = ""
    raw_text: str = ""
    command: str = ""
    arguments: str = ""
    images: tuple[ImageRef, ...] = ()
    notes: tuple[str, ...] = ()
    source: str = "terminal"
    hook_context: tuple[str, ...] = ()

    @property
    def has_attachments(self) -> bool:
        return bool(self.images)


class InputProcessor:
    """把原始输入转换为可入队的 InputAction。"""

    def __init__(
        self,
        workspace_root: Path,
        *,
        expander: Optional[Callable] = None,
        hook_context_provider: Optional[
            Callable[[str], Awaitable[tuple[str, ...]]]
        ] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self._expander = expander or expand_at_files
        self._hook_context_provider = hook_context_provider

    async def process(self, envelope: InputEnvelope) -> InputAction:
        text = envelope.raw_text.strip()
        if not text:
            return InputAction(
                kind=InputKind.EMPTY,
                raw_text=envelope.raw_text,
                source=envelope.source,
            )

        if text.startswith("/"):
            command, separator, arguments = text.partition(" ")
            if command in CONTROL_COMMANDS:
                kind = InputKind.CONTROL
            elif command in UI_COMMANDS:
                kind = InputKind.UI_COMMAND
            else:
                kind = InputKind.COMMAND
            return InputAction(
                kind=kind,
                text=text,
                raw_text=envelope.raw_text,
                command=command,
                arguments=arguments.strip() if separator else "",
                source=envelope.source,
            )

        # 文件读取/图片编码是阻塞 IO，不占用事件循环。
        expanded, notes, images = await asyncio.to_thread(
            self._expander, text, self.workspace_root)
        action = InputAction(
            kind=InputKind.PROMPT,
            text=expanded,
            raw_text=envelope.raw_text,
            images=tuple(images),
            notes=tuple(notes),
            source=envelope.source,
        )
        if self._hook_context_provider is not None:
            try:
                context = await self._hook_context_provider(text)
            except Exception:
                # 输入 Hook 是增强项，失败不能吞掉用户输入。
                context = ()
            action = await self.with_hook_context(action, tuple(context))
        return action

    async def with_hook_context(
        self,
        action: InputAction,
        context: tuple[str, ...],
    ) -> InputAction:
        """附加输入 Hook 提供的上下文；保持动作类型和附件不变。"""
        if action.kind is not InputKind.PROMPT or not context:
            return action
        blocks = tuple(f"<hook_context>\n{entry}\n</hook_context>" for entry in context)
        text = "\n\n".join((action.text, *blocks)) if action.text else "\n\n".join(blocks)
        return replace(action, text=text, hook_context=context)
