# Krelcode 配置参考（CONFIG.md）

全部 CLI 旗标、REPL 命令、TOML 配置、环境变量、工作区状态目录与事件清单。
本文档有测试守护：每个 CLI 旗标都必须在此出现（tests/test_config_reference.py）。

## 1. CLI 用法

```
krelcode [子命令] [选项]
```

### 子命令（缺省进入 REPL 对话）

| 子命令 | 说明 | 专属旗标 |
|--------|------|----------|
| `doctor` | 环境自检（Python/key/配置/git/工作区/Sandbox/MCP 命令），✓⚠✗ 报告；有 ✗ 退出码 1 | — |
| `init` | 生成 `krelcode.toml` + `AGENTS.md` 模板（不覆盖已有文件） | `--force` |
| `review` | 评审当前 git 变更（只读工具集，天然安全） | `--staged`（只评审暂存区）、`--focus "重点"` |

### 通用旗标

| 旗标 | 默认 | 说明 |
|------|------|------|
| `--provider` | 配置/env/openai | openai / anthropic / gemini / mock |
| `--mock` | — | 等价 `--provider mock`（含演示脚本），无需 API key |
| `--model` | 配置/env/provider 默认 | 模型名 |
| `--base-url` | provider 默认 | 任意兼容端点（DeepSeek/Kimi/Ollama 等） |
| `--config` | 自动探测 | TOML 配置路径 |
| `--workspace` | `.` | 工作区根（所有路径约束与状态目录的锚点） |
| `-m`, `--message` | — | 单次提问模式，执行完退出 |
| `--output-format` | text | `text` 普通输出；`json` 单行结果（配合 `-m`）；`stream-json` JSONL 事件流（配合 `-m` 或 stream-json stdin） |
| `--input-format` | text | `text` 普通 REPL/`-m`；`stream-json` 从 stdin 逐行读取 user/control 消息（SDK/管道） |
| `--max-steps` | 64 | 单轮任务最大 LLM 迭代步数；达到上限时本轮安全中止，可显式调高 |
| `--repeat-failure-limit` | 3 | 同签名调用连续失败 N 次判定卡死中止 |
| `--token-budget` | 不限 | 单轮 token 预算，超限优雅中止 |
| `--retries` | 2 | LLM 瞬态失败（429/5xx/网络）重试次数，尊重 Retry-After，0 关闭 |
| `--context-tokens` | 窗口×75% | 压缩预算；未知模型回退 100000 |
| `--no-compress` | — | 关闭上下文压缩（驱逐+摘要） |
| `--no-context` | — | 跳过启动时工作区上下文注入 |
| `--no-skills` | — | 关闭 Agent Skills（不发现、不注册 Skill 工具） |
| `--skills-dir <路径>` | — | 追加 Skill 根目录，可重复；也支持 TOML `[skills].paths` |
| `--no-plugins` | — | 关闭本地目录插件发现与插件工具 |
| `--plugins-dir <路径>` | — | 追加插件根目录，可重复；也支持 TOML `[plugins].paths` |
| `--auto-approve` | — | ASK 级操作自动批准（DENY 级仍拒绝） |
| `--permission-mode default\|read-only\|auto-approve\|plan` | 配置/`default` | 工具权限模式；`read-only`/`plan` 只暴露只读工具 |
| `--sandbox-backend auto\|static\|seatbelt\|bubblewrap` | auto | 运行时隔离后端；显式后端不可用则启动失败 |
| `--sandbox-network` | — | 允许 Sandbox 子进程访问网络（默认关闭） |
| `--no-sandbox` | — | 同时关闭审批策略与运行时 Sandbox（AllowAll，自担风险） |
| `--no-mcp` | — | 不启动配置中的 MCP 服务器 |
| `--resume` | — | 恢复会话：裸旗=最新，`--resume <path>`=指定 |
| `--no-session` | — | 关闭会话 JSONL 录制 |
| `--no-checkpoint` | — | 关闭文件检查点及 compact 前 history checkpoint；同时禁用 `/undo`、`/rewind` 的对应恢复 |
| `--auto-commit` | — | 每轮有变更自动 git 提交 |
| `--version` | — | 打印版本退出 |

## 2. REPL 命令

