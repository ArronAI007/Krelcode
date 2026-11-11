"""REPL：常驻 reader + 状态机（Step 26 重构，支持运行中 steering 与 /stop）。

结构：
- reader task 持续读输入 → inbox 队列（input_fn 可注入，测试零终端）
- 主循环用 asyncio.wait 同时盯 inbox 与运行中的 agent 任务：
  · IDLE：处理 /命令；普通消息（含 @文件展开）→ 启动 loop.run 后台任务
  · RUNNING：普通文本 → loop.steering_queue（模型在下一 step 看到）；
    /stop 取消并消毒历史；/exit 停止并退出；其他斜杠命令给提示
- 任务自然结束后，队列里未送达的插话会被丢弃并提示（语义可见，不静默吞）
- 最终回复由 StreamRenderer 流式打印（订阅事件总线）
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from ..agent.loop import AgentLoop
from ..session.export import export_session
from ..session.manager import format_sessions, list_sessions
from .atfiles import expand_at_files
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
  /undo          回滚上一轮对话的文件改动（检查点恢复；/undo list 查看栈）
  /cost          显示 token 用量（本轮 + 会话累计 + 估算成本）
  /model [p/m]   查看/热切换模型（"gpt-4o" 沿用当前 provider，
                 "anthropic/claude-sonnet-4-5" 跨 provider）
  /allow         查看命令审批白名单；/allow add <程序名> 添加、
                 /allow remove <程序名> 移除（持久化到工作区）
  /context       显示上下文统计；/context refresh 重新采集工作区上下文
  /export [路径] 把当前会话导出为 Markdown（缺省存 .krelcode/exports/）
  /compact       立即压缩对话历史（LLM 摘要，不必等预算超限）
  /stop          中止正在运行的任务（历史自动修复）
  /reset         清空对话历史
  /exit          退出（Ctrl-D / Ctrl-C 同样有效）
任务运行中：直接输入文字 = 插话（steering，模型在下一步看到）；/stop 中止。
其他任何输入都会作为消息发给 agent；消息里可用 @路径 引用文件。"""

RUNNING_HINT = "  （任务运行中：直接输入=插话，/stop 中止，/exit 退出）"
STOPPED_NOTE = "  [stopped] 已中止（历史已修复，可继续对话）"


