"""REPL：用户输入循环 + StreamRenderer 订阅（二者都只是事件旁路消费者）。

阻塞的 input() 经 asyncio.to_thread 桥接。最终回复由渲染器流式打印，
REPL 不再重复输出（未经流式的系统消息由渲染器在 RunFinished 兜底打印）。
"""

from __future__ import annotations

import asyncio
from typing import Optional

from ..agent.loop import AgentLoop
from .render import StreamRenderer

HELP_TEXT = """可用命令：
  /help     显示本帮助
  /history  查看当前对话历史
  /tools    列出已注册工具
  /session  显示当前会话文件路径
  /reset    清空对话历史
  /exit     退出（Ctrl-D / Ctrl-C 同样有效）
其他任何输入都会作为消息发给 agent。"""


async def run_repl(
    loop: AgentLoop,
    tool_names: Optional[list] = None,
    session_path: Optional[str] = None,
) -> int:
    renderer = StreamRenderer()
    unsubscribe = loop.bus.subscribe(renderer)
    tools_note = f"，工具: {', '.join(tool_names)}" if tool_names else ""
    print(f"Krelcode REPL（/help 查看命令，/exit 退出{tools_note}）")
    try:
        while True:
            try:
                user_input = (await asyncio.to_thread(input, "you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not user_input:
                continue
            if user_input == "/exit":
                return 0
            if user_input == "/help":
                print(HELP_TEXT)
                continue
            if user_input == "/tools":
                for name in loop.registry.names():
                    tool = loop.registry.get(name)
                    print(f"  {name}: {tool.description}")
                continue
            if user_input == "/session":
                print(f"  会话文件: {session_path or '（未启用，--no-session）'}")
                continue
            if user_input == "/history":
                for m in loop.history:
                    content = m.content or ""
                    extra = f" tool_calls={[tc.name for tc in m.tool_calls]}" if m.tool_calls else ""
                    print(f"  [{m.role}]{extra} {content}")
                continue
            if user_input == "/reset":
                loop.reset()
                print("（历史已清空）")
                continue
            try:
                await loop.run(user_input)
            except Exception as e:
                renderer.end_stream()
                print(f"错误：{type(e).__name__}: {e}")
    finally:
        unsubscribe()