| 命令 | 说明 |
|------|------|
| `/help` | 帮助 |
| `/history` | 当前对话历史 |
| `/thinking` | 展开最近一次思考（正文默认折叠） |
| `/plan` · `/plan <任务>` | 查看最近计划 · 进入只读 Plan 模式并生成待确认计划 |
| `/plan approve` · `/plan cancel` | 恢复进入 Plan 前的权限模式并自动执行 · 取消待确认计划 |
| `/permissions` | 当前权限模式、session/workspace 规则与优先级 |
| `/permissions mode <mode>` | 运行时切换 `default / plan / read-only / auto-approve` |
| `/permissions add <decision> <scope> <kind> <pattern>` | 添加 `allow/ask/deny` × `session/workspace` 规则 |
| `/permissions remove <rule-id>` | 按 ID 删除规则 |
| `/tool-output [编号\|last]` | 列出工具输出（默认折叠）；按编号或 `last` 在灰色背景面板中展开 |
| `/queue [delete <n>\|move <n> <m>\|clear]` | 查看或管理 `Alt+Enter` 排队的下一条消息 |
| `/tools` | 已注册工具（含 MCP 远程与子代理工具） |
| `/model [provider/]model` | 查看/热切换模型（预算联动，失败不破坏现状） |
| `/cost` | token 用量（本轮+累计）与估算成本 $ |
| `/context` | 上下文统计、Working Context、Tool Microcompact、Structured Compact generation/来源/校验摘要、最近 checkpoint、Fragment 来源与 cache 边界 |
| `/context sources` | 查看已注册 ContextSource、最近装配 layer 顺序、来源 token、cache 属性与有界错误诊断 |
| `/context rules` | 查看 user/project/directory 规则入口、@import 来源、字符数与越界/循环/敏感文件问题 |
| `/context fragments [source]` | 列出全部或指定来源的 Fragment、启停状态、group、tokens 与 cache 属性 |
| `/context enable\|disable\|toggle <id\|source>` | 手动启停单个 Fragment 或整个来源；精确 id 设置优先于 source 设置 |
| `/context reset` | 清除当前会话的全部手动启停设置 |
| `/context cache` | 显示稳定前缀字符/token 数、稳定/动态 Fragment 列表与 SHA-256 哈希 |
| `/context refresh` | 重采目录树/git/规则文件，原子替换 workspace Fragment 并重建 system prompt |
| `/memory` · `/memory list` | 列出 user/project/session 三层记忆及 ID、类型和内容 |
| `/memory scopes` | 显示各作用域条数与存储路径 |
| `/memory add <user\|project\|session> <text>` | 写入指定作用域；user 跨工作区，session 跟随当前会话 |
| `/memory remove <id>` | 删除指定记忆并重建 memory Fragment |
| `/compact` | 立即执行工具微压缩+Structured Compact v2（不等预算），完成后显示 generation、来源节省量、checkpoint 与实际边界 |
| `/compact preview [--auto]` | 预览工具微压缩与 compact 的 head/compact/keep 边界、高低水位、来源节省量、条数、预计 tokens 与待摘要消息；不改 history。`--auto` 仅在超预算时预览 |
| `/sessions` · `/sessions search <关键词>` | 列出/搜索会话标题与用户输入 |
| `/resume <n\|latest\|path>` | 恢复会话并建立新分支（不覆盖源会话） |
| `/fork [--turn <n>] [名称]` | 从当前历史或第 n 轮结束点创建并切换独立会话分支（不恢复工作区） |
| `/rewind` · `/rewind list` | 列出当前进程内可回退的 turn |
| `/rewind <n>` | 回到第 n 轮开始前，联合恢复 history、compact 前完整现场、文件 checkpoint 与 plan/审批/权限状态 |
| `/rewind fork <n> [名称]` | 从第 n 轮结束后的 history + plan 分叉，工作区文件保持当前状态 |
| `/rename <名称>` | 为当前会话设置可搜索标题 |
| `/delete <n\|path> [--force]` | 二次确认后归档非活动会话到 `sessions/.archive` |
| `/session` | 当前会话文件、标题、父会话与模型元数据 |
| `/export [路径]` | 导出 Markdown；`.json` 路径输出可重新 `/resume` 的 native 快照 |
| `/undo` · `/undo list` | 回滚上一轮文件改动 · 查看检查点栈 |
| `/agents` · `/agents list` | 子代理状态（定义、轮数、排队深度、feed 游标） |
| `/agents feed <id> [after] [--limit N]` | 按稳定 sequence 读取子代理实时事件增量 |
| `/agents pause <id>` · `/agents resume <id>` | 在 step 安全边界暂停/恢复，保留 Task 与上下文 |
| `/agents kill <id>` | 终止整个子代理（区别于 interrupt） |
| `/agents definitions` | 列出用户自定义 Agent Definition 与加载问题 |
| `/agents sidechains` · `/agents sidechain <id>` | 列出或读取持久化 sidechain JSONL |
| `/jobs` · `/jobs list` | 列出当前进程管理的后台 Shell 任务、PID、状态、exit code、sequence 与日志 |
| `/jobs output <id> [after] [--wait 秒]` | 按游标读取任务增量输出；wait 最多 30 秒 |
| `/jobs kill <id>` | 终止任务及其整个进程组 |
| `/skills [关键词]` | 列出或搜索 Skill |
| `/skills reload` | 重读全部 Skill 目录并刷新 system prompt 索引 |
| `/skills show <name>` | 查看元数据、资源与完整 `SKILL.md` |
| `/skill <name> [参数]` | 用户主动加载并执行 Skill；参数替换 `$ARGUMENTS` |
| `/plugins [reload\|show <name>]` | 查看插件状态/问题 · 热重载 · 查看单个 manifest |
| `/mcp` · `/mcp reload` | MCP 服务器状态 · 重读配置热重载 |
| `/allow [add\|remove <程序名>]` | 旧版程序白名单兼容入口（持久化） |
| `/stop` | 中止运行中任务（历史自动修复） |
| `/reset` | 清空历史与当前 turn 时间线 |
| `/exit` | 退出（Ctrl-D 同） |

