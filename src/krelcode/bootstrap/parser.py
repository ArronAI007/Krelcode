"""Shared CLI parser owned by the composition layer.

Keeping parser construction under ``bootstrap`` lets daemon composition build
the canonical runtime arguments without importing the process entrypoint.
"""

from __future__ import annotations

import argparse

from .. import __version__
from ..agent.context.compression import DEFAULT_MAX_TOKENS as DEFAULT_CONTEXT_BUDGET
from ..agent.turn.loop import DEFAULT_MAX_STEPS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="krelcode", description="Krelcode coding agent (统一 Provider + tool-calling 循环 + MCP)"
    )
    p.add_argument("command", nargs="?",
                   choices=("doctor", "init", "review", "security-review", "benchmark",
                            "daemon", "jobs", "agents", "enterprise", "audit", "auth",
                            "telemetry"),
                   default=None,
                   help="子命令：doctor=环境自检；init=生成配置/规则模板；"
                        "review=评审当前 git 变更；security-review=只读安全审查；"
                        "benchmark=真实模型 Prompt 行为评测；daemon/jobs/agents="
                        "持久化后台任务与子代理；enterprise/audit/auth=企业策略、审计与 SSO；"
                        "telemetry=匿名遥测状态与显式启停；"
                        "缺省进入对话")
    p.add_argument("action", nargs="?", default=None,
                   help="daemon/jobs/agents 子操作（start/status/stop/list/output/...）")
    p.add_argument("target", nargs="?", default=None,
                   help="子操作目标（job id、agent id、sidechain 等）")
    p.add_argument("--force", action="store_true",
                   help="init 子命令：覆盖已存在的模板文件")
    p.add_argument("--staged", action="store_true",
                   help="review/security-review 子命令：只处理已暂存变更（git diff --cached）")
    p.add_argument("--focus", default=None,
                   help="review/security-review 子命令：附加评审重点")
    p.add_argument(
        "--fail-on", choices=("never", "info", "low", "medium", "high", "critical"),
        default="high",
        help="security-review 子命令：达到该严重度时返回非零退出码（默认 high）")
    p.add_argument("--provider", default=None,
                   help="openai / anthropic / gemini / mock（默认: KRELCODE_PROVIDER 或配置文件 default_provider 或 openai）")
    p.add_argument("--mock", action="store_true", help="等价于 --provider mock（含演示脚本），无需 API key")
    p.add_argument("--model", default=None, help="模型名（默认: 配置文件 / KRELCODE_MODEL / provider 默认）")
    p.add_argument("--base-url", default=None, help="覆盖 provider 的 base_url（任意兼容端点）")
    p.add_argument("--config", default=None, help="TOML 配置文件路径（默认 krelcode.toml 或 ~/.krelcode/config.toml）")
    p.add_argument("--workspace", default=".", help="工作区根目录（默认当前目录）")
    p.add_argument("--audit-output", default=None,
                   help="audit export：导出目标文件路径")
    p.add_argument("--telemetry-output", default=None,
                   help="telemetry export：导出目标文件路径")
    p.add_argument("--telemetry-endpoint", default=None,
                   help="telemetry enable remote：HTTPS 接收端点")
    p.add_argument("--telemetry-sample-rate", type=float, default=None,
                   help="telemetry enable：会话级采样率 0..1")
    p.add_argument("--issuer", default=None,
                   help="auth：临时覆盖 OIDC issuer（默认使用 managed SSO 配置）")
    p.add_argument("--client-id", default=None,
                   help="auth：临时覆盖 OIDC client_id")
    p.add_argument("--redirect-uri", default=None,
                   help="auth：覆盖 OIDC redirect_uri（默认本机随机 loopback）")
    p.add_argument("--token-dir", default=None,
                   help="auth：覆盖私有 token store 目录")
    p.add_argument("--no-open-browser", action="store_true",
                   help="auth login：只打印授权 URL，不自动打开浏览器")
    p.add_argument("--allow-http-issuer", action="store_true",
                   help="auth：允许 HTTP issuer（仅限本地测试）")
    p.add_argument(
        "--max-steps", type=int, default=DEFAULT_MAX_STEPS,
        help=f"单轮任务的最大 LLM 迭代步数（默认 {DEFAULT_MAX_STEPS}）",
    )
    p.add_argument("--repeat-failure-limit", type=int, default=3,
                   help="同一调用连续失败多少次判定卡死并中止（默认 3）")
    p.add_argument("--token-budget", type=int, default=None,
                   help="单轮任务的 token 预算，超限中止（默认不限）")
    p.add_argument("--retries", type=int, default=2,
                   help="LLM 瞬态失败（429/5xx/网络）自动重试次数，指数退避（默认 2，0 关闭）")
    p.add_argument("--no-context", action="store_true",
                   help="跳过启动时的工作区上下文注入（目录树/git/AGENTS.md）")
    p.add_argument("--no-skills", action="store_true",
                   help="禁用 Agent Skills 发现、索引与相关工具")
    p.add_argument("--skills-dir", action="append", default=[],
                   help="额外 skill 根目录（可重复；优先级高于项目/全局目录）")
    p.add_argument("--output-style", default=None,
                   help="当前 Output Style（内置 default/explanatory/learning，或自定义名称；"
                        "覆盖 [output_styles].active）")
    p.add_argument("--output-style-dir", action="append", default=[],
                   help="额外 Output Style 文件或目录（可重复）")
    p.add_argument("--no-output-styles", action="store_true",
                   help="禁用 Output Styles 发现与注入")
    p.add_argument("--no-web", action="store_true",
                   help="禁用内置 WebFetch/WebSearch 与浏览器网络工具")
    p.add_argument("--no-browser", action="store_true",
                   help="禁用浏览器会话工具（不影响 WebFetch/WebSearch）")
    p.add_argument("--no-notebook", action="store_true",
                   help="禁用 Jupyter Notebook inspect/edit/create 工具")
    p.add_argument("--no-database", action="store_true",
                   help="禁用数据库迁移 inspect/validate/create 工具")
    p.add_argument("--no-plugins", action="store_true",
                   help="禁用本地目录插件发现与插件工具")
    p.add_argument("--plugins-dir", action="append", default=[],
                   help="额外插件根目录（可重复；优先级高于项目/全局目录）")
    p.add_argument("--auto-approve", action="store_true",
                   help="ASK 级操作自动批准（信任模式；DENY 级仍然拒绝）")
    p.add_argument("--no-diff-review", action="store_true",
                   help="关闭 REPL 文件写入前的交互式 diff 审阅（仍保留安全审批）")
    daemon_group = p.add_mutually_exclusive_group()
    daemon_group.add_argument(
        "--daemon", dest="daemon_enabled", action="store_true", default=None,
        help="启用持久化 daemon：后台 Job/子代理在 REPL 退出后继续运行")
    daemon_group.add_argument(
        "--no-daemon", dest="daemon_enabled", action="store_false",
        help="禁用 daemon，使用兼容的进程内 Job/SubagentManager")
    p.add_argument("--daemon-idle-timeout", type=float, default=None,
                   help="daemon 无活动任务后的退出秒数（0 表示不自动退出）")
    p.add_argument("--foreground", action="store_true",
                   help="daemon start：前台运行（默认后台启动）")
    p.add_argument("--command", dest="job_command", default=None,
                   help="jobs start：要执行的后台 shell 命令")
    p.add_argument("--task", default=None,
                   help="agents spawn：子代理任务描述")
    p.add_argument("--definition", default=None,
                   help="agents spawn：Agent Definition 名称")
    p.add_argument("--after", type=int, default=0,
                   help="jobs output/agents feed：起始 sequence")
    p.add_argument("--wait", type=float, default=0.0,
                   help="jobs output：等待新输出的秒数")
    p.add_argument("--isolated", action="store_true",
                   help="agents spawn：在独立 git worktree 中运行")
    p.add_argument("--fork-context", action="store_true",
                   help="agents spawn：继承指令与筛选 Memory（不继承完整历史）")
    p.add_argument("--permission-mode",
                   choices=("default", "read-only", "auto-approve", "plan"),
                   default=None,
                   help="工具权限模式（默认: 配置文件 permissions.mode 或 default）")
    p.add_argument("--sandbox-backend", choices=("auto", "static", "seatbelt", "bubblewrap"),
                   default=None, help="运行时 Sandbox 后端（默认 auto；显式后端不可用则启动失败）")
    p.add_argument("--sandbox-network", action="store_true", default=None,
                   help="允许 Sandbox 子进程访问网络（默认关闭）")
    p.add_argument("--no-sandbox", action="store_true",
                   help="关闭审批策略与运行时 Sandbox（AllowAll，仅建议在隔离环境中使用）")
    observability_group = p.add_mutually_exclusive_group()
    observability_group.add_argument(
        "--observability", dest="observability", action="store_true", default=None,
        help="启用本地 Agent 观测事件日志（默认: [observability].enabled 或开启）")
    observability_group.add_argument(
        "--no-observability", dest="observability", action="store_false",
        help="关闭本轮 Agent 观测事件日志")
    webui_group = p.add_mutually_exclusive_group()
    webui_group.add_argument(
        "--webui", dest="webui", action="store_true", default=None,
        help="强制随当前 CLI 启动 loopback Web UI；默认交互式 REPL 自动启动")
    webui_group.add_argument(
        "--no-webui", dest="webui", action="store_false",
        help="关闭随当前 CLI 同步启动的 Web UI")
    tui_mouse_group = p.add_mutually_exclusive_group()
    tui_mouse_group.add_argument(
        "--tui-mouse", dest="tui_mouse", action="store_true", default=None,
        help="启用 TUI 面板与工具行鼠标点击；默认关闭以保留终端文本选择与复制")
    tui_mouse_group.add_argument(
        "--no-tui-mouse", dest="tui_mouse", action="store_false",
        help="关闭 TUI 鼠标上报（默认）")
    p.add_argument(
        "--experiment-profile", default=None,
        help="本轮消融实验 profile JSON；在启动时校验并冻结，不支持 turn 中热改")
    p.add_argument("--memory-mode", choices=("off", "suggest", "auto"), default=None,
                   help="Auto Memory 模式（默认: [memory].mode 或 suggest；"
                        "suggest 仅入队等待 /memory review）")
    p.add_argument("--memory-suggest-threshold", type=float, default=None,
                   help="Auto Memory 候选入队最低置信度（默认 0.55）")
    p.add_argument("--memory-auto-threshold", type=float, default=None,
                   help="auto 模式自动写入最低置信度（默认 0.80）")
    p.add_argument("--context-tokens", type=int, default=None,
                   help="上下文 token 预算，超限自动压缩（默认: 模型上下文窗口×75%%，"
                        f"未知模型回退 {DEFAULT_CONTEXT_BUDGET}）")
    p.add_argument("--no-compress", action="store_true",
                   help="关闭上下文自动压缩")
    p.add_argument("--no-mcp", action="store_true",
                   help="不启动配置文件中的 MCP 服务器")
    p.add_argument("--resume", nargs="?", const="latest", default=None,
                   help="恢复会话：--resume 取最新，--resume <path> 取指定文件")
    p.add_argument("--no-session", action="store_true",
                   help="关闭会话 JSONL 录制（默认写入 <workspace>/.krelcode/sessions/）")
    p.add_argument("--no-checkpoint", action="store_true",
                   help="关闭文件检查点（/undo 回滚功能）")
    p.add_argument("--auto-commit", action="store_true",
                   help="每轮结束若有文件变更自动 git 提交（需工作区是 git 仓库）")
    p.add_argument("-m", "--message", default=None, help="单次提问模式：执行完直接退出")
    p.add_argument(
        "--output-format", choices=("text", "json", "stream-json"),
        default="text",
        help="text 普通文本；json 单条结构化结果；stream-json 逐行 JSON 事件流",
    )
    p.add_argument(
        "--input-format", choices=("text", "stream-json"), default="text",
        help="text 普通 REPL/-m；stream-json 从 stdin 逐行读取用户消息（SDK/管道）",
    )
    p.add_argument("--version", action="version", version=f"krelcode {__version__}")
    return p
