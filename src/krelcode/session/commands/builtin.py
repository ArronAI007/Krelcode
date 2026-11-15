"""Krelcode 内置 slash command 目录。"""

from __future__ import annotations

from .registry import CommandRegistry
from .types import CommandKind, CommandSpec


def builtin_command_specs() -> tuple[CommandSpec, ...]:
    ui = CommandKind.UI_COMMAND
    control = CommandKind.CONTROL
    return (
        CommandSpec(
            "help", "显示本帮助",
            help="显示本帮助"),
        CommandSpec(
            "history", "查看当前对话历史",
            help="查看当前对话历史"),
        CommandSpec(
            "thinking", "展开最近一次思考", kind=ui,
            available_during_run=True,
            help="展开最近一次思考（保留 Markdown 多行）"),
        CommandSpec(
            "tool-output", "查看或展开工具输出", kind=ui,
            argument_hint="[编号|last]", available_during_run=True,
            help="列出工具输出；/tool-output <编号|last> 展开灰色面板"),
        CommandSpec(
            "queue", "查看或管理下一条消息队列", kind=ui,
            argument_hint="[delete|move|clear ...]",
            available_during_run=True,
            help=("查看下一条消息队列；/queue delete <n> 删除、\n"
                  "/queue move <n> <m> 调整顺序、/queue clear 清空")),
        CommandSpec(
            "tools", "列出已注册工具",
            help="列出已注册工具"),
        CommandSpec(
            "session", "显示当前会话文件",
            help="显示当前会话文件路径"),
        CommandSpec(
            "sessions", "列出工作区会话",
            help="列出工作区的全部会话"),
        CommandSpec(
            "resume", "恢复指定会话", argument_hint="<n|latest|path>",
            help="切换到第 n 个会话（/sessions 里的序号；latest=最新）"),
        CommandSpec(
            "mcp", "查看或重载 MCP", argument_hint="[reload]",
            help="查看 MCP 服务器状态；/mcp reload 重读配置并热重启"),
        CommandSpec(
            "agents", "查看子代理状态",
            help="列出子代理（后台任务）状态"),
        CommandSpec(
            "skills", "查看或重载 Skills", argument_hint="[reload|show <name>]",
            help="列出 Skill；/skills reload 热重载；/skills show <name> 查看详情"),
        CommandSpec(
            "skill", "调用指定 Skill", argument_hint="<name> [参数]",
            help="用户主动加载并执行一个 Skill"),
        CommandSpec(
            "undo", "回滚上一轮文件改动", argument_hint="[list]",
            help="回滚上一轮对话的文件改动（检查点恢复；/undo list 查看栈）"),
        CommandSpec(
            "cost", "查看 token 用量与成本",
            help="显示 token 用量（本轮 + 会话累计 + 估算成本）"),
        CommandSpec(
            "model", "查看或切换模型", argument_hint="[provider/]model",
            help=("查看/热切换模型（\"gpt-4o\" 沿用当前 provider，\n"
                  "\"anthropic/claude-sonnet-4-5\" 跨 provider）")),
        CommandSpec(
            "allow", "管理命令审批白名单", argument_hint="[add|remove <程序名>]",
            help=("查看审批白名单；/allow add <程序名> 添加显式程序级规则、\n"
                  "/allow remove <程序名> 移除（持久化到工作区；审批中的\n"
                  "“始终允许此命令”只保存当前精确命令）")),
        CommandSpec(
            "context", "查看或刷新上下文", argument_hint="[refresh]",
            help="显示上下文统计；/context refresh 重新采集工作区上下文"),
        CommandSpec(
            "export", "导出当前会话", argument_hint="[路径]",
            help="把当前会话导出为 Markdown（缺省存 .krelcode/exports/）"),
        CommandSpec(
            "compact", "立即压缩历史",
            help="立即压缩对话历史（LLM 摘要，不必等预算超限）"),
        CommandSpec(
            "stop", "中止正在运行的任务", kind=control,
            available_during_run=True,
            help="中止正在运行的任务（历史自动修复）"),
        CommandSpec(
            "reset", "清空对话历史",
            help="清空对话历史"),
        CommandSpec(
            "exit", "退出 Krelcode", kind=control,
            available_during_run=True,
            help="退出（Ctrl-D / Ctrl-C 同样有效）"),
    )


def builtin_command_registry() -> CommandRegistry:
    return CommandRegistry(builtin_command_specs())


def render_help(registry: CommandRegistry) -> str:
    lines = ["可用命令："]
    specs = registry.list(include_hidden=False)
    width = max(20, min(max((len(spec.usage) for spec in specs), default=0) + 2, 32))
    for spec in specs:
        details = spec.help_text.splitlines() or [""]
        lines.append(f"  {spec.usage:<{width}}{details[0]}")
        for continuation in details[1:]:
            lines.append(f"  {'':<{width}}{continuation}")
    lines.extend((
        "任务运行中：直接输入文字 = 插话（steering）；/thinking 展开思考；",
        "/tool-output <编号> 展开工具输出；/stop 中止。",
        "输入 / 会实时显示匹配命令，Tab 可补全唯一匹配；↑/↓ 调取历史，Ctrl-R 增量搜索。",
        "Alt+Enter 排队下一条消息；/queue 可查看、删除、移动或清空队列。",
        "其他任何输入都会作为消息发给 agent；消息里可用 @路径 引用文件。",
    ))
    return "\n".join(lines)