Fragment 启停是**当前会话运行时状态**，不写入 TOML；重启后恢复默认全开。
内置来源为 `core`（不可关闭的基础 system prompt）、`skills`（可用 Skill 索引）以及
`workspace:rules|git|tree`。cacheable Fragment 会形成连续的稳定前缀；只有真正位于
第一个动态 Fragment 之前的稳定内容会报告为 cache boundary。

规则层级为 `user (~/.krelcode/AGENTS.md) → project (根目录 AGENTS.md/KRELCODE.md) →
directory (当前轮目标路径的祖先目录规则)`。每个目录优先使用 `AGENTS.md`，其次
`KRELCODE.md`；子目录规则按需加载，非递归扫描。规则文件中的独立一行
`@相对路径` 会递归展开，代码围栏中的示例不会展开；导入仅允许当前作用域内普通
UTF-8 文件，并受深度、文件数、字符预算、循环/重复与敏感路径保护。

**权限快捷键**：`Shift-Tab` 按 `default → plan → read-only → auto-approve`
循环切换，状态栏实时显示当前模式；切换会同步更新当前 turn 的策略快照和下一轮
工具池。`/permissions mode` 也可在任务运行中切换。

**运行中（steering）**：`Enter`=插话（当前 turn 下一步看到，`[用户插话]` 前缀注入）；
`Alt+Enter`=排队为下一条独立消息，当前 turn 自然完成或中止后自动发送；
`Esc`/`Ctrl-C` 中止当前任务；`/stop` 中止；`/exit` 停止并退出。
空闲时 `Ctrl-C` 清空当前输入但**不退出**，`Ctrl-D` 或 `/exit` 退出。
审批请求由 REPL 前台通过异步 broker 接管；`y`/`n`/`a` 不会被写入用户问题历史。
**消息语法**：`@路径` 注入文本文件（8K/5 个预算）或附加图片（png/jpeg/webp/gif，5MB/4 张）。
`!命令` 直接运行 Shell（不进入模型 history，但复用审批/Sandbox）；`#内容` 保存 project
记忆，`#user: 内容` / `#session: 内容` 分别写入用户级和会话级作用域；裸 `#` 打开
Memory 面板。`Ctrl-O` 聚焦面板，面板内用方向键切换/选择，Enter/Esc 返回。

## 3. TOML 配置

探测顺序：`--config` 显式 > `./krelcode.toml` > `~/.krelcode/config.toml`。

