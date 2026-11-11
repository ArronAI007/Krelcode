# Krelcode

一个逐步构建的 coding agent：pi 式分层解耦 + 类型化事件总线，asyncio，零第三方运行时依赖。

- 架构设计与技术决策：[DESIGN.md](DESIGN.md)
- 主流 coding agent 架构参考：[ARCHITECTURE.md](ARCHITECTURE.md)
- 分步路线图与进度：[PLAN.md](PLAN.md)

## 快速开始

```bash
# 运行全部测试（218 个，全离线）
/opt/anaconda3/bin/pytest tests/ -v

# 离线体验 mock 模式：第一轮会演示完整 tool-call 循环（LLM→审批→执行get_time→回填→总结）
PYTHONPATH=src /opt/anaconda3/bin/python -m krelcode --mock

# 真实模式：统一 Provider 机制，--provider openai / anthropic
export OPENAI_API_KEY=sk-...          # 或 ANTHROPIC_API_KEY
PYTHONPATH=src /opt/anaconda3/bin/python -m krelcode --provider openai -m "现在纽约几点？"
PYTHONPATH=src /opt/anaconda3/bin/python -m krelcode --provider anthropic --model claude-sonnet-4-5

# 也可以用 TOML 配置（./krelcode.toml 或 ~/.krelcode/config.toml）
#   default_provider = "anthropic"
#   [providers.anthropic]
#   model = "claude-sonnet-4-5"
#   api_key_env = "ANTHROPIC_API_KEY"
```

REPL 内置命令：`/help`、`/history`、`/tools`、`/session`、`/sessions`（列出全部会话）、`/resume <n|latest|path>`（会话内切换）、`/reset`、`/exit`。

### MCP 远程工具

在配置文件里声明 MCP 服务器（stdio），远程工具自动以 `<server>__<tool>` 命名注册，与本地工具无差别执行：

```toml
[mcp_servers.fetch]
command = "uvx"
args = ["mcp-server-fetch"]
env = { SOME_KEY = "value" }
timeout = 30
```

`--no-mcp` 可跳过启动；单个服务器启动失败只警告不阻断。

### repo_map

内置 `repo_map` 工具：Python 用 ast 精确提取类/函数/方法+行号（零依赖，不引 tree-sitter），JS/TS 用正则，20K 字符预算，LLM 可按行号接 `read_file` 精读。

当前内置工具（8 个）：`get_time`、`read_file`、`write_file`、`edit_file`、`run_command`、`grep`、`glob`、`repo_map`；另加 MCP 远程工具。
文件与命令操作限定在工作区内（相对路径基于 `--workspace`），自动跳过 `.git`/`node_modules` 等目录。

安全策略（默认开启）：只读命令静默放行；执行代码/删文件/网络访问等 ASK 级操作弹 `[y]是/[n]否/[a]始终允许` 审批，"始终允许"按程序名持久化到 `<workspace>/.krelcode/allow_rules.json`；`sudo`、`rm -rf /`、`curl|sh` 等 DENY 级无条件拒绝。`--auto-approve` 自动批准 ASK 级，`--no-sandbox` 完全关闭。

上下文管理：CJK 感知 token 估算；历史超过 `--context-tokens`（默认 10 万）自动把中段摘要压缩（`--no-compress` 关闭）；超长工具输出全文落盘 `.krelcode/tool_outputs/` 并只在上下文留引用。

体验：LLM 回复逐字流式渲染；edit_file 的 diff 红绿高亮（尊重 `NO_COLOR`）；会话自动录制为 JSONL（`<workspace>/.krelcode/sessions/`，审计事件流 + 历史快照），`--resume` 恢复最近会话继续聊（`--resume <path>` 指定文件，`--no-session` 关闭）。

启动时自动注入工作区上下文（目录树 + git 分支/状态 + `AGENTS.md` 项目规则），`--no-context` 可关闭；同批工具调用并行执行，同一调用连续失败 3 次判定卡死自动中止（`--repeat-failure-limit` 可调）。

## 模块结构（依赖严格单向）

```
core/    wire-free 规范消息·事件总线·错误（零依赖内核）
llm/     统一 Provider 机制：transport+SSE / OpenAI·Anthropic·Mock 适配器 / 工厂+配置
agent/   AgentLoop 主循环（只依赖端口，不认识具体工具/模型）
tools/   Tool 端口 + Registry + builtin/（每工具一个文件）
safety/  ApprovalPolicy 端口（Step 6 加沙箱）
ui/      REPL（事件总线旁路订阅者）
app.py   组装根：唯一知道所有具体类的地方，负责依赖注入
```
