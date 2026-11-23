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
            "plan", "查看当前执行计划", kind=ui,
            available_during_run=True,
            help="显示模型最近一次 update_plan 提交的结构化计划"),
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
            "commands", "查看命令来源、信任与模板契约",
            argument_hint="[show <name>]",
            help=("查看全部 slash command 的来源、信任级别、参数策略、"
                  "工具 allowlist、reminder 声明与模板 Hash；"
                  "不显示命令正文或用户参数。")),
        CommandSpec(
            "session", "显示当前会话文件",
            help="显示当前会话文件路径"),
        CommandSpec(
            "sessions", "列出或搜索工作区会话", argument_hint="[search <关键词>]",
            help="列出工作区的全部会话；/sessions search <关键词> 搜索标题与用户输入"),
        CommandSpec(
            "resume", "恢复指定会话", argument_hint="<n|latest|path>",
            help="恢复第 n 个会话（/sessions 里的序号；latest=最新），并建立新分支"),
        CommandSpec(
            "fork", "从当前历史或指定 turn 创建会话分支",
            argument_hint="[--turn <n>] [名称]",
            help=("复制当前上下文到新会话并切换；/fork --turn <n> [名称] "
                  "从第 n 轮结束后的 history + plan 分叉（不恢复工作区文件）")),
        CommandSpec(
            "rewind", "回到任意 turn 开始前并联合恢复",
            argument_hint="[n|list|fork <n> [名称]]",
            help=("查看可回退 turn；/rewind <n> 回到第 n 轮开始前并恢复 "
                  "history + 文件 checkpoint + plan；/rewind fork <n> [名称] "
                  "从第 n 轮结束后的历史分叉")),
        CommandSpec(
            "rename", "重命名当前会话", argument_hint="<名称>",
            help="为当前会话写入可搜索标题"),
        CommandSpec(
            "delete", "归档一个非活动会话", argument_hint="<n|path> [--force]",
            help="删除前要求二次确认；文件移入 sessions/.archive 可手动恢复"),
        CommandSpec(
            "mcp", "查看、授权或重载 MCP",
            argument_hint="[reload|conformance [server]|auth <server>]",
            help=("查看 MCP 状态与 Resource/Template 统计；/mcp reload 重读配置并热重启；"
                  "/mcp conformance [server] 输出安全的一致性矩阵；"
                  "/mcp auth <server> 执行 OAuth + PKCE 授权")),
        CommandSpec(
            "agents", "查看和控制子代理",
            argument_hint="[list|feed|pause|resume|kill|definitions|sidechains]",
            available_during_run=True,
            help=("查看子代理；/agents feed <id> [after] 读取事件流；\n"
                  "/agents pause|resume|kill <id>；/agents definitions 查看内置与自定义定义")),
        CommandSpec(
            "jobs", "查看或控制后台 Shell 任务",
            argument_hint="[list|output <id> [after]|kill <id>]",
            available_during_run=True,
            help=("列出后台进程；/jobs output <id> [after] [--wait 秒] 读取增量；\n"
                  "/jobs kill <id> 终止整个进程组")),
        CommandSpec(
            "skills", "查看或重载 Skills", argument_hint="[reload|show <name>]",
            help="列出 Skill；/skills reload 热重载；/skills show <name> 查看详情"),
        CommandSpec(
            "plugins", "查看、验证或安装插件",
            argument_hint=("[reload|show|verify|deps|registry|trust] ..."),
            help=("列出插件与依赖/签名状态；/plugins reload 热重载；"
                  "/plugins registry install 从可信 registry 安装签名插件")),
        CommandSpec(
            "lsp", "查看 LSP 客户端状态",
            argument_hint="[status]",
            help="查看配置的语言服务器、按需启动状态与加载问题"),
        CommandSpec(
            "web", "查看 WebFetch/WebSearch 安全配置",
            argument_hint="[status]",
            help="查看网页工具是否启用、网络边界和搜索后端配置（不发起网络请求）"),
        CommandSpec(
            "output-style", "查看、切换或重载 Output Style",
            argument_hint="[list|show <name>|reload|off]",
            help=("查看当前 Output Style；内置 default/explanatory/learning；"
                  "/output-style <name> 切换；"
                  "/output-style list 列出可用样式；"
                  "/output-style show <name> 查看正文；"
                  "/output-style reload 重载内置与 user/project/config 文件；"
                  "/output-style default|off 恢复默认输出。")),
        CommandSpec(
            "statusline", "使用内置 statusline-setup agent 配置状态栏",
            argument_hint="[要求]",
            help=("启动 Claude Code 兼容的 statusline-setup 子代理；"
                  "无参数时生成保守的默认状态栏，也可描述希望展示的字段")),
        CommandSpec(
            "extensions", "查看统一扩展目录与来源",
            argument_hint="[kind] [source]",
            help=("列出插件、工具、Skill、Agent、Command、Hook 与 MCP 的来源；\n"
                  "/extensions plugin|tool|skill|agent|command|hook|mcp")),
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
            "permissions", "切换权限模式并管理会话/工作区规则",
            argument_hint="[mode|add|remove ...]", available_during_run=True,
            help=("查看当前权限模式与规则；/permissions mode <mode> 切换；\n"
                  "/permissions add <allow|ask|deny> <session|workspace> "
                  "<path|command|command_program|tool> <pattern>；\n"
                  "/permissions remove <rule-id>。规则优先级 deny > ask > allow。")),
        CommandSpec(
            "allow", "管理命令审批白名单", argument_hint="[add|remove <程序名>]",
            help=("查看审批白名单；/allow add <程序名> 添加显式程序级规则、\n"
                  "/allow remove <程序名> 移除（持久化到工作区；审批中的\n"
                  "“始终允许此命令”只保存当前精确命令）")),
        CommandSpec(
            "memory", "查看或管理分层记忆",
            argument_hint=(
                "[list [scope]|show|add|forget|pin|search|review|migrate ...]"),
            help=("查看 user/project/session 三层记忆；"
                  "/memory add <text> [--scope <scope>] 写入（兼容旧语法）；"
                  "/memory forget <id> 删除；\n"
                  "/memory pin|unpin <id> 调整检索优先级；"
                  "/memory search <关键词> 搜索；\n"
                  "/memory mode [off|suggest|auto] 设置 Auto Memory；"
                  "/memory review [pending|accepted|rejected|all|accept|reject <id>]；"
                  "/memory migrate [scope] 迁移旧格式并备份。")),
        CommandSpec(
            "context", "查看、启停或刷新上下文槽位",
            argument_hint=("[plan|why|sources|rules|conflicts|authoring|fragments|"
                           "enable|disable|toggle|reset|cache|refresh]"),
            help=("显示上下文统计与 Fragment 来源；/context sources 查看 "
                  "ContextSource 装配诊断；/context rules 查看分层规则与 @import；"
                  "/context attachments 查看动态附件 revision；"
                  "/context plan 查看最近请求计划；/context why <id> 解释来源；"
                  "/context conflicts 查看 Prompt 指令冲突与权威裁决；"
                  "/context authoring 查看可选 PromptAuthoringContract 状态；"
                  "/context ledger 查看统一请求/缓存身份与 cache miss 原因；"
                  "/context reminders 查看本轮 L12 reminder 选择；"
                  "/context fragments [source] 查看来源；\n"
                  "/context enable|disable|toggle <id|source> 手动启停；"
                  "/context reset 清除启停设置；/context cache 查看缓存边界；\n"
                  "/context refresh 重新采集工作区上下文")),
        CommandSpec(
            "prompts", "查看所有模型可见 Prompt 的只读目录",
            argument_hint="[list [kind|source]|show <id>|coverage|hash]",
            help=("查看主 Agent、Tool、Workflow、Subagent、Compact、Review、"
                  "Hook、MCP、Skill、Plugin 和 Custom Command 的 Prompt Surface；"
                  "仅显示来源、Hash、Token、权限与最近渲染状态，不显示 Prompt 正文。")),
        CommandSpec(
            "prompt-release", "查看或人工审批 Prompt 发布提案",
            argument_hint=(
                "[list|status [id]|approve <id> <candidate-hash> [reason]|"
                "reject <id> <candidate-hash> <reason>]"),
            help=("查看 content-light Prompt Release proposal；approve/reject 只能由"
                  "前台人类命令执行，Agent tool 无审批动作。批准只生成本地审核"
                  "产物与 calibration artifact，不修改生产 Prompt、Registry 或配置。")),
        CommandSpec(
            "export", "导出当前会话", argument_hint="[路径]",
            help="导出 Markdown；路径以 .json 结尾时输出可 /resume 的 native 快照"),
        CommandSpec(
            "compact", "立即压缩历史",
            argument_hint="[preview [--auto] | <focus>]",
            help=("立即压缩对话历史（LLM 摘要，不必等预算超限）；\n"
                  "/compact preview 预览 compact 边界与待摘要消息；"
                  "/compact preview --auto 仅在超预算时预览；"
                  "/compact <focus> 带重点生成交接摘要")),
        CommandSpec(
            "security-review", "对当前 git 变更执行只读安全审查",
            argument_hint="[--staged] [--focus <重点>]",
            help=("启动 security-review Workflow，仅暴露本地只读工具；"
                  "返回结构化 JSON 安全报告与风险门禁结论")),
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
        "任务运行中：直接输入文字 = 插话（steering）；/thinking 展开思考；/plan 查看计划；",
        "/tool-output <编号> 展开工具输出；/stop 中止。",
        "输入 / 会实时显示匹配命令，Tab 可补全唯一匹配；↑/↓ 调取历史，Ctrl-R 增量搜索。",
        "Alt+Enter 排队下一条消息；/queue 可查看、删除、移动或清空队列。",
        "其他任何输入都会作为消息发给 agent；消息里可用 @路径 引用文件。",
    ))
    return "\n".join(lines)
