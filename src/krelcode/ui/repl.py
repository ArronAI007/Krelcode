"""REPL：用户输入循环 + StreamRenderer 订阅（二者都只是事件旁路消费者）。

阻塞的 input() 经 asyncio.to_thread 桥接。最终回复由渲染器流式打印，
REPL 不再重复输出（未经流式的系统消息由渲染器在 RunFinished 兜底打印）。
多会话：/sessions 列表、/resume <n|latest|path> 会话内切换。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from ..agent.loop import AgentLoop
from ..session.manager import format_sessions, list_sessions
from .render import StreamRenderer

HELP_TEXT = """可用命令：
  /help          显示本帮助
  /history       查看当前对话历史
  /tools         列出已注册工具
  /session       显示当前会话文件路径
  /sessions      列出工作区的全部会话
  /resume <n>    切换到第 n 个会话（/sessions 里的序号；latest=最新）
  /mcp           查看 MCP 服务器状态；/mcp reload 重读配置并热重启
  /agents        列出子代理（后台任务）状态
  /compact       立即压缩对话历史（LLM 摘要，不必等预算超限）
  /undo          回滚上一轮对话的文件改动（检查点恢复；/undo list 查看栈）
  /reset         清空对话历史
  /exit          退出（Ctrl-D / Ctrl-C 同样有效）
其他任何输入都会作为消息发给 agent。"""


async def run_repl(
    loop: AgentLoop,
    tool_names: Optional[list] = None,
    session_path: Optional[str] = None,
    sessions_dir: Optional[Path] = None,
    mcp_supervisor=None,
    mcp_reload=None,
    subagents=None,
    checkpoints=None,
) -> int:
    from ..app import restore_session  # 延迟导入避免 ui→app 循环依赖

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
                print(f"  当前会话文件: {session_path or '（未启用，--no-session）'}")
                continue
            if user_input == "/sessions":
                if sessions_dir is None:
                    print("  （会话目录未知）")
                else:
                    print(format_sessions(list_sessions(sessions_dir)))
                continue
            if user_input.startswith("/resume"):
                target = user_input.split(maxsplit=1)[1].strip() if len(user_input.split(maxsplit=1)) > 1 else "latest"
                path = _resolve_resume_target(target, sessions_dir)
                if path is None:
                    print(f"  （找不到会话: {target}；用 /sessions 查看列表）")
                else:
                    restore_session(loop, path)
                continue
            if user_input == "/undo" or user_input.startswith("/undo "):
                if checkpoints is None:
                    print("  （检查点未启用：--no-checkpoint）")
                    continue
                arg = user_input.split(maxsplit=1)[1].strip() if len(user_input.split(maxsplit=1)) > 1 else ""
                if arg == "list":
                    lines = checkpoints.list_groups()
                    print("\n".join(f"  {l}" for l in lines) if lines else "  （无可回滚的检查点）")
                    continue
                result = checkpoints.undo_last()
                if result is None:
                    print("  （无可回滚的检查点）")
                else:
                    parts = []
                    if result.restored:
                        parts.append(f"还原 {len(result.restored)} 个: {', '.join(result.restored)}")
                    if result.deleted:
                        parts.append(f"删除新建 {len(result.deleted)} 个: {', '.join(result.deleted)}")
                    label = f"（{result.label}）" if result.label else ""
                    print(f"  [undo] 已回滚上一轮{label}: {'；'.join(parts) if parts else '无文件变化'}")
                continue
            if user_input == "/compact":
                if loop.context_manager is None:
                    print("  （上下文压缩未启用：--no-compress）")
                else:
                    from ..agent.tokens import estimate_history_tokens
                    before = estimate_history_tokens(loop.history)
                    done = await loop.context_manager.maybe_compress(loop.history, force=True)
                    after = estimate_history_tokens(loop.history)
                    if done:
                        print(f"  [compact] 已压缩: {before} → {after} tokens")
                    else:
                        print(f"  [compact] 历史太短，无需压缩（当前约 {before} tokens）")
                continue
            if user_input == "/agents":
                if subagents is None:
                    print("  （子代理未启用）")
                else:
                    lines = subagents.status_lines()
                    print("\n".join(f"  {l}" for l in lines) if lines else "  （暂无子代理）")
                continue
            if user_input == "/mcp" or user_input.startswith("/mcp "):
                if mcp_supervisor is None:
                    print("  （MCP 未启用）")
                    continue
                sub = user_input.split(maxsplit=1)[1].strip() if len(user_input.split(maxsplit=1)) > 1 else ""
                if sub == "reload":
                    restarted = await mcp_reload() if mcp_reload else []
                    note = f"，重启: {', '.join(restarted)}" if restarted else ""
                    print(f"  [mcp] 配置已同步{note}")
                for item in mcp_supervisor.status():
                    state = "在线" if item["alive"] else "已断开"
                    print(f"  {item['name']}（{item['server']}，{item['transport']}）"
                          f"{state}，{item['tools']} 个工具")
                if not mcp_supervisor.status():
                    print("  （没有配置 MCP 服务器；在 krelcode.toml 的 [mcp_servers.*] 里声明）")
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


def _resolve_resume_target(target: str, sessions_dir: Optional[Path]) -> Optional[Path]:
    """latest / 序号 / 文件路径 → 会话文件。"""
    if sessions_dir is None:
        return Path(target) if Path(target).is_file() else None
    if target == "latest":
        infos = list_sessions(sessions_dir)
        return infos[0].path if infos else None
    if target.isdigit():
        infos = list_sessions(sessions_dir)
        idx = int(target)
        if 1 <= idx <= len(infos):
            return infos[idx - 1].path
        return None
    path = Path(target)
    return path if path.is_file() else None