```toml
default_provider = "openai"          # openai / anthropic / gemini / mock

[providers.openai]
model = "gpt-4o-mini"                # 覆盖 KRELCODE_MODEL
base_url = "https://api.openai.com/v1"
api_key = "sk-..."                   # 不建议明文；优先 api_key_env
api_key_env = "OPENAI_API_KEY"
timeout = 120
generation = { temperature = 0.3 }   # 生成参数透传（三家通用）

[providers.anthropic]
model = "claude-sonnet-4-5"
api_key_env = "ANTHROPIC_API_KEY"
max_tokens = 4096
prompt_cache = true                  # system/tools/messages 三段 ephemeral 缓存（默认开）
thinking_budget = 10000              # 扩展思考（≥1024；缺省不启用）

[providers.gemini]
model = "gemini-2.5-flash"
api_key_env = "GEMINI_API_KEY"       # GOOGLE_API_KEY 兜底

[sandbox]
backend = "auto"                     # auto / static / seatbelt / bubblewrap
network = false                      # 默认禁止子进程联网；CLI --sandbox-network 可开启

[permissions]
mode = "default"                     # default / read-only / auto-approve / plan

[features]
enabled = []                         # 启用的功能开关名；工具池可按开关过滤

[tools]
disabled = []                        # 本轮工具池永久排除的工具名

[skills]
enabled = true                       # false 等价 --no-skills
paths = ["./team-skills", "/opt/shared-skills"]  # 额外目录，可相对工作区

[plugins]                            # 轻量目录插件（本地可信代码）
enabled = true                       # false 等价 --no-plugins
paths = ["./team-plugins", "/opt/shared-plugins"] # 额外目录，可相对工作区

[context]                            # 启动时工作区上下文（Context v2）
max_tokens = 6000                    # rules + git + tree 的统一 token 预算
include_rules = true                 # ~/.krelcode/AGENTS.md + 项目根规则
include_git = true                   # 分支与 porcelain 状态
include_tree = true                  # 剪枝后的目录树
tree_max_depth = 3
tree_max_entries = 150
rules_max_chars = 8000               # 入口 + @import 展开后的统一字符预算
include_directory_rules = true       # 按当前轮目标路径加载祖先目录规则
rules_imports = true                 # 展开独立行 @相对路径
rules_import_max_depth = 5
rules_import_max_files = 32

[mcp_servers.fetch]                  # stdio 传输
command = "uvx"
args = ["mcp-server-fetch"]
env = { KEY = "value" }
timeout = 30

[mcp_servers.remote]                 # HTTP 传输
url = "http://localhost:3000/mcp"    # 默认 streamable-http（2025-03-26）
transport = "sse"                    # 显式回退旧 HTTP+SSE（2024-11-05）
headers = { Authorization = "Bearer ..." }

[[hooks]]
on = "file_edited"                   # 触发器见下文
command = "ruff format {path}"       # 模板变量自动 shell-quote
tools = ["write_file", "edit_*"]     # 可选 glob；只影响工具型触发器
timeout = 30
enabled = true
```

配置优先级：**显式 CLI 旗标 > TOML > 环境变量 > 内置默认**。
未知键进 `extra` 透传（如 anthropic 的 prompt_cache/thinking_budget）。

### Hooks 协议（Hooks v2）

支持的触发器：

- 会话/输入：`session_start`、`user_input`、`user_prompt_submit`、`session_end`
- 工具：`pre_tool_use`、`post_tool_use`、`tool_completed`、`tool_failed`、`file_edited`
- 上下文：`pre_compact`、`post_compact`
- 子代理：`subagent_started`、`subagent_feed`、`subagent_state`、`subagent_finished`
- 后台 Job：`shell_job_started`、`shell_job_finished`（实时 output 走事件/协议与 job 日志，不逐块无限写会话审计）
- 轮次：`run_finished`

`user_input` 在输入预处理层执行，保留给附件展开前的原始提示；
`user_prompt_submit` 在查询引擎入口执行，因此 REPL、headless 与子代理都会触发。

每次执行都会把 JSON payload 从 stdin 传给 Hook：

```json
{
  "event": "pre_tool_use",
  "workspace": "/path/to/project",
  "timestamp": 1760000000,
  "data": {"tool": "write_file", "path": "src/a.py"},
  "tool": {
    "name": "write_file",
    "arguments": {"path": "src/a.py", "content": "..."},
    "id": "call_1"
  }
}
```

同时提供 `KRELCODE_HOOK_EVENT`、`KRELCODE_WORKSPACE`、`KRELCODE_TOOL`
环境变量。Hook 子进程使用净化环境，不继承 API key/token/proxy/SSH agent。
Hook 命令来自用户配置，视为显式信任，不进入工具审批链；stdout 日志、注入
上下文、用户提示与执行审计记录仍统一经过 `SecretRedactor` 脱敏。

