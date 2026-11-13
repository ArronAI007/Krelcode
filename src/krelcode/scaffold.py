"""krelcode init：生成起步配置与项目规则模板（不覆盖已存在文件，--force 强制）。"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

CONFIG_TEMPLATE = '''# Krelcode 配置（krelcode --config 或放在工作区根 / ~/.krelcode/config.toml）
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

# ---- Agent Skills（SKILL.md 指令包）----
# [skills]
# enabled = true                    # false 等价 --no-skills
# paths = ["./team-skills"]        # 额外目录；相对路径基于工作区

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

# ---- 事件钩子 ----
# [[hooks]]
# on = "file_edited"    # file_edited / tool_completed / tool_failed / run_finished
# command = "ruff format {path}"
# timeout = 30
'''

AGENTS_TEMPLATE = '''# 项目规则

> 本文件会随工作区上下文自动注入 agent 的 system prompt（分层规则：
> 全局 ~/.krelcode/AGENTS.md + 本项目文件）。请写"agent 必须知道"的约定。

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
