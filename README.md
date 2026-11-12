# Krelcode

一个逐步构建的 coding agent：pi 式分层解耦 + 类型化事件总线，asyncio，零第三方运行时依赖。

- 架构设计与技术决策：[DESIGN.md](DESIGN.md)
- 主流 coding agent 架构参考：[ARCHITECTURE.md](ARCHITECTURE.md)
- 分步路线图与进度：[PLAN.md](PLAN.md)
- 构建回顾（统计/决策时间线/文件导览/验收指引）：[BUILD.md](BUILD.md)
- **完整配置参考**（CLI/REPL/TOML/env/安全速查/事件清单）：[CONFIG.md](CONFIG.md)

配好 API key 后可跑真模型自动验收：`python scripts/real_acceptance.py`（7 阶段，详见 BUILD.md §5）。

## 安装

```bash
pip install .          # 或 pip install -e . 开发模式；之后 krelcode 命令全局可用
krelcode --version
```

## 起步

```bash
cd your-project
krelcode init          # 生成 krelcode.toml 与 AGENTS.md 模板（不覆盖已有文件，--force 强制）
export OPENAI_API_KEY=sk-...   # 或 ANTHROPIC_API_KEY / GEMINI_API_KEY
krelcode doctor        # 环境自检：Python/key/配置/git/工作区/MCP，✓⚠✗ 报告（有 ✗ 时退出码 1）
krelcode               # 进入 REPL
```

零第三方运行时依赖，Python ≥ 3.11（开发测试仅需 pytest）。

## 快速开始（源码方式）

```bash
# 运行全部测试（453 个，全离线*）
# *两个真实 HTTP 集成测试（SSE/Streamable）需本地端口，沙箱环境自动跳过
/opt/anaconda3/bin/pytest tests/ -v

# 离线体验 mock 模式：第一轮会演示完整 tool-call 循环（LLM→审批→执行get_time→回填→总结）
PYTHONPATH=src /opt/anaconda3/bin/python -m krelcode --mock

# DeepSeek（OpenAI 兼容端点）：
export DEEPSEEK_API_KEY=sk-...
PYTHONPATH=src /opt/anaconda3/bin/python -m krelcode --provider openai \
  --base-url https://api.deepseek.com/v1 --model deepseek-chat
# 建议写进 krelcode.toml（krelcode init 生成的模板里有现成注释示例），之后直接运行 krelcode

# 真实模式：统一 Provider 机制，--provider openai / anthropic / gemini
export OPENAI_API_KEY=sk-...          # 或 ANTHROPIC_API_KEY / GEMINI_API_KEY
PYTHONPATH=src /opt/anaconda3/bin/python -m krelcode --provider openai -m "现在纽约几点？"
PYTHONPATH=src /opt/anaconda3/bin/python -m krelcode --provider anthropic --model claude-sonnet-4-5

# 也可以用 TOML 配置（./krelcode.toml 或 ~/.krelcode/config.toml）
#   default_provider = "anthropic"
#   [providers.anthropic]
#   model = "claude-sonnet-4-5"
#   api_key_env = "ANTHROPIC_API_KEY"
```

REPL 内置命令：`/help`、`/history`、`/tools`、`/session`、`/sessions`、`/resume <n|latest|path>`、`/mcp`（状态）、`/mcp reload`（热重载）、`/agents`（子代理状态）、`/compact`（立即压缩历史）、`/undo`（回滚上一轮文件改动）、`/undo list`、`/cost`（token 用量 + 估算成本 $）、`/model [provider/model]`（运行时热切换模型，预算联动）、`/allow`（管理命令审批白名单：add/remove 程序名，持久化到工作区）、`/context`（上下文统计）、`/context refresh`（重采工作区上下文）、`/export [路径]`（会话导出 Markdown）、`/stop`（中止运行中任务）、`/reset`、`/exit`。

**Steering**：任务运行中直接打字 = 插话（模型在下一步看到，无需等任务结束）；`/stop` 随时中止（历史自动修复，可立即续聊）。

消息里可用 **@路径** 引用文件（如 `解释 @src/main.py 的作用`），内容随消息自动注入（工作区内、带预算与截断，email 地址不会误触发）。

**图片输入**：`@截图.png` 自动作为多模态附件发送（png/jpeg/webp/gif，单张 ≤5MB、单条 ≤4 张），三家 provider 的 wire 格式各自适配；会话按源路径持久化不膨胀。

### MCP 远程工具

在配置文件里声明 MCP 服务器（stdio），远程工具自动以 `<server>__<tool>` 命名注册，与本地工具无差别执行：

```toml
[mcp_servers.fetch]          # stdio 传输
command = "uvx"
args = ["mcp-server-fetch"]
env = { SOME_KEY = "value" }
timeout = 30

[mcp_servers.remote]         # Streamable HTTP（2025-03-26 规范，url 默认）
url = "http://localhost:3000/mcp"
headers = { Authorization = "Bearer ..." }

[mcp_servers.legacy]         # 旧 HTTP+SSE 规范：显式 transport = "sse"
url = "http://localhost:3001/sse"
transport = "sse"
```

- 按服务器 capabilities 自动注册：远程工具 + `list_resources`/`read_resource` + `list_prompts`/`get_prompt`
- 热重载：REPL 内 `/mcp` 查看状态，`/mcp reload` 重读配置（增/删/改 diff 同步）；崩溃的服务器自动重启并重注册工具
- `--no-mcp` 跳过启动；单服务器失败只警告不阻断