模板变量包括 `{workspace}`、`{prompt}`、`{tool}`、`{path}`、`{arguments}`、
`{reply}`、`{error}`、`{is_error}`、`{before_tokens}`、`{after_tokens}` 等；
未提供或未知的变量保持原样。所有替换值只展开一次并经过 `shlex.quote`，防止
LLM 参数或用户输入注入 shell。

stdout 可返回普通文本或一行 JSON：

```json
{
  "additional_context": "注入到本轮 system context 的内容",
  "system_message": "显示给用户的提示",
  "decision": "block",
  "reason": "禁止修改该文件"
}
```

`additional_context` 也接受字符串数组；`systemMessage`、`additionalContext`
两种 camelCase 写法兼容。上下文型 Hook 的纯文本 stdout 会作为 legacy 上下文。
`pre_tool_use` 返回 `decision=block|deny`，或以 Unix 退出码 `2` 退出（stderr
作为原因）时，工具不会执行。除显式阻断外，Hook 非零退出、超时或执行失败均
fail-open，不中断主循环。`pre_compact`/`post_compact` 注入的上下文只用于本轮
后续模型调用，不写入持久历史；`/context` 可查看运行中 Hook 上下文段数。

## 4. 环境变量

| 变量 | 用途 |
|------|------|
| `KRELCODE_PROVIDER` | 默认 provider |
| `KRELCODE_MODEL` | 默认模型（三家 provider 共用） |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | OpenAI 兼容 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | Anthropic |
| `GEMINI_API_KEY`（`GOOGLE_API_KEY` 兜底）/ `GEMINI_BASE_URL` | Gemini |
| `KRELCODE_HISTORY_FILE` | REPL 输入历史 JSONL 路径（默认 `~/.krelcode/history.jsonl`） |
| `NO_COLOR` | 设置后禁用全部 ANSI 颜色 |

## 5. 工作区状态目录（`.krelcode/`，对 LLM 写保护）

```
.krelcode/
├── sessions/          会话 JSONL（session_start/session_meta + 审计事件流 + history）
│   └── .archive/      /delete 的可恢复归档
├── checkpoints/       文件快照（/undo、/rewind 数据源；meta 带 session/turn 身份）
├── compact-checkpoints/  compact 前完整脱敏 history + Structured Compact 摘要（/rewind 数据源）
├── tool_outputs/      超长工具输出全文（历史里留截断+路径引用）
├── memory/            # project 记忆 entries.jsonl + sessions/<session-id>.jsonl
├── jobs/              后台 Shell 任务脱敏日志（每任务 output.log，默认 10 MiB 上限）
├── sidechains/        子代理 JSONL（按 manager run-id 分目录；定义/feed/状态/history/结果）
├── exports/           /export 缺省输出目录
├── sandbox/            隔离 HOME/TMP/cache（命令与 Skill 脚本使用）
└── allow_rules.json   审批规则（旧版 allow_programs/allow_commands + rules[]，仅用户可改）
```

全局：`~/.krelcode/config.toml`（配置）、`~/.krelcode/AGENTS.md`（全局规则，
与项目级 `AGENTS.md`/`KRELCODE.md` 分层注入，项目规则在后=更具体者优先）、
`~/.krelcode/memory/entries.jsonl`（跨工作区 user 记忆，权限与隐私由本机用户边界保护）。

Memory 作用域：`user`（跨工作区）→ `project`（当前工作区 `.krelcode/memory/entries.jsonl`）
→ `session`（`.krelcode/memory/sessions/<session-id>.jsonl`）。每层独立限长、限条数、
脱敏与原子删除；读取时以文件所在目录决定 scope，旧 project 记录缺少新字段时自动按
`note/manual` 兼容。fork/resume 会重新绑定当前会话，no-session 模式下的 session 记忆仅在进程内存在。

Agent Definition 发现目录（后者覆盖前者同名项）：`~/.krelcode/agents/*.md`、
`<workspace>/.krelcode/agents/*.md`、`<workspace>/.agents/agents/*.md`。格式：

```markdown
---
name: reviewer
description: 只读代码审查
allowed-tools: [read_file, grep, glob, repo_map]
fork-context: true     # 可选；默认继承父对话
isolated: false        # 可选；true=独立 git worktree
---
你是严格的代码审查员，只报告可复现的问题并给出文件路径与行号。
```

