"""krelcode init：生成起步配置与项目规则模板（不覆盖已存在文件，--force 强制）。"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

CONFIG_TEMPLATE = '''# Krelcode 配置（默认按 user → project → local → managed 合并）
# user: ~/.krelcode/config.toml | project: ./krelcode.toml
# local: ./krelcode.local.toml（本机覆盖，默认不提交）| managed: KRELCODE_MANAGED_CONFIG
# 完整说明见 README.md 与 DESIGN.md

# default_provider = "openai"        # openai / anthropic / gemini / mock

# ---- 生产级 Base Prompt 与模型 profile ----
# [prompt]
# profile = "auto"                   # auto / default / claude / deepseek / openai / gemini
# auto 先按模型名、再按 provider 匹配；profile 只追加指导，不能覆盖安全 section
#
# [prompt.authoring]                 # 可选 Prompt 设计/评审契约（默认关闭）
# enabled = true                     # 启用 prompt_authoring:contract、prompt_lab 与 content-light proposal store
# 契约覆盖目标分析、Instruction/Evidence 分离、stable/dynamic、冲突检测、
# 工具权限设计、可机器验证用例、canonical Candidate Prompt Schema
# 以及 temporary PromptPlan 六项隔离评估（通过后仅进入人工审核）

[providers.openai]
# model = "gpt-4o-mini"
# base_url = "https://api.openai.com/v1"   # 任意 OpenAI 兼容端点
api_key_env = "OPENAI_API_KEY"
# prompt_cache = "auto"                   # auto / on / off / implicit / explicit
# generation = { temperature = 0.3 }       # 生成参数透传（top_p、max_tokens…）

# ---- DeepSeek 示例（OpenAI 兼容端点）：取消注释并置顶 default_provider = "openai" ----
# model = "deepseek-flash"                 # 快速模型；deepseek-v4-pro 更强（默认思考模式）
# base_url = "https://api.deepseek.com/v1"
# api_key_env = "DEEPSEEK_API_KEY"
# generation = { stream_options = { include_usage = true } }   # 流式回传 usage，/cost 才有数据

[providers.anthropic]
# model = "claude-sonnet-4-5"
api_key_env = "ANTHROPIC_API_KEY"
# prompt_cache = "auto"    # 能力协商为 explicit cache_control（true 等价 on）
# thinking_budget = 10000  # 扩展思考预算（tokens，>=1024；缺省不启用）

[providers.gemini]
# model = "gemini-2.5-flash"
api_key_env = "GEMINI_API_KEY"
# prompt_cache = "auto"    # Gemini 2.5+ 协商为 server-managed implicit cache

# ---- 内置 Agent 的逻辑模型档位映射（可选）----
# Claude Code 兼容内置 Agent 使用 sonnet/haiku 档位。父 provider 为 anthropic
# 时自动映射；DeepSeek/OpenAI 等环境默认继承父模型。下面的显式映射优先级最高。
# [agent_models]
# sonnet = "anthropic/claude-sonnet-4-5"
# haiku = "anthropic/claude-haiku-4-5"
# opus = "anthropic/claude-opus-4-1"

# ---- TUI ----
# [ui]
# diff_review = true
# mouse = false           # 默认保留原生复制和滚轮；true 启用面板/工具行点击
# mode = "fullscreen"     # fullscreen（Alt Screen 视口）/ native（终端 scrollback）

# ---- 运行时 Sandbox（命令 / Skill 脚本子进程）----
# [sandbox]
# backend = "auto"        # auto / static / seatbelt(macOS) / bubblewrap(Linux)
# network = false         # 默认断网；需要依赖下载时临时启用 --sandbox-network
# user_install = true     # 允许安装/链接到 ~/.local/bin 等用户目录（禁止 sudo/系统目录）
# install_network = true  # 仅对 pip/uv/npm install、git clone 等安装命令放行网络
# user_install_paths = [".hermes"]  # 额外的用户级安装目录；必须位于真实 HOME 下

# ---- 持久化 daemon（后台 Job / 子代理）----
# [daemon]
# enabled = "auto"        # off / auto / on；auto 在首次 job_start/spawn_agent 时启动
# idle_timeout = 1800     # 无活动任务后退出秒数；0 表示常驻。运行中任务不会因 idle 被杀
# daemon 状态位于 .krelcode/daemon/；token 文件权限 0600，客户端断连不影响任务。

# ---- Auto Memory（稳定、可复用、经过验证的项目知识）----
# [memory]
# mode = "suggest"        # off / suggest / auto；默认只生成候选供审核
# suggest_threshold = 0.55
# auto_threshold = 0.80

# ---- Agent Skills（SKILL.md 指令包）----
# [skills]
# enabled = true                    # false 等价 --no-skills
# paths = ["./team-skills"]        # 额外目录；相对路径基于工作区

# ---- 本地目录插件（plugin.toml；仅可信代码）----
# [plugins]
# enabled = true                    # false 等价 --no-plugins
# paths = ["./team-plugins"]       # 额外目录；相对路径基于工作区
# trust_store = "~/.krelcode/plugin-trust.json"

# [plugins.trust]
# mode = "prefer"                  # off / prefer / signed / strict
# allow_local_unsigned = true      # signed 模式下允许本地开发插件
# require_signature_for_install = false
# enforce_dependencies = true

# ---- 用户/项目 Markdown slash commands ----
# [commands]
# paths = ["./team-commands"]      # 额外目录；相对路径基于工作区
# 还会自动发现 ~/.krelcode/commands、.krelcode/commands、.commands

# ---- 工作区上下文预算（Context v3；legacy 仅保留一个版本用于回退）----
# [context]
# engine = "v3"                    # v3（默认，安全边界+来源归因）| legacy
# max_tokens = 6000                # 规则 + git + 目录树总预算
# include_rules = true
# include_git = true
# include_tree = true
# tree_max_depth = 3
# tree_max_entries = 150
# rules_max_chars = 8000
# include_directory_rules = true     # 按当前请求涉及的路径加载祖先目录规则
# rules_imports = true               # 展开独立行 @相对路径
# rules_import_max_depth = 5
# rules_import_max_files = 32

# ---- MCP 服务器（stdio 或 Streamable HTTP）----
# [mcp_servers.fetch]
# command = "uvx"
# args = ["mcp-server-fetch"]
#
# [mcp_servers.remote]
# url = "http://localhost:3000/mcp"        # 默认 streamable-http
# transport = "sse"                        # 旧规范时显式指定
# listener_retries = 3                     # GET 监听流断线重连次数（0=不重连）
# listener_retry_delay = 1.0               # 基础退避秒数（指数增长）
# headers = { Authorization = "Bearer ${MCP_TOKEN}" }
#
# [mcp_servers.remote.oauth]               # 可选；先用 /mcp auth <server> 交互授权
# access_token = "${MCP_ACCESS_TOKEN}"     # 静态 token 可选
# refresh_token = "${MCP_REFRESH_TOKEN}"
# token_url = "https://auth.example.com/token"
# authorization_url = "https://auth.example.com/authorize"
# client_id = "krelcode"
# client_secret = "${MCP_CLIENT_SECRET}"
# scopes = ["mcp.read"]
# resource = "https://mcp.example.com/mcp"
# redirect_port = 8765

# ---- P2：语言服务器 / Output Styles / Status Line / Web ----
# [lsp]
# enabled = false                   # 按需启动配置的 LSP server
# startup_timeout_ms = 10000
# request_timeout_ms = 5000
# max_message_bytes = 16777216
# [lsp.servers.python]
# command = "pyright-langserver"
# args = ["--stdio"]
# extensions = [".py"]
# language_id = "python"
#
# [output_styles]                   # 内置 default/explanatory/learning + 自定义 Markdown
# enabled = true
# active = ""                       # 空/default=默认；或填写样式名称
# paths = ["./output-styles"]       # 额外文件/目录
#
# [status_line]                     # 安全模板，不执行 shell
# enabled = true
# template = "{provider}/{model} · {permission} · {context} · {branch}"
# max_length = 160
#
# [web]                             # WebFetch 的 SSRF/size/time 边界
# enabled = true
# timeout = 15
# max_bytes = 2097152
# max_redirects = 5
# max_chars = 20000
# allowed_ports = [80, 443]
#
# [web_search]
# enabled = true
# provider = "brave"                # 或通过自定义 SearchBackend 注入
# api_key_env = "BRAVE_SEARCH_API_KEY"
# endpoint = "https://api.search.brave.com/res/v1/web/search"
# max_results = 5

# ---- 匿名遥测（默认关闭；必须用 krelcode telemetry enable 显式同意）----
# [telemetry]
# flush_interval = 60               # remote 模式的后台批量间隔秒数
# batch_size = 50
# max_events = 10000               # 本地队列有界保留上限
# max_bytes = 10000000
# retention_days = 30
# sample_rate = 1.0                 # 0..1；按用户会话采样
# 遥测从不采集 Prompt、代码、路径、工具参数/输出、用户名、hostname 或 API key。

# ---- 事件钩子 ----
# [[hooks]]
# on = "file_edited"    # 生命周期触发器，完整列表见 CONFIG.md
# command = "ruff format {path}"
# tools = ["write_file", "edit_*"]   # 可选工具 glob 匹配
# timeout = 30
# enabled = true
#
# [[hooks]]
# on = "pre_tool_use"
# type = "http"
# url = "${HOOK_ENDPOINT}"
# headers = { Authorization = "Bearer ${HOOK_TOKEN}" }
#
# [[hooks]]
# on = "user_prompt_submit"
# type = "prompt"
# prompt = "Review this request against project policy: {prompt}"
'''

AGENTS_TEMPLATE = '''# 项目规则

> 本文件会随工作区上下文自动注入 agent 的 system prompt（分层规则：
> 全局 ~/.krelcode/AGENTS.md -> 项目根 AGENTS.md -> 目标目录 AGENTS.md）。
> 子目录规则只在当前任务涉及该目录时加载；独立一行 `@相对路径` 可导入其他
> Markdown 规则文件。请写"agent 必须知道"的约定。

## 代码规范
- （示例）注释与提交信息使用中文

## 测试要求
- （示例）改动 src/ 后运行 pytest tests/ 并保证通过

## 禁止事项
- （示例）不要修改 migrations/ 下的历史文件
'''

TEMPLATES: Tuple[Tuple[str, str], ...] = (
    ("krelcode.toml", CONFIG_TEMPLATE),
    ("AGENTS.md", AGENTS_TEMPLATE),
)


def scaffold_workspace(root: Path, force: bool = False) -> List[str]:
    """在工作区生成模板文件；返回每个文件的动作描述。"""
    root = Path(root)
    actions: List[str] = []
    root.mkdir(parents=True, exist_ok=True)
    for name, template in TEMPLATES:
        path = root / name
        existed = path.exists()
        if existed and not force:
            actions.append(f"跳过（已存在，--force 可覆盖）: {path}")
            continue
        path.write_text(template, encoding="utf-8")
        actions.append(f"{'已覆盖' if existed else '已创建'}: {path}")
    return actions