### repo_map

内置 `repo_map` 工具：Python 用 ast 精确提取类/函数/方法+行号（零依赖，不引 tree-sitter），JS/TS 用正则，20K 字符预算，LLM 可按行号接 `read_file` 精读。

当前内置工具（14 个）：`get_time`、`read_file`、`write_file`、`edit_file`、`run_command`、`grep`、`glob`、`repo_map`、`find_refs`（ast 级符号引用查找）、`check_syntax`（批量语法检查），以及子代理工具 `spawn_agent`、`wait_agent`、`send_to_agent`、`list_agents`；另加 MCP 远程工具。

### 代码评审

```bash
krelcode review                  # 评审工作区未提交变更
krelcode review --staged         # 只评审已暂存变更
krelcode review --focus "安全" --output-format json   # CI 集成
```

评审模式只给模型**只读工具**（可查上下文、绝无写/执行能力），输出以「结论: 可合入 / 建议修改后合入 / 需要修改」收尾。

### Headless / CI 模式

```bash
krelcode --provider openai -m "修复 tests/ 下失败的测试" --output-format json --auto-approve --no-session
# stdout 输出单行 JSON: {"ok": true, "reply": ..., "usage": {...}, "tools_used": [...], "denied": 0, "session": null}
```

`--token-budget N` 可限制单轮 token 用量（超限优雅中止）；`--retries N`（默认 2）对 429/5xx/网络错误自动指数退避重试；启动提示走 stderr，stdout 保持纯净可管道。

### Hooks（事件钩子）

在配置文件里声明，典型用途是编辑后自动 formatter/linter：

```toml
[[hooks]]
on = "file_edited"          # file_edited / tool_completed / tool_failed / run_finished
command = "ruff format {path}"
timeout = 30

[[hooks]]
on = "run_finished"
command = "osascript -e 'display notification \"任务完成\"'"
```

hooks 只能由用户在配置文件定义（`krelcode.toml` 与 `.krelcode/` 已列入写保护，agent 无法自我注入）；模板变量值经 shell-quote，钩子失败/超时不影响主循环。

### git 自动提交（可选）

`--auto-commit`：每轮对话结束若工作区有变更，自动 `git add -A && commit -m "krelcode: <你的输入>"`（Aider 风格）。`.krelcode/` 运行时目录自动写入 `.git/info/exclude`（仅本地生效）。与 `/undo` 互补：checkpoint 管文件级回滚，auto-commit 管历史留痕。

### 检查点与回滚

每轮对话自动为 write_file/edit_file 的目标文件做修改前快照（`<workspace>/.krelcode/checkpoints/`）。改坏了？`/undo` 一键恢复上一轮：改过的还原、本轮新建的删除；跨重启依然可用。`--no-checkpoint` 关闭。（注：run_command 的文件修改不在追踪范围。）

### 子代理 / 后台任务

LLM 可用 `spawn_agent` 把自包含的子任务委派给后台子代理（独立上下文、并行执行），`wait_agent` 收取报告（支持逗号分隔多 id 一次等多个），`send_to_agent` 续聊追加任务——运行中默认排队、`interrupt=true` 打断当前任务立即处理（自动消毒历史防协议违规）；子代理不能再派生子代理（防递归）。REPL `/agents` 查看状态；子代理事件写入会话 JSONL 审计流。
文件与命令操作限定在工作区内（相对路径基于 `--workspace`），自动跳过 `.git`/`node_modules` 等目录。

安全策略（默认开启）：只读命令与常规文件写入静默放行，但 `.git/`、`.env*`、密钥文件、审批白名单自身等**受保护路径**的写入需要审批；执行代码/删文件/网络访问等 ASK 级操作弹 `[y]是/[n]否/[a]始终允许` 审批，"始终允许"按程序名持久化到 `<workspace>/.krelcode/allow_rules.json`；`sudo`、`rm -rf /`、`curl|sh` 等 DENY 级无条件拒绝。`--auto-approve` 自动批准 ASK 级，`--no-sandbox` 完全关闭。

上下文管理（两道防线）：CJK 感知 token 估算；超过 `--context-tokens`（默认按模型上下文窗口×75% 自适应，未知模型 10 万）先**驱逐旧工具输出**（免费、保配对结构，只留最近 6 条），仍超限再把中段交给 LLM 摘要压缩（`--no-compress` 关闭，`/compact` 手动触发）；超长工具输出全文落盘 `.krelcode/tool_outputs/` 并只在上下文留引用。

体验：LLM 回复逐字流式渲染；reasoning 模型（DeepSeek-R1/Qwen 思考模式/Claude thinking）的思考过程以 `[thinking]` 暗色流展示（统一 ThinkingDelta 事件，思考不进对话历史）；edit_file 的 diff 红绿高亮（尊重 `NO_COLOR`）；会话自动录制为 JSONL（`<workspace>/.krelcode/sessions/`，审计事件流 + 历史快照），`--resume` 恢复最近会话继续聊（`--resume <path>` 指定文件，`--no-session` 关闭）。

启动时自动注入工作区上下文（目录树 + git 分支/状态 + 分层规则：全局 `~/.krelcode/AGENTS.md` + 项目 `AGENTS.md`/`KRELCODE.md`），`--no-context` 可关闭，长会话中 `/context refresh` 可重采；同批工具调用并行执行，同一调用连续失败 3 次判定卡死自动中止（`--repeat-failure-limit` 可调）。

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