`allowed-tools`（也接受 `tools`）只能缩小默认工具池；未知工具会让该定义在 spawn 时
fail-closed。Definition frontmatter/正文属于用户显式配置，但不会绕过审批、Sandbox、
只读模式或子代理防递归边界。单个坏定义只记录 issue，不阻断主 Agent。

Skill 发现目录（后者覆盖前者同名项）：`~/.agents/skills`、`~/.krelcode/skills`、
`<workspace>/.agents/skills`、`<workspace>/.krelcode/skills`，再追加
`[skills].paths` / `--skills-dir`。每个 Skill 是包含 `SKILL.md` 的目录。

Plugin 发现目录（后者覆盖前者同名插件）：`~/.krelcode/plugins`、
`<workspace>/.krelcode/plugins`，再追加 `[plugins].paths` / `--plugins-dir`。
每个插件是包含 `plugin.toml` 的目录；插件代码按本地可信代码处理，单插件加载失败
只记录 issue，不影响主 Agent。插件工具统一命名空间为
`plugin__<plugin-name>__<tool-name>`。

### 运行时权限与规则示例

```text
/permissions
/permissions mode plan
/permissions add allow session path "docs/**"
/permissions add ask workspace command "git push"
/permissions add deny workspace command_program rm
/permissions remove session-allow-1
```

`kind` 支持：

| kind | pattern 语义 |
|------|---------------|
| `path` | 解析 symlink 后的工作区相对路径 glob，例如 `docs/**`、`src/*.py` |
| `command` | 规范化后的完整直接命令，例如 `git push origin main` |
| `command_program` | 单一直接命令的首程序名，例如 `pytest`；包装/复合命令不匹配 |
| `tool` | 工具名 glob，例如 `write_file`、`mcp__catalog__*` |

workspace 规则文件新增字段示例：

```json
{
  "allow_programs": [],
  "allow_commands": [],
  "rules": [
    {
      "id": "workspace-allow-1",
      "decision": "allow",
      "scope": "workspace",
      "kind": "path",
      "pattern": "docs/**"
    }
  ]
}
```

规则文件本身位于受保护目录 `.krelcode/`，Agent 写它仍需用户审批；模型没有
修改权限规则的专用工具，规则管理入口只属于用户命令。

## 6. 安全策略速查（审批策略 + 运行时 Sandbox，默认开启）

| 操作 | 处置 |
|------|------|
| 只读工具（get_time/read_file/grep/glob/find_refs/repo_map/check_syntax/ask_user/update_plan，以及 list/load/read Skill） | 放行；显式 `deny/ask` 路径或工具规则优先；`ask_user` 无交互前台时工具结果 fail-closed |
| write_file/edit_file（普通路径） | 放行（工作区约束兜底） |
| write/edit 受保护路径：`.git/`、`.krelcode/`、`krelcode.toml`、`.env*`、`*.pem/*.key/*.p12/*.pfx`、`id_rsa*`、`secrets.*`、`credentials*`、`.npmrc/.netrc/.pgpass/.htpasswd` | ASK（非交互拒绝） |
| run_command 只读命令（ls/cat/grep/git status·log·diff…） | 放行 |
| run_command 执行代码/装依赖/删文件/git 写操作/网络/未知命令/重定向 | ASK |
| run_skill_script（Skill 内 bundled 脚本） | ASK；参数数组不经过 shell，子进程走运行时 Sandbox |
| run_command：sudo/su、dd/mkfs/diskutil、shutdown/reboot、rm 灾难目标、curl\|sh、fork 炸弹 | **DENY（不可越过，--auto-approve 也拒）** |
| 统一规则优先级 | `deny > ask > allow`；`deny` 不能靠 auto-approve 越过，显式 `ask` 也不会被 auto-approve 跳过 |
| 规则作用域 | `session` 仅当前进程；`workspace` 持久化到 `.krelcode/allow_rules.json`；`path` 必须是工作区相对 glob 且拒绝 `..` |
| ASK 处理链 | 统一规则 → 旧版 allow 规则 → --auto-approve/运行时模式 → 交互 [y/n/a] → 都没有则拒绝（fail-closed） |
| 文件/命令路径约束 | resolve 后限定工作区（含 symlink 逃逸防护）；Skill 根目录仅额外只读 |
| 子进程环境 | 剥离 API key/token/password/proxy/SSH agent/动态加载注入变量，HOME/TMP 重定向到 `.krelcode/sandbox/` |
| OS 隔离后端 | macOS `seatbelt` 或 Linux `bubblewrap`：工作区可写、系统运行时只读、Home 不可读、网络默认关闭 |
| `static` 降级模式 | 跨平台硬性审计：越界/敏感路径、`sh -c`/`eval` 递归、命令替换、已知网络命令；**不能抵御解释器代码中的动态绕过** |