async def run_repl(
    loop: AgentLoop,
    tool_names: Optional[list] = None,
    session_path: Optional[str] = None,
    sessions_dir: Optional[Path] = None,
    mcp_supervisor=None,
    mcp_reload=None,
    subagents=None,
    checkpoints=None,
    usage=None,
    model_switch=None,
    input_fn=None,
) -> int:
    from ..app import restore_session  # 延迟导入避免 ui→app 循环依赖

    renderer = StreamRenderer()
    unsubscribe = loop.bus.subscribe(renderer)
    read_line = input_fn or (lambda prompt: asyncio.to_thread(input, prompt))
    workspace_root = loop.tool_context.workspace_root

    tools_note = f"，工具: {', '.join(tool_names)}" if tool_names else ""
    print(f"Krelcode REPL（/help 查看命令，/exit 退出{tools_note}）")

    running: Optional[asyncio.Task] = None
    state = {"running": False}
    inbox: asyncio.Queue = asyncio.Queue()

    def current_prompt() -> str:
        return "↪ > " if state["running"] else "you> "

    async def reader() -> None:
        while True:
            try:
                line = await read_line(current_prompt())
            except (EOFError, KeyboardInterrupt):
                await inbox.put(None)
                return
            except asyncio.CancelledError:
                raise
            await inbox.put(line)

    def drain_steering_notes() -> None:
        """任务结束后丢弃未送达的插话（可见地），避免悄悄留到下一轮。"""
        while not loop.steering_queue.empty():
            try:
                leftover = loop.steering_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            print(f"  [steering] 任务已结束，未送达的插话被丢弃: {str(leftover)[:60]}")

    reader_task = asyncio.create_task(reader())
    inbox_get = asyncio.create_task(inbox.get())
    try:
        while True:
            wait_set = {inbox_get}
            if running is not None:
                wait_set.add(running)
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            # 1) 运行中的任务结束（自然完成/异常/被取消）
            if running is not None and running in done:
                state["running"] = False
                try:
                    running.result()
                except asyncio.CancelledError:
                    renderer.end_stream()
                    print(STOPPED_NOTE)
                except Exception as e:
                    renderer.end_stream()
                    print(f"错误：{type(e).__name__}: {e}")
                running = None
                drain_steering_notes()

            if inbox_get not in done:
                continue
            item = inbox_get.result()
            inbox_get = asyncio.create_task(inbox.get())

            # 2) EOF：停掉运行中任务并退出
            if item is None:
                if running is not None:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                print()
                return 0

            user_input = item.strip()

            # 3) RUNNING 状态：steering / /stop / /exit
            if running is not None:
                if user_input == "/stop":
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    state["running"] = False
                    running = None
                    renderer.end_stream()
                    print(STOPPED_NOTE)
                    drain_steering_notes()
                    continue
                if user_input == "/exit":
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    print()
                    return 0
                if user_input.startswith("/"):
                    print(RUNNING_HINT)
                    continue
                if user_input:
                    loop.steering_queue.put_nowait(user_input)
                    preview = user_input if len(user_input) <= 40 else user_input[:40] + "…"
                    print(f"  [steering] 插话已排队（模型下一步看到）: {preview}")
                continue

            # 4) IDLE 状态：命令与消息
            if not user_input:
                continue
            if user_input == "/exit":
                return 0
            if user_input == "/stop":
                print("  （当前没有运行中的任务）")
                continue
            handled = await _handle_command(
                user_input, loop, renderer,
                session_path=session_path, sessions_dir=sessions_dir,
                mcp_supervisor=mcp_supervisor, mcp_reload=mcp_reload,
                subagents=subagents, checkpoints=checkpoints, usage=usage,
                restore_session=restore_session, workspace_root=workspace_root,
                model_switch=model_switch,
            )
            if handled is not None:
                if handled is False:      # /reset 等已处理
                    continue
                continue
            # 普通消息：@展开 → 后台任务启动
            expanded, at_notes = expand_at_files(user_input, workspace_root)
            for note_text in at_notes:
                print(f"  [@] {note_text}")
            state["running"] = True
            running = asyncio.create_task(loop.run(expanded))
    finally:
        reader_task.cancel()
        inbox_get.cancel()
        unsubscribe()


