"""交互会话控制层。

负责会话级业务动作：斜杠命令、历史恢复、任务生命周期和 steering。
本模块不得导入终端渲染、PromptWriter 或任何 ``ui.*`` 模块；纯展示命令
（/thinking、/tool-output）由 UI 层处理。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..agent.context import build_workspace_context
from ..agent.loop import AgentLoop
from ..agent.prompts import build_system_prompt
from ..agent.tokens import estimate_history_tokens
from .export import export_session
from .manager import format_sessions, list_sessions, resolve_resume_target
from .restore import restore_session

HELP_TEXT = """可用命令：
  /help          显示本帮助
  /history       查看当前对话历史
  /thinking      展开最近一次思考（保留 Markdown 多行）
  /tool-output   列出工具输出；/tool-output <编号|last> 展开灰色面板
  /tools         列出已注册工具
  /session       显示当前会话文件路径
  /sessions      列出工作区的全部会话
  /resume <n>    切换到第 n 个会话（/sessions 里的序号；latest=最新）
  /mcp           查看 MCP 服务器状态；/mcp reload 重读配置并热重启
  /agents        列出子代理（后台任务）状态
  /skills        列出 Skill；/skills reload 热重载；/skills show <name> 查看详情
  /skill <name> [参数]  用户主动加载并执行一个 Skill
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
任务运行中：直接输入文字 = 插话（steering）；/thinking 展开思考；
/tool-output <编号> 展开工具输出；/stop 中止。
输入 / 会实时显示匹配命令，Tab 可补全唯一匹配；↑/↓ 调取本次会话的历史问题。
其他任何输入都会作为消息发给 agent；消息里可用 @路径 引用文件。"""


@dataclass(frozen=True)
class CommandResult:
    """会话命令的可渲染结果；业务层不直接操作终端。"""

    handled: bool
    output: tuple[str, ...] = ()
    run_prompt: Optional[str] = None
    clear_tool_outputs: bool = False

    @classmethod
    def unhandled(cls) -> "CommandResult":
        return cls(handled=False)


class SessionController:
    """拥有会话命令与会话任务状态，不依赖任何 UI 实现。"""

    def __init__(
        self,
        loop: AgentLoop,
        *,
        session_path: Optional[str] = None,
        sessions_dir: Optional[Path] = None,
        mcp_supervisor=None,
        mcp_reload=None,
        subagents=None,
        checkpoints=None,
        usage=None,
        model_switch=None,
        skills=None,
        skills_reload=None,
        workspace_root: Optional[Path] = None,
    ) -> None:
        self.loop = loop
        self.session_path = session_path
        self.sessions_dir = sessions_dir
        self.mcp_supervisor = mcp_supervisor
        self.mcp_reload = mcp_reload
        self.subagents = subagents
        self.checkpoints = checkpoints
        self.usage = usage
        self.model_switch = model_switch
        self.skills = skills
        self.skills_reload = skills_reload
        self.workspace_root = workspace_root or loop.tool_context.workspace_root
        self._task: Optional[asyncio.Task] = None

    @property
    def active_task(self) -> Optional[asyncio.Task]:
        return self._task

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start_run(self, message: str, *, images=None) -> asyncio.Task:
        """启动一轮 Agent 执行并返回 task；同一控制器不允许并发两轮。"""
        if self.is_running:
            raise RuntimeError("已有任务正在运行")
        self._task = asyncio.create_task(self.loop.run(message, images=images))
        return self._task

    def steer(self, message: str) -> str:
        """把运行中插话放入 Agent steering 队列，返回可见摘要。"""
        text = message.strip()
        if not text:
            return ""
        self.loop.steering_queue.put_nowait(text)
        return text if len(text) <= 40 else text[:40] + "…"

    def drain_steering(self) -> list[Any]:
        """任务结束后取出未送达插话，避免污染下一轮。"""
        pending: list[Any] = []
        while not self.loop.steering_queue.empty():
            try:
                pending.append(self.loop.steering_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return pending

    async def stop(self) -> bool:
        """取消当前任务；没有活动任务时返回 False。"""
        task = self._task
        if task is None or task.done():
            self._task = None
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self._task = None
        return True

    async def execute_command(self, user_input: str) -> CommandResult:
        """执行会话级斜杠命令；普通消息返回 unhandled。"""
        if not user_input.startswith("/"):
            return CommandResult.unhandled()

        output: list[str] = []

        def emit(text: str) -> None:
            output.append(str(text))

        run_prompt: Optional[str] = None
        clear_tool_outputs = False

        if user_input == "/help":
            emit(HELP_TEXT)
        elif user_input == "/tools":
            for name in self.loop.registry.names():
                tool = self.loop.registry.get(name)
                emit(f"  {name}: {tool.description}")
        elif user_input == "/session":
            emit(f"  当前会话文件: {self.session_path or '（未启用，--no-session）'}")
        elif user_input == "/sessions":
            if self.sessions_dir is None:
                emit("  （会话目录未知）")
            else:
                emit(format_sessions(list_sessions(self.sessions_dir)))
        elif user_input.startswith("/resume"):
            parts = user_input.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else "latest"
            path = resolve_resume_target(target, self.sessions_dir)
            if path is None:
                emit(f"  （找不到会话: {target}；用 /sessions 查看列表）")
            elif restore_session(self.loop, path, log=emit):
                clear_tool_outputs = True
        elif user_input == "/mcp" or user_input.startswith("/mcp "):
            if self.mcp_supervisor is None:
                emit("  （MCP 未启用）")
            else:
                parts = user_input.split(maxsplit=1)
                sub = parts[1].strip() if len(parts) > 1 else ""
                if sub == "reload":
                    restarted = await self.mcp_reload() if self.mcp_reload else []
                    note = f"，重启: {', '.join(restarted)}" if restarted else ""
                    emit(f"  [mcp] 配置已同步{note}")
                status = self.mcp_supervisor.status()
                for item in status:
                    state = "在线" if item["alive"] else "已断开"
                    emit(f"  {item['name']}（{item['server']}，{item['transport']}）"
                         f"{state}，{item['tools']} 个工具")
                if not status:
                    emit("  （没有配置 MCP 服务器；在 krelcode.toml 的 "
                         "[mcp_servers.*] 里声明）")
        elif user_input == "/agents":
            if self.subagents is None:
                emit("  （子代理未启用）")
            else:
                lines = self.subagents.status_lines()
                emit("\n".join(f"  {line}" for line in lines)
                     if lines else "  （暂无子代理）")
        elif user_input == "/skills" or user_input.startswith("/skills "):
            if self.skills is None or not getattr(self.skills, "enabled", False):
                emit("  （Skills 未启用：移除 --no-skills 或在配置中设置 "
                     "skills.enabled=true）")
            else:
                parts = user_input.split(maxsplit=2)
                sub = parts[1].strip() if len(parts) > 1 else ""
                if sub == "reload":
                    report = (self.skills_reload() if self.skills_reload
                              else self.skills.reload())
                    emit(f"  [skills] 已重载 {report.count} 个 Skill")
                    for issue in report.issues:
                        emit(f"  [skills] 跳过无效 Skill: {issue.path}: {issue.message}")
                elif sub == "show":
                    if len(parts) < 3 or not parts[2].strip():
                        emit("  用法: /skills show <name>")
                    else:
                        emit(_format_skill_details(self.skills, parts[2].strip()))
                else:
                    query = sub
                    items = self.skills.list(query=query)
                    if not items:
                        emit("  （没有匹配的 Skill）" if query else "  （未发现 Skill）")
                    for skill in items:
                        flags = []
                        if not skill.user_invocable:
                            flags.append("仅模型")
                        if skill.disable_model_invocation:
                            flags.append("仅用户")
                        flag_text = f" [{', '.join(flags)}]" if flags else ""
                        hint = f" · {skill.argument_hint}" if skill.argument_hint else ""
                        emit(f"  {skill.name}: {skill.description}{hint}{flag_text}")
                    if items:
                        emit("  使用 /skills show <name> 查看详情，"
                             "/skill <name> [参数] 调用")
                    for issue in self.skills.issues:
                        emit(f"  [skills] 跳过无效 Skill: {issue.path}: {issue.message}")
        elif user_input.startswith("/skill "):
            if self.skills is None or not getattr(self.skills, "enabled", False):
                emit("  （Skills 未启用：移除 --no-skills 或在配置中设置 "
                     "skills.enabled=true）")
            else:
                parts = user_input.split(maxsplit=2)
                if len(parts) < 2 or not parts[1].strip():
                    emit("  用法: /skill <name> [参数]")
                else:
                    name = parts[1].strip()
                    arguments = parts[2] if len(parts) > 2 else ""
                    try:
                        run_prompt = self.skills.user_prompt(name, arguments)
                    except Exception as exc:
                        emit(f"  [skill] {exc}")
                    else:
                        emit(f"  [skill] 已加载并执行: {name}")
        elif user_input == "/undo" or user_input.startswith("/undo "):
            if self.checkpoints is None:
                emit("  （检查点未启用：--no-checkpoint）")
            else:
                parts = user_input.split(maxsplit=1)
                arg = parts[1].strip() if len(parts) > 1 else ""
                if arg == "list":
                    lines = self.checkpoints.list_groups()
                    emit("\n".join(f"  {line}" for line in lines)
                         if lines else "  （无可回滚的检查点）")
                else:
                    result = self.checkpoints.undo_last()
                    if result is None:
                        emit("  （无可回滚的检查点）")
                    else:
                        parts_desc = []
                        if result.restored:
                            parts_desc.append(
                                f"还原 {len(result.restored)} 个: "
                                f"{', '.join(result.restored)}")
                        if result.deleted:
                            parts_desc.append(
                                f"删除新建 {len(result.deleted)} 个: "
                                f"{', '.join(result.deleted)}")
                        label = f"（{result.label}）" if result.label else ""
                        detail = "；".join(parts_desc) if parts_desc else "无文件变化"
                        emit(f"  [undo] 已回滚上一轮{label}: {detail}")
        elif user_input == "/model" or user_input.startswith("/model "):
            if self.model_switch is None:
                emit("  （模型切换不可用）")
            else:
                parts = user_input.split(maxsplit=1)
                target = parts[1].strip() if len(parts) > 1 else None
                emit(f"  [model] {self.model_switch(target)}")
        elif user_input == "/allow" or user_input.startswith("/allow "):
            rules = getattr(self.loop.policy, "rules", None)
            if rules is None:
                emit("  （安全策略未启用（--no-sandbox），无审批白名单）")
            else:
                parts = user_input.split()
                if len(parts) == 1:
                    listed = rules.as_list()
                    emit("  已允许的程序: " + (", ".join(listed) if listed else "（空）"))
                    emit(f"  规则文件: {rules.path or '（仅内存）'}")
                elif len(parts) == 3 and parts[1] == "add":
                    added = rules.add_program(parts[2])
                    emit(f"  [allow] {'已添加' if added else '已存在，无需重复添加'}: "
                         f"{parts[2]}")
                elif len(parts) == 3 and parts[1] == "remove":
                    removed = rules.remove_program(parts[2])
                    emit(f"  [allow] {'已移除' if removed else '规则不存在'}: {parts[2]}")
                else:
                    emit("  用法: /allow | /allow add <程序名> | "
                         "/allow remove <程序名>")
        elif user_input == "/cost":
            emit(f"  [cost] {self.usage.summary()}" if self.usage
                 else "  （用量统计不可用）")
        elif user_input == "/context" or user_input.startswith("/context "):
            parts = user_input.split(maxsplit=1)
            sub = parts[1].strip() if len(parts) > 1 else ""
            if sub == "refresh":
                context = await build_workspace_context(self.workspace_root)
                if self.loop.history and self.loop.history[0].role == "system":
                    base_prompt = build_system_prompt(context)
                    self.loop.skill_base_prompt = base_prompt
                    self.loop.history[0].content = (
                        self.skills.apply_to_prompt(base_prompt)
                        if self.skills else base_prompt)
                    emit("  [context] 工作区上下文已刷新（目录树/git/规则文件）")
                else:
                    emit("  [context] 历史中无 system 消息，未刷新")
            else:
                total = estimate_history_tokens(self.loop.history)
                budget = (self.loop.context_manager.max_tokens
                          if self.loop.context_manager else "未启用压缩")
                tool_messages = sum(
                    1 for message in self.loop.history if message.role == "tool")
                emit(f"  [context] 历史 {len(self.loop.history)} 条消息"
                     f"（含 {tool_messages} 条工具结果） · 估算 {total} tokens"
                     f" · 预算 {budget}")
        elif user_input == "/compact":
            if self.loop.context_manager is None:
                emit("  （上下文压缩未启用：--no-compress）")
            else:
                before = estimate_history_tokens(self.loop.history)
                done = await self.loop.context_manager.maybe_compress(
                    self.loop.history, force=True)
                after = estimate_history_tokens(self.loop.history)
                if done:
                    emit(f"  [compact] 已压缩: {before} → {after} tokens")
                else:
                    emit(f"  [compact] 历史太短，无需压缩（当前约 {before} tokens）")
        elif user_input == "/export" or user_input.startswith("/export "):
            parts = user_input.split(maxsplit=1)
            try:
                if len(parts) > 1:
                    out = export_session(self.loop.history, Path(parts[1].strip()))
                else:
                    out = export_session(
                        self.loop.history,
                        export_dir=self.workspace_root / ".krelcode" / "exports")
                emit(f"  [export] 已导出: {out}")
            except OSError as exc:
                emit(f"  [export] 导出失败: {exc}")
        elif user_input == "/history":
            for message in self.loop.history:
                content = message.content or ""
                extra = (f" tool_calls={[tc.name for tc in message.tool_calls]}"
                         if message.tool_calls else "")
                emit(f"  [{message.role}]{extra} {content}")
        elif user_input == "/reset":
            self.loop.reset()
            clear_tool_outputs = True
            emit("（历史已清空）")
        else:
            emit(f"  （未知命令: {user_input.split()[0]}，/help 查看可用命令）")

        return CommandResult(
            handled=True,
            output=tuple(output),
            run_prompt=run_prompt,
            clear_tool_outputs=clear_tool_outputs,
        )


def _format_skill_details(skills, name: str) -> str:
    """格式化 Skill 元数据、资源和完整说明。"""
    try:
        skill = skills.get(name)
        if skill is None:
            raise ValueError(f"未知 Skill: {name}")
        lines = [
            f"  [skill] {skill.name}",
            f"  描述: {skill.description}",
            f"  来源: {skill.source}",
            f"  位置: {skill.location}",
        ]
        if skill.argument_hint:
            lines.append(f"  参数: {skill.argument_hint}")
        if skill.allowed_tools:
            lines.append(f"  建议工具: {', '.join(skill.allowed_tools)}")
        if skill.version:
            lines.append(f"  版本: {skill.version}")
        if skill.license:
            lines.append(f"  许可证: {skill.license}")
        resources = skills.list_resources(name)
        if resources:
            lines.append("  资源:")
            for resource in resources[:50]:
                lines.append(
                    f"    {resource.kind:4s} {resource.size:>8d}  {resource.path}")
            if len(resources) > 50:
                lines.append(f"    … 另有 {len(resources) - 50} 个资源")
        lines.append("\n" + skills.load(name, for_model=False))
        return "\n".join(lines)
    except Exception as exc:
        return f"  [skill] {exc}"