`auto` 会实际探测 `sandbox-exec`/`bwrap` 是否可运行；探测失败自动降级为
`static` 并在启动时告警。需要强隔离时应显式检查启动日志中的
`[sandbox] backend=...`，Linux 可安装 bubblewrap，macOS 使用系统 Seatbelt。
MCP stdio 子进程默认同样剥离宿主密钥，但 `[mcp_servers.*].env` 中显式配置的
值视为用户信任输入而保留。

## 7. 事件清单（EventBus，hooks/UI/审计的消费面）

`RunStarted` · `StepStarted` · `QueryPhaseChanged` · `LLMThinkingDelta` · `LLMStreamDelta` ·
`LLMResponseReceived`(usage) · `ToolCallStarted` · `ToolProgress` · `ToolOutputDelta` ·
`ToolCallCompleted`(is_error) · `PlanUpdated` · `UserInputRequested` ·
`ApprovalRequested` · `PermissionDenied` · `ContextAssembled`(usage snapshot) ·
`ContextCompressed`(before/after、strategy、summary source/generation、高/低水位、来源节省量、checkpoint ID) ·
`HookTriggered` · `SubagentSpawned` · `SubagentFeedUpdated` · `SubagentStateChanged` · `SubagentFinished` ·
`ShellJobStarted` · `ShellJobOutput` · `ShellJobFinished` · `UserSteered` ·
`RunFinished` · `ErrorOccurred`

JSONL 审计记录类型：`user_input` `tool_call` `tool_result` `permission_denied`
`hook_triggered` `context_assembled` `context_compressed`(含 Structured Compact 元数据) `subagent_spawned` `subagent_feed` `subagent_state`
`subagent_finished`
`shell_job_started` `shell_job_finished` `user_steered`
`run_finished` `history`(快照)。

## 7.1 后台 Shell Job 安全边界

`job_start` 与 `run_command` 使用同一 Sandbox 审计、命令风险分级和审批规则；
后台进程使用独立 session/process group、净化环境和 `stdin=DEVNULL`，不会抢占终端输入。
`job_output`/`job_list` 是本地只读工具；`job_kill` 只能操作当前 manager 持有的 job id。
read-only/plan 模式禁止 start/kill，但仍允许读取已有任务输出。命令、实时输出、日志和完成
preview 全部经过 SecretRedactor；跨 read 边界的 secret 也会通过前缀尾巴暂存后统一脱敏。
Job 不随 turn abort，进程退出时由 manager 回收进程组；应用退出由 `AgentRuntime.close()`
统一 `shutdown()`。重启 Krelcode 不会接管旧 OS 进程，也不会静默恢复工作区文件。

## 7.2 交互式子代理安全边界

Pause 不取消 Task，而是在 `AgentLoop` 每个 step 开始前等待 gate；当前模型调用或
工具执行完成到安全点后才真正停住。Kill 是独立语义：取消整个 Task、状态进入
`killed`，之后拒绝 `send_to_agent`；Interrupt 仍只取消当前轮以便立即处理新指令。
Resume 沿用原 history、工具上下文和队列，不重建子代理。

Definition 只能从默认工具池中做 allowlist 收缩，不能授予父 Agent 没有的能力。
Feed 和 sidechain 在进入内存、父 EventBus、stream-json、recorder 或磁盘前统一脱敏；
流式文本会暂存可能构成 secret 前缀的尾部，避免密钥被 chunk 拆分后绕过替换；
feed 有界（默认 2,000 条 / 单条 24,000 字符），sidechain 保存 start/feed/state/message/
history，可重启后审计，但当前进程不会自动接管或复活旧子代理 Task。Sidechain 不自动加入
父 Agent 上下文，父代理必须通过 `wait_agent` / `get_agent_feed` 主动读取。

## 8. JSON 输出模式（`-m --output-format json`）