async def _handle_command(user_input, loop, renderer, *, session_path, sessions_dir,
                          mcp_supervisor, mcp_reload, subagents, checkpoints,
                          usage, restore_session, workspace_root, model_switch=None):
    """处理 IDLE 态斜杠命令；非命令返回 None。"""
    if not user_input.startswith("/"):
        return None

    if user_input == "/help":
        print(HELP_TEXT)
    elif user_input == "/tools":
        for name in loop.registry.names():
            tool = loop.registry.get(name)
            print(f"  {name}: {tool.description}")
    elif user_input == "/session":
        print(f"  当前会话文件: {session_path or '（未启用，--no-session）'}")
    elif user_input == "/sessions":
        if sessions_dir is None:
            print("  （会话目录未知）")
        else:
            print(format_sessions(list_sessions(sessions_dir)))
    elif user_input.startswith("/resume"):
        parts = user_input.split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else "latest"
        path = _resolve_resume_target(target, sessions_dir)
        if path is None:
            print(f"  （找不到会话: {target}；用 /sessions 查看列表）")
        else:
            restore_session(loop, path)
    elif user_input == "/mcp" or user_input.startswith("/mcp "):
        if mcp_supervisor is None:
            print("  （MCP 未启用）")
        else:
            parts = user_input.split(maxsplit=1)
            sub = parts[1].strip() if len(parts) > 1 else ""
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
    elif user_input == "/agents":
        if subagents is None:
            print("  （子代理未启用）")
        else:
            lines = subagents.status_lines()
            print("\n".join(f"  {l}" for l in lines) if lines else "  （暂无子代理）")
    elif user_input == "/undo" or user_input.startswith("/undo "):
        if checkpoints is None:
            print("  （检查点未启用：--no-checkpoint）")
        else:
            parts = user_input.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            if arg == "list":
                lines = checkpoints.list_groups()
                print("\n".join(f"  {l}" for l in lines) if lines else "  （无可回滚的检查点）")
            else:
                result = checkpoints.undo_last()
                if result is None:
                    print("  （无可回滚的检查点）")
                else:
                    parts_desc = []
                    if result.restored:
                        parts_desc.append(f"还原 {len(result.restored)} 个: {', '.join(result.restored)}")
                    if result.deleted:
                        parts_desc.append(f"删除新建 {len(result.deleted)} 个: {', '.join(result.deleted)}")
                    label = f"（{result.label}）" if result.label else ""
                    print(f"  [undo] 已回滚上一轮{label}: {'；'.join(parts_desc) if parts_desc else '无文件变化'}")
    elif user_input == "/model" or user_input.startswith("/model "):
        if model_switch is None:
            print("  （模型切换不可用）")
        else:
            parts = user_input.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else None
            print(f"  [model] {model_switch(target)}")
    elif user_input == "/allow" or user_input.startswith("/allow "):
        rules = getattr(loop.policy, "rules", None)
        if rules is None:
            print("  （安全策略未启用（--no-sandbox），无审批白名单）")
        else:
            parts = user_input.split()
            if len(parts) == 1:
                listed = rules.as_list()
                print("  已允许的程序: " + (", ".join(listed) if listed else "（空）"))
                print(f"  规则文件: {rules.path or '（仅内存）'}")
            elif len(parts) == 3 and parts[1] == "add":
                added = rules.add_program(parts[2])
                print(f"  [allow] {'已添加' if added else '已存在，无需重复添加'}: {parts[2]}")
            elif len(parts) == 3 and parts[1] == "remove":
                removed = rules.remove_program(parts[2])
                print(f"  [allow] {'已移除' if removed else '规则不存在'}: {parts[2]}")
            else:
                print("  用法: /allow | /allow add <程序名> | /allow remove <程序名>")
    elif user_input == "/cost":
        print(f"  [cost] {usage.summary()}" if usage else "  （用量统计不可用）")
    elif user_input == "/context" or user_input.startswith("/context "):
        from ..agent.tokens import estimate_history_tokens
        parts = user_input.split(maxsplit=1)
        sub = parts[1].strip() if len(parts) > 1 else ""
        if sub == "refresh":
            from ..agent.context import build_workspace_context
            from ..agent.prompts import build_system_prompt
            ctx_text = await build_workspace_context(workspace_root)
            if loop.history and loop.history[0].role == "system":
                loop.history[0].content = build_system_prompt(ctx_text)
                print("  [context] 工作区上下文已刷新（目录树/git/规则文件）")
            else:
                print("  [context] 历史中无 system 消息，未刷新")
        else:
            total = estimate_history_tokens(loop.history)
            budget = (loop.context_manager.max_tokens
                      if loop.context_manager else "未启用压缩")
            tool_msgs = sum(1 for m in loop.history if m.role == "tool")
            print(f"  [context] 历史 {len(loop.history)} 条消息（含 {tool_msgs} 条工具结果）"
                  f" · 估算 {total} tokens · 预算 {budget}")
    elif user_input == "/compact":
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
    elif user_input == "/export" or user_input.startswith("/export "):
        parts = user_input.split(maxsplit=1)
        try:
            if len(parts) > 1:
                out = export_session(loop.history, Path(parts[1].strip()))
            else:
                out = export_session(
                    loop.history,
                    export_dir=workspace_root / ".krelcode" / "exports")
            print(f"  [export] 已导出: {out}")
        except OSError as e:
            print(f"  [export] 导出失败: {e}")
    elif user_input == "/history":
        for m in loop.history:
            content = m.content or ""
            extra = f" tool_calls={[tc.name for tc in m.tool_calls]}" if m.tool_calls else ""
            print(f"  [{m.role}]{extra} {content}")
    elif user_input == "/reset":
        loop.reset()
        print("（历史已清空）")
    else:
        print(f"  （未知命令: {user_input.split()[0]}，/help 查看可用命令）")
    return False


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
