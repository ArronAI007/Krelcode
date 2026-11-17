"""krelcode init：生成起步配置与项目规则模板（不覆盖已存在文件，--force 强制）。"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

CONFIG_TEMPLATE = '''# Krelcode 配置（默认按 user → project → local → managed 合并）
# user: ~/.krelcode/config.toml | project: ./krelcode.toml
# local: ./krelcode.local.toml（本机覆盖，默认不提交）| managed: KRELCODE_MANAGED_CONFIG
# 完整说明见 README.md 与 DESIGN.md

# default_provider = "openai"        # openai / anthropic / gemini / mock

[providers.openai]
# model = "gpt-4o-mini"
# base_url = "https://api.openai.com/v1"   # 任意 OpenAI 兼容端点
api_key_env = "OPENAI_API_KEY"
# generation = { temperature = 0.3 }       # 生成参数透传（top_p、max_tokens…）

# ---- DeepSeek 示例（OpenAI 兼容端点）：取消注释并置顶 default_provider = "openai" ----
# model = "deepseek-flash"                 # 快速模型；deepseek-v4-pro 更强（默认思考模式）
# base_url = "https://api.deepseek.com/v1"
# api_key_env = "DEEPSEEK_API_KEY"
# generation = { stream_options = { include_usage = true } }   # 流式回传 usage，/cost 才有数据

[providers.anthropic]
# model = "claude-sonnet-4-5"
api_key_env = "ANTHROPIC_API_KEY"
# prompt_cache = true      # 提示缓存（默认开）：长会话可省 ~90% 重复输入成本
# thinking_budget = 10000  # 扩展思考预算（tokens，>=1024；缺省不启用）

[providers.gemini]
# model = "gemini-2.5-flash"
api_key_env = "GEMINI_API_KEY"

# ---- 运行时 Sandbox（命令 / Skill 脚本子进程）----
# [sandbox]
# backend = "auto"        # auto / static / seatbelt(macOS) / bubblewrap(Linux)
# network = false         # 默认断网；需要依赖下载时临时启用 --sandbox-network

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