```json
{"ok": true, "reply": "...", "usage": {"calls": 2, "input": 0, "output": 0, "total": 0},
 "tools_used": ["get_time"], "denied": 0, "cost_usd": null, "session": null}
```
失败：`{"ok": false, "error": "LLMHTTPError: ..."}`，退出码 1。启动提示走 stderr。
`krelcode review --output-format json` 同构（`review` 字段替代 `reply`）。

## 9. Stream JSON v1（`--input-format/--output-format stream-json`）

stdout 每行是一个 UTF-8 JSON 对象，启动日志与诊断只写 stderr。首条固定为：

```json
{"type":"system","subtype":"init","protocol":"krelcode.stream-json",
 "protocol_version":"1","session_id":null,"cwd":"/repo","provider":"openai",
 "model":"deepseek-flash","mode":"stream-json","permission_mode":"default",
 "tools":["read_file"],"mcp_servers":[]}
```

stdin 支持两种 user 形式：

```json
{"type":"user","content":"解释 README","request_id":"optional-id"}
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"看图"},
 {"type":"image","source":{"type":"base64","media_type":"image/png","data":"..."}}]}}
```

控制消息为 `{"type":"control","subtype":"interrupt"}` 与
`{"type":"control","subtype":"end"}`。`interrupt` 只取消当前查询；`end` 是优雅关闭，
会先处理此前已到达的 user 消息。每次查询恰好产生一个 `result`：

```json
{"type":"result","subtype":"success","is_error":false,"result":"最终回答",
 "turn_id":"turn-1","sequence":42,"session_id":"...","request_id":"optional-id",
 "usage":{"calls":2,"input":100,"output":20,"total":120,"cache_read":0,"cache_creation":0},
 "session_usage":{},"cost_usd":0.0001,"session_cost_usd":0.0001,
 "num_turns":2,"duration_ms":800}
```

`subtype` 可为 `success` / `cancelled` / `error`。其他消息类型包括
`system/turn_started`、`step/started`、`query/phase_changed`、
`assistant/text_delta|thinking_delta|message`、`tool/started|progress|output_delta|completed`、
`plan/updated`（含 `items/statuses/active_index/explanation`）、
`input/requested|resolved`（含 `request_id/options/allow_free_text`；resolved 只表示是否已回答，
不回传答案正文）、`permission/requested|denied`、`context/assembled|compressed`、
`hook/triggered`、`subagent/started|feed|state|finished`、`job/started|output_delta|finished`、
`user/steered`、`error/occurred`；job 事件独立于 turn，因此不带 turn_id，但带
`session_id`；`job/output_delta` 的 `sequence` 可直接作为 `after_sequence` 游标；
`context/assembled` 的 `snapshot` 只含 token 分项、预算、cache boundary 与 hash，不含 Prompt 正文；
`context/compressed` 只含策略、摘要来源/generation、水位、来源节省量与 checkpoint ID，不含摘要正文；
`subagent/feed` 的 agent cursor 是 `agent_sequence`（外层 `sequence` 仍是 QueryEvent 顺序）；
最后以 `system/session_end` 结束。turn 内事件都带 `turn_id`、单调 `sequence` 和
`session_id`，user 对应的全部事件及 result 还带同一 `request_id`；后台 job 是跨 turn
长生命周期事件，使用自己的 `job_id` 与游标。

Stream JSON 当前不接收远端 `input/response`；headless 查询中的 `ask_user` 会立即
fail-closed。需要人工澄清的会话应使用 REPL，或由 SDK 拆成多次普通 user 请求。

## 10. Python SDK

```python
import asyncio
from krelcode.sdk import ImageInput, KrelcodeClient, KrelcodeOptions

async def main():
    options = KrelcodeOptions(
        workspace=".",
        provider="openai",
        no_session=True,
        env={"DEEPSEEK_API_KEY": "从宿主秘密管理器注入"},
    )
    async with KrelcodeClient.from_options(options) as client:
        messages = [message async for message in client.query("解释 README")]
        follow_up = await client.run("继续检查测试")

asyncio.run(main())
```

SDK 使用 `python -m krelcode` 子进程，因此与 CLI 的 Provider、工具池、MCP、
Hooks、权限模式和 Sandbox 完全一致。常用选项与 CLI 同名；`extra_args` 只用于
新增旗标，禁止覆盖 `-m/--message/--input-format/--output-format`。API key 不应
写入 `extra_args` 或 user 消息，应继续使用 `[providers.*].api_key_env`。
`stderr_tail()` 和进程异常中的诊断文本也会做二次密钥脱敏。
