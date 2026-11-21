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
| `--config` | 自动探测 | 独占 TOML 配置路径；不参与默认层合并 |
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
| `--output-style <名称>` | — | 覆盖当前 Output Style；也可用 `[output_styles].active` |
| `--output-style-dir <路径>` | — | 追加 Output Style 文件或目录，可重复 |
| `--no-output-styles` | — | 禁用 Output Styles |
| `--no-web` | — | 禁用 WebFetch/WebSearch |
| `--auto-approve` | — | ASK 级操作自动批准（DENY 级仍拒绝） |
| `--permission-mode default\|read-only\|auto-approve\|plan` | 配置/`default` | 工具权限模式；`read-only`/`plan` 只暴露只读工具 |
| `--sandbox-backend auto\|static\|seatbelt\|bubblewrap` | auto | 运行时隔离后端；显式后端不可用则启动失败 |
| `--sandbox-network` | — | 允许 Sandbox 子进程访问网络（默认关闭） |
| `--no-sandbox` | — | 同时关闭审批策略与运行时 Sandbox（AllowAll，自担风险） |
| `--memory-mode off\|suggest\|auto` | 配置/`suggest` | Auto Memory：关闭；候选等待审核；高置信无冲突候选自动写入 |
| `--memory-suggest-threshold` | `0.55` | 候选进入项目本地审核队列的最低置信度 |
| `--memory-auto-threshold` | `0.80` | `auto` 模式自动晋级为长期 knowledge 的最低置信度 |
| `--no-mcp` | — | 不启动配置中的 MCP 服务器 |
| `--resume` | — | 恢复会话：裸旗=最新，`--resume <path>`=指定 |
| `--no-session` | — | 关闭会话 JSONL 录制 |
| `--no-checkpoint` | — | 关闭文件检查点及 compact 前 history checkpoint；同时禁用 `/undo`、`/rewind` 的对应恢复 |
| `--auto-commit` | — | 每轮有变更自动 git 提交 |
| `--version` | — | 打印版本退出 |

Live Model Behavior Benchmark 使用独立 CLI：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--live` | — | 启用真实模型行为层；provider/model 来自用户 → project → local → managed 配置 |
| `--live --list` | — | 列出 6 个行为场景，不读取 API key |
| `--trials` | `1` | 每个场景独立 trial 数，限制 1-20 |
| `--trial-timeout` | `300` | 单个 trial 总超时秒数；超时只失败当前 trial |
| `--live-max-steps` | `14` | 每个 Agent turn 的最大模型/工具步数 |
| `--sandbox-backend` | `auto` | `auto/static/seatbelt/bubblewrap` |
| `--include-content` | 关闭 | 在报告中包含最终答案与工具参数；默认仅输出计数、hash 和检查结果 |
| `--tune` | 关闭 | 根据失败 trial 的工具轨迹与 checks 生成人工审核用 Prompt 调优候选 |
| `--tune-output` | — | 将内容轻量的 tuning JSON 写入指定文件；不自动修改任何 Prompt |

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
| `/tools` | 当前动态工具池：core/discoverable/deferred、ToolSearch 已加载状态、来源与排除原因 |
| `/model [provider/]model` | 查看/热切换模型（预算联动，失败不破坏现状） |
| `/cost` | token 用量（本轮+累计）与估算成本 $ |
| `/context` | 最近真实请求的 token 分项、预算、stable/volatile、memory 命中、dropped 来源、cache hash 变化、compact 水位与 checkpoint |
| `/context plan` | 查看最近请求的逐 Fragment source/scope/token、stable/volatile 分布、cache boundary、stable/dynamic/full hash、工具目录 hash、裁剪原因、Memory 命中与上一请求 diff |
| `/context conflicts` | 查看最近 Prompt 的跨层指令冲突、严重度/状态、双方 Fragment、winner、权威裁决、置信度与内容无关 hash |
| `/context why <id>` | 解释 Fragment/source/Memory entry/layer/token-source/omitted item 的归因、位置、哈希、cache/compact 属性与裁剪原因；不输出正文 |
| `/context why conflict:<id>` | 解释单条 Prompt 冲突的 code/status、权威顺序、winner 与 resolution；不输出冲突指令正文 |
| `/context why tool_playbook:<id>` | 查看工具级 Playbook 的 L5/session/cache 属性、工具族、本轮 active tools、定义版本与内容 hash；不输出完整 Prompt 正文 |
| `/context why workflow:<id>` | 查看本轮任务类型、分类置信度、命中信号、L9/turn 属性、Workflow 版本与内容 hash；不输出完整 Prompt 正文 |
| `/context sources` | 查看已注册 ContextSource、最近装配 layer 顺序、来源 token、cache 属性、dropped item 与有界错误诊断 |
| `/context rules` | 查看 user/project/directory 规则入口、@import 来源、字符数与越界/循环/敏感文件问题 |
| `/context attachments` | 查看动态 Context Attachment 的 ID、kind、revision、generation、layer 与 token；不输出附件正文 |
| `/context fragments [source]` | 列出全部或指定来源的 Fragment、启停状态、group、tokens 与 cache 属性 |
| `/context enable\|disable\|toggle <id\|source>` | 手动启停单个 Fragment 或整个来源；精确 id 设置优先于 source 设置 |
| `/context reset` | 清除当前会话的全部手动启停设置 |
| `/context cache` | 显示稳定前缀字符/token 数、稳定/动态 Fragment、stable/dynamic/full hash、工具目录 hash、与上一请求相比的 hash/flag/token 差值及 cache segment 归因 |
| `/context ledger` | 显示统一请求身份、instruction/evidence/tool/attachment/working/compact/memory/history/wire hash、完整请求 hash、cache miss 原因、Resume invalidation 与子代理指令继承状态 |
| `/context refresh` | 重采目录树/git/规则文件；规则变化才更新稳定 Fragment，环境变化只替换对应 snapshot/delta attachment |
| `/memory` · `/memory list [scope]` | 列出全部或指定 user/project/session 作用域记忆及 ID、类型和内容 |
| `/memory scopes` | 显示各作用域条数与存储路径 |
| `/memory show <id>` | 查看作用域、类型、source、时间、tag、token 与完整内容；ID 支持唯一后缀 |
| `/memory add <text> [--scope user\|project\|session]` | 写入指定作用域，缺省 project；兼容 `/memory add <scope> <text>` 旧语法 |
| `/memory remove <id>` · `/memory forget <id>` | 删除指定记忆并重建 memory Fragment；两者语义相同 |
| `/memory pin\|unpin <id>` | 固定/取消固定记忆；固定项在搜索和 Prompt 注入中优先 |
| `/memory search <关键词> [--scope <scope>]` | 在有界作用域内按相关度搜索已审核记忆，不搜索待审核候选 |
| `/memory mode [off\|suggest\|auto]` | 查看或切换当前进程的 Auto Memory 模式 |
| `/memory review [pending\|accepted\|rejected\|superseded\|all]` | 按状态查看候选、触发来源、置信度、证据数与冲突 |
| `/memory review accept\|reject <id>` | 接受为 `project/knowledge`，或拒绝且永不自动写入 |
| `/memory migrate [user\|project\|session]` | 幂等迁移旧 JSONL：补全 schema、脱敏、去重、保留 pinned；仅在变更时备份 |
| `/compact` | 立即执行工具微压缩+Structured Compact v2（不等预算），完成后显示 generation、来源节省量、checkpoint 与实际边界 |
| `/compact preview [--auto]` | 预览工具微压缩与 compact 的 head/compact/keep 边界、高低水位、来源节省量、条数、预计 tokens 与待摘要消息；不改 history。`--auto` 仅在超预算时预览 |
| `/compact <focus>` | 强制 compact，并把自然语言重点写入交接摘要，例如保留 API 设计、失败测试和下一步 |
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
| `/plugins [reload\|show <name>\|install\|update\|enable\|disable\|remove ...]` | 查看状态、热重载和 Plugin v2 基础生命周期 |
| `/extensions [kind] [source]` | 统一列出 Plugin/Tool/Skill/Agent/Command/Hook/MCP 的来源、scope、启停与冲突 |
| `/lsp` | 查看语言服务器配置、按需启动状态与加载问题（不启动未使用的服务器） |
| `/web` | 查看 WebFetch/WebSearch 网络边界、搜索后端与密钥环境变量是否就绪（不发起请求） |
| `/output-style [list\|show <name>\|reload\|off\|<name>]` | 查看、切换或重载声明式 Output Style |
| `/mcp` · `/mcp reload` | MCP 状态（含 Resource/Template/OAuth 统计）· 重读配置热重载 |
| `/mcp auth <server>` | 对 HTTP MCP 服务器执行 authorization code + PKCE OAuth 授权并重连 |
| `/allow [add\|remove <程序名>]` | 旧版程序白名单兼容入口（持久化） |
| `/stop` | 中止运行中任务（历史自动修复） |
| `/reset` | 清空历史与当前 turn 时间线 |
| `/exit` | 退出（Ctrl-D 同） |

### Custom / Plugin CommandSource

命令目录按 user → project → configured 顺序扫描，后扫描的项目/显式目录覆盖同名用户命令：

```text
~/.krelcode/commands/*.md
<workspace>/.krelcode/commands/*.md
<workspace>/.commands/*.md
[commands].paths                 # 相对路径基于 workspace
```

```markdown
---
name: review-api
description: Review API changes with project rules
argument-hint: <path>
allowed-tools: [read_file, grep, glob]
---
Review $ARGUMENTS. Check compatibility and cite file:line.
```

- `name` 必须匹配 `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`，正文不能为空；frontmatter 缺失、
  路径逃逸/越界 symlink、大于 256 KB 或单个坏文件只记录 issue。
- `$ARGUMENTS` / `${ARGUMENTS}` 替换用户参数；没有占位符时参数追加到正文末尾。
  执行时以 `<custom_command>` envelope 进入正常 AgentLoop。
- `allowed-tools` 只能收窄当前工具集，仍需经过权限、Hook、审批与 Sandbox。
- 来源优先级：`builtin > custom > skill > plugin > MCP`。Plugin Markdown Command
  保留独立 `CommandSource.PLUGIN` 与 `/plugin__<plugin>__<command>` 名称。

Fragment 启停是**当前会话运行时状态**，不写入 TOML；重启后恢复默认全开。
稳定内置来源为按职责拆分的 `core:base*`（不可关闭的生产级 Base Prompt sections）
与 `workspace:rules`。模型 profile 以独立 `core:base:profile:<name>` Fragment 追加，
不能覆盖共享的安全与指令权威 section。Git/tree、
日期、Skill/Agent/MCP catalog 和 mode/token reminder 属于动态
`ContextAttachmentStore`，各有独立 revision，不参与 Fragment 启停。cacheable Fragment
会形成连续的稳定前缀；只有真正位于第一个动态 block 之前的稳定内容会报告为 cache boundary。

TUI 的 `Context` 面板展示当前请求 token 分布、stable/volatile、source/fragment/schema
数量、cache hash 是否变化、实际 Memory 命中、dropped 来源、Tool Microcompact 节省量与
compact generation/来源/水位/checkpoint。高频 panel redraw 只读 `SessionViewState` 和缓存
快照，不调用 Memory `list()` / `dashboard_snapshot()`，也不会重新扫描工作区文件；定向刷新
使用 `/context refresh`。操作与字段解释见 `docs/context-and-memory.md`。

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

默认配置按低到高优先级深合并：

```text
~/.krelcode/config.toml
<workspace>/krelcode.toml
<workspace>/krelcode.local.toml
KRELCODE_MANAGED_CONFIG 或平台 managed 路径
```

字典递归合并；数组与标量由高层整体替换。`--config <path>` 是显式独占模式，只读取该文件，不与默认层合并。`krelcode.local.toml` 已在 `.gitignore` 中且被写保护。managed 配置损坏时 fail-closed；普通 user/project/local 层的解析错误会由 `doctor` 明确报告，并继续使用其余可信层。

```toml
default_provider = "openai"          # openai / anthropic / gemini / mock

[prompt]
profile = "auto"                     # auto / default / claude / deepseek / openai / gemini
release = "2026.11.21"               # 当前内置的不可变 Prompt Release
calibration_baselines = []           # live baseline 路径；用于模型 Profile 校准

[providers.openai]
model = "gpt-4o-mini"                # 覆盖 KRELCODE_MODEL
base_url = "https://api.openai.com/v1"
api_key = "sk-..."                   # 不建议明文；优先 api_key_env
api_key_env = "OPENAI_API_KEY"
timeout = 120
prompt_cache = "auto"                # auto / on / off / implicit / explicit
generation = { temperature = 0.3 }   # 生成参数透传（三家通用）

[providers.anthropic]
model = "claude-sonnet-4-5"
api_key_env = "ANTHROPIC_API_KEY"
max_tokens = 4096
prompt_cache = "auto"                # 协商后使用自适应显式断点
cache_min_tokens = 1024              # 可选；Sonnet/Opus 默认 1024，Haiku 默认 2048
thinking_budget = 10000              # 扩展思考（≥1024；缺省不启用）

[providers.gemini]
model = "gemini-2.5-flash"
api_key_env = "GEMINI_API_KEY"       # GOOGLE_API_KEY 兜底
prompt_cache = "auto"                # 2.5+ 协商为 server-managed implicit cache

[sandbox]
backend = "auto"                     # auto / static / seatbelt / bubblewrap
network = false                      # 默认禁止子进程联网；CLI --sandbox-network 可开启

[permissions]
mode = "default"                     # default / read-only / auto-approve / plan

[memory]                             # Auto Memory（只记录稳定、可复用、已验证知识）
mode = "suggest"                     # off / suggest / auto；默认只入队，不自动写
suggest_threshold = 0.55             # 候选入队阈值
auto_threshold = 0.80                # auto 模式晋级阈值；冲突候选永不自动晋级

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

[commands]                           # 自定义 Markdown slash command
paths = ["./team-commands", "/opt/shared-commands"]

[context]                            # Context v3（legacy 仅保留一个版本用于回退）
engine = "v3"                        # v3（默认）| legacy（兼容 v2）
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

[lsp]                                # 语言服务器（默认关闭，按扩展名懒启动）
enabled = false
startup_timeout_ms = 10000
request_timeout_ms = 5000

[lsp.servers.python]
command = "pyright-langserver"
args = ["--stdio"]
extensions = [".py"]
language_id = "python"

[output_styles]                      # Markdown 只读指令；项目同名覆盖用户
enabled = true
active = ""                          # 空=默认输出
paths = ["./output-styles"]

[status_line]                        # 仅模板变量，不执行 shell
enabled = true
template = "{provider}/{model} · {permission} · {context} · {branch}"
max_length = 160

[web]                                # WebFetch 安全网络边界
enabled = true
timeout = 15
max_bytes = 2097152
max_redirects = 5
max_chars = 20000
allowed_ports = [80, 443]

[web_search]                         # SearchBackend；密钥只从环境变量读取
enabled = true
provider = "brave"
api_key_env = "BRAVE_SEARCH_API_KEY"
endpoint = "https://api.search.brave.com/res/v1/web/search"
max_results = 5

[mcp_servers.fetch]                  # stdio 传输
command = "uvx"
args = ["mcp-server-fetch"]
env = { KEY = "value" }
timeout = 30

[mcp_servers.remote]                 # HTTP 传输
url = "http://localhost:3000/mcp"    # 默认 streamable-http（2025-03-26）
transport = "sse"                    # 显式回退旧 HTTP+SSE（2024-11-05）
headers = { Authorization = "Bearer ${MCP_TOKEN}" }

[mcp_servers.remote.oauth]           # 可选；静态/刷新/客户端凭据/交互授权
access_token = "${MCP_ACCESS_TOKEN}" # 可选；也可由 /mcp auth 写入缓存
refresh_token = "${MCP_REFRESH_TOKEN}"
token_url = "https://auth.example.com/token"
authorization_url = "https://auth.example.com/authorize"
client_id = "krelcode"
client_secret = "${MCP_CLIENT_SECRET}"
scopes = ["mcp.read"]
resource = "https://mcp.example.com/mcp"
redirect_port = 8765
# grant_type = "client_credentials"  # 机器到机器时可显式选择
# token_endpoint_auth_method = "client_secret_basic" # 或 client_secret_post
# resource_metadata_url / authorization_server / registration_url / redirect_uri

[[hooks]]
on = "file_edited"                   # 触发器见下文
command = "ruff format {path}"       # 模板变量自动 shell-quote
tools = ["write_file", "edit_*"]     # 可选 glob；只影响工具型触发器
timeout = 30
enabled = true
```

`engine = "v3"` 是默认装配路径：所有非 system 来源都带 `trust/origin`，Memory、
Working Context 与 Hook runtime context 只作为 data-not-instructions 注入；高置信度
“只解释指定文件”会过滤 git/tree 等无关 workspace 来源。缺少该键时仍使用 v3；
`engine = "legacy"`（兼容别名 `v2`）仅用于一个发布周期的故障回退，未知值会安全回落
到 v3。`/context`、`/context plan` 与 stream-json 的 `context_engine` 字段可确认当前
请求实际使用的引擎。

配置优先级：**显式 CLI 旗标 > TOML > 环境变量 > 内置默认**。
未知键进 `extra` 透传（如 anthropic 的 thinking_budget）。

`prompt_cache` 是“用户偏好”，不是直接写死 provider wire 的开关。Krelcode 先读取当前
Provider/model 的 `CacheCapabilities`，再协商出实际 `effective_mode`：

- `auto` / `on`：选择该 Provider 支持的最强缓存模式；Anthropic 为 `explicit`，
  OpenAI-compatible/DeepSeek 与 Gemini 2.5+ 为 server-managed `implicit`；
- `off` / `false`：不生成显式缓存控制，也不把缓存段计入本端观测；服务端自身的隐式
  前缀缓存无法通过普通 OpenAI-compatible API 强制关闭；
- `explicit`：显式 `cache_control` 不受支持时可降级为 `implicit`，并记录
  `cache_negotiation_reason=explicit_unsupported_fell_back_to_implicit`；
- `implicit`：只接受 server-managed 缓存，不会意外给 Anthropic 增加显式断点。

Anthropic 的 explicit 缓存使用 `adaptive-prefix-v1` planner，而不是固定写死三个断点。
Planner 在最终 wire 顺序中按 tools → stable system → messages 计算候选前缀，优先选择最新
system 与 tools 稳定锚点、当前消息 frontier，再用剩余槽位选择历史中段 checkpoint；始终
遵守 Provider 的 `max_breakpoints`。默认最小缓存前缀为 Sonnet/Opus 1024 tokens、
Haiku 2048 tokens；`cache_min_tokens` 可覆盖该值，设为 `0` 可用于离线测试或确知模型
支持更短前缀的兼容端点。单个候选的累计前缀低于阈值时不会写入该候选的 `cache_control`，
并记录 fail-closed reason。

兼容旧配置：布尔值 `true` / `false` 分别等价于 `on` / `off`。非法值会在启动时以
`ConfigError` 失败。实际 requested/effective mode、协商原因、fallback、支持模式、断点
数量/位置/hash、跳过原因和最小 token 阈值可通过 `/context` 与 `/context cache` 查看，
均由真实 `PreparedLLMRequest` 派生，不包含 Prompt、工具正文、headers 或 API key。

`[prompt].profile = "auto"` 先按模型名做最长模式匹配（例如 `deepseek-*`、
`claude-*`、`gpt-*`、`gemini-*`），再按 Provider family 匹配，最后回退到
`default`。显式指定 profile 会覆盖自动识别；未知名称启动时直接报配置错误。
profile 只追加模型适配指导，不替换、关闭或削弱 `core:base:safety` 等共享
required section。`/model` 热切换在 `auto` 模式会重新解析 profile、原子替换
`core:base*` 并刷新 stable hash；显式 profile 保持不变。子代理按自身实际
provider/model 解析同一套 profile。

`[prompt].release` 只能选择当前代码内置的不可变 Release；未知值启动失败。Release
hash 由全部 Base section hash、Profile pattern 和 Profile section hash 归一化生成，
不保存 Prompt 正文。`calibration_baselines` 接受一个字符串或字符串数组，路径相对
workspace 解析；文件必须是 `--save-baseline` 生成的 schema v2 live baseline。

运行时按 provider/model 匹配 calibration scope；精确 provider/model 优先，其次是最长
provider/model pattern。匹配记录还必须与当前 Release、Profile hash、Parity contract
和冻结 behavior suite 一致。没有证据时状态为 `uncalibrated`；Release/contract/suite
漂移时状态为 `stale`。这两种状态都可正常对话，但 `/context why` 会明确展示，且
`--gate` 不会接受旧现场。Calibration 只是 Benchmark 证据，不会作为额外 System
Instruction 注入。

MCP 远程能力：

- `tools/list`、`resources/list`、`resources/templates/list`、`prompts/list` 都自动
  跟随 `nextCursor`；单个列表最多 100 页，重复 cursor/非法结构 fail-closed。
- resources capability 自动注册 `list_resources`、`read_resource` 和
  `list_resource_templates`；Prompt 继续映射到 `/mcp__<server>__<prompt>`。
- `notifications/tools/list_changed`、`notifications/resources/list_changed`、
  `notifications/prompts/list_changed` 会按服务器串行刷新。工具替换先验证重复/命名
  冲突再原子替换；刷新失败保留旧目录，不影响主 Agent 和其他服务器。
- OAuth 支持 Bearer、refresh token、client credentials、protected-resource
  metadata、authorization-server metadata、动态客户端注册与 authorization code +
  PKCE。首次交互授权执行 `/mcp auth <server>`；401/403 会刷新 token 后重试一次。
- HTTP Header 值和 OAuth 字符串字段支持 `${ENV_VAR}` 展开，建议 secret 不直接写入
  TOML。OAuth token cache 位于 `~/.krelcode/mcp-oauth.json`：以 server/url 哈希隔离，
  原子写入并尽力设置为 `0600`；缓存含 bearer/refresh token，不应提交或分享。

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

三类 Hook 共用同一触发器和决策协议：

- `command`（默认）：从 stdin 接收 JSON payload；
- `http`：向 `http/https` URL POST 同一 JSON，Header 支持 `${ENV_VAR}`；
- `prompt`：把 Hook 指令与结构化 payload 交给当前 LLM，输出走同一解释器。

```toml
[[hooks]]
on = "pre_tool_use"
type = "http"
url = "${HOOK_ENDPOINT}"
headers = { Authorization = "Bearer ${HOOK_TOKEN}" }
timeout = 10

[[hooks]]
on = "user_prompt_submit"
type = "prompt"
prompt = "Apply the repository policy to: {prompt}"
```

每次 command/http/prompt 执行都使用同一 payload：

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

command Hook 同时提供 `KRELCODE_HOOK_EVENT`、`KRELCODE_WORKSPACE`、
`KRELCODE_TOOL` 环境变量。Hook 子进程使用净化环境，不继承 API key/token/proxy/
SSH agent。Hook 来自用户配置或可信插件，视为显式信任，不进入工具审批链；HTTP
诊断目标会去掉 userinfo/query/fragment，不记录 Header 或 Prompt 正文，解析后的
Header 凭据会动态加入 `SecretRedactor`。stdout 日志、注入上下文、用户提示与执行
审计记录仍统一脱敏。

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
├── memory/            # entries.jsonl + candidates.jsonl + sessions/<session-id>.jsonl
├── jobs/              后台 Shell 任务脱敏日志（每任务 output.log，默认 10 MiB 上限）
├── sidechains/        子代理 JSONL（按 manager run-id 分目录；定义/feed/状态/history/结果）
├── exports/           /export 缺省输出目录
├── sandbox/            隔离 HOME/TMP/cache（命令与 Skill 脚本使用）
└── allow_rules.json   审批规则（旧版 allow_programs/allow_commands + rules[]，仅用户可改）
```

全局插件启停状态：`~/.krelcode/plugin-state.json`（按 `scope:name` 隔离）。
全局 MCP OAuth token cache：`~/.krelcode/mcp-oauth.json`（`0600`，含 token，属于敏感文件）。

全局：`~/.krelcode/config.toml`（配置）、`~/.krelcode/AGENTS.md`（全局规则，
与项目级 `AGENTS.md`/`KRELCODE.md` 分层注入，项目规则在后=更具体者优先）、
`~/.krelcode/memory/entries.jsonl`（跨工作区 user 记忆，权限与隐私由本机用户边界保护）。

Memory 作用域：`user`（跨工作区）→ `project`（当前工作区 `.krelcode/memory/entries.jsonl`）
→ `session`（`.krelcode/memory/sessions/<session-id>.jsonl`）。每层独立限长、限条数、
脱敏与原子删除；读取时以文件所在目录决定 scope，旧 project 记录缺少新字段时自动按
`note/manual` 兼容。fork/resume 会重新绑定当前会话，no-session 模式下的 session 记忆仅在进程内存在。
Auto Memory 候选保存在项目本地 `.krelcode/memory/candidates.jsonl`，不会跨 workspace；
模型生成的候选一律为 `knowledge` 类型，写入前执行脱敏、摘要去重、冲突检测与置信度阈值。
Memory Fragment 明确声明为 untrusted contextual knowledge，而不是 system instructions。
`/memory migrate [scope]` 会把旧 JSONL 补全为新 schema，脱敏并合并规范化文本重复项，保留
pinned/tag/metadata，跳过损坏行；只有内容实际变化时才创建同目录
`entries.jsonl.bak-YYYYMMDD-HHMMSS[-N]`，随后原子重写。迁移幂等，二次执行不会重复备份。
存储字段、完整命令和恢复方式见 `docs/context-and-memory.md`。

Agent Definition 发现目录（后者覆盖前者同名项）：`~/.krelcode/agents/*.md`、
`<workspace>/.krelcode/agents/*.md`、`<workspace>/.agents/agents/*.md`。格式：

```markdown
---
name: reviewer
description: 只读代码审查
allowed-tools: [read_file, grep, glob, repo_map]
model: deepseek/deepseek-flash  # 可选；provider/model 或沿用父 provider 的 model
permission-mode: read-only      # default / plan / read-only / auto-approve
mcp-servers: [github, docs]     # 省略=继承全部；*=全部；[]=不使用 MCP
fork-context: true     # 可选；默认继承父对话
isolated: false        # 可选；true=独立 git worktree
---
你是严格的代码审查员，只报告可复现的问题并给出文件路径与行号。
```

`allowed-tools`（也接受 `tools`）与 `mcp-servers` 只能缩小默认工具池；未知工具/
MCP 服务器会让该定义在 spawn 时 fail-closed。`model` 只影响该子代理，不改变父级
或 `/model` 当前选择；`permission-mode` 写入子代理独立 ToolContext，与审批/Sandbox
共同生效。Definition frontmatter/正文属于用户显式配置，但不会绕过审批、Sandbox、
只读模式或子代理防递归边界。单个坏定义只记录 issue，不阻断主 Agent。

Skill 发现目录（后者覆盖前者同名项）：`~/.agents/skills`、`~/.krelcode/skills`、
`<workspace>/.agents/skills`、`<workspace>/.krelcode/skills`，再追加
`[skills].paths` / `--skills-dir`。每个 Skill 是包含 `SKILL.md` 的目录。

Plugin 发现目录（后者覆盖前者同名插件）：`~/.krelcode/plugins`、
`<workspace>/.krelcode/plugins`，再追加 `[plugins].paths` / `--plugins-dir`。
每个插件是包含 `plugin.toml` 的目录；插件代码按本地可信代码处理，单插件加载失败
只记录 issue，不影响主 Agent。

Plugin v2 可用一个 manifest 同时声明 Tool、Skill、Agent、Command、Hook、MCP：

```toml
manifest_version = 2
name = "demo"
version = "1.0.0"

[tools]
entrypoint = "plugin:tools"

[skills]
paths = ["skills"]

[agents]
paths = ["agents"]

[commands]
paths = ["commands"]

[hooks]
paths = ["hooks.toml"]

[mcp]
paths = ["mcp.toml"]
```

所有组件路径必须是插件内相对路径，拒绝绝对路径、`..` 与越界 symlink。工具命名空间为
`plugin__<plugin>__<tool>`，Command 为 `/plugin__<plugin>__<command>`；MCP 服务器默认
命名为 `plugin__<plugin>__<server>`。`/extensions` 展示统一来源与冲突，`/plugins reload`
会原子刷新 Tool/Skill/Agent/Command/Hook/MCP。`install/update/remove/enable/disable`
会持久化生命周期状态；安装限制 5000 文件 / 100 MiB，remove 移到 `.trash`。
插件 `hooks.toml` 与用户 Hook 使用相同 `command/http/prompt` schema，并自动带来源
`plugin:<name>@<version>` 与 `KRELCODE_PLUGIN_ROOT`。

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
| 只读工具（get_time/read_file/grep/glob/find_refs/repo_map/check_syntax/memory_search/ask_user/update_plan，以及 list/load/read Skill） | 放行；显式 `deny/ask` 路径或工具规则优先；`ask_user` 无交互前台时工具结果 fail-closed |
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
`ContextDropped`(content-free source ids) · `MemoryLoaded`(entry ids/scope/token) ·
`MemoryCandidateObserved`(candidate id/status/trigger/confidence/digest) ·
`MemoryWritten`(id/action/scope/kind/token) ·
`ContextCompressed`(before/after、strategy、summary source/generation、高/低水位、来源节省量、checkpoint ID) ·
`CompactCheckpointCreated`(checkpoint/generation/before/after/summary digest/source savings) ·
`HookTriggered` · `SubagentSpawned` · `SubagentFeedUpdated` · `SubagentStateChanged` · `SubagentFinished` ·
`ShellJobStarted` · `ShellJobOutput` · `ShellJobFinished` · `UserSteered` ·
`RunFinished` · `ErrorOccurred`

JSONL 审计记录类型：`user_input` `tool_call` `tool_result` `permission_denied`
`hook_triggered` `context_assembled` `context_dropped` `memory_loaded` `memory_candidate`
`memory_written` `compact_checkpoint` `context_compressed`(含 Structured Compact 元数据) `subagent_spawned` `subagent_feed` `subagent_state`
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
`context/dropped`、`memory/loaded|candidate|written`、`compact/checkpoint`、
`hook/triggered`、`subagent/started|feed|state|finished`、`job/started|output_delta|finished`、
`user/steered`、`error/occurred`；job 事件独立于 turn，因此不带 turn_id，但带
`session_id`；`job/output_delta` 的 `sequence` 可直接作为 `after_sequence` 游标；
`context/assembled` 的 `snapshot` 只含 token 分项、预算、cache boundary 与 hash，不含 Prompt 正文；
`context/dropped` 只含来源 ID 和 reason；`memory/loaded` 只含 entry ID/scope/token，不含
记忆正文；`memory/candidate` 含 candidate ID/status/trigger/confidence/冲突数/digest；
`memory/written` 含 memory ID/action/scope/kind/token；`compact/checkpoint` 含
checkpoint/generation/before/after/summary digest/strategy/source savings；
`context/compressed` 只含策略、摘要来源/generation、水位、来源节省量与 checkpoint ID，不含摘要正文；
`subagent/feed` 的 agent cursor 是 `agent_sequence`（外层 `sequence` 仍是 QueryEvent 顺序）；
最后以 `system/session_end` 结束。turn 内事件都带 `turn_id`、单调 `sequence` 和
`session_id`，user 对应的全部事件及 result 还带同一 `request_id`；后台 job 是跨 turn
长生命周期事件，使用自己的 `job_id` 与游标。

上述新事件同时保留旧式 `type/subtype`，并增加稳定 `event` 名：
`context_assembled`、`context_dropped`、`memory_loaded`、`memory_candidate`、
`memory_written`、`compact_checkpoint`。消费者可优先按 `event` 路由，兼容消费者仍可按
`type/subtype` 解析。

stream-json 的 `user.content` 可以是 slash command。命令由同一个 command registry 直接执行，
不创建模型请求；先输出内部 `memory/*`、`compact/*`、`context/*` 等事件，再输出：

```json
{"type":"command","subtype":"result","command":"/memory","handled":true,
 "output":["[memory] ..."],"run_prompt":false,"turn_id":"command-1",
 "sequence":2,"session_id":"..."}
```

随后仍为该 user 请求发出唯一普通 `result`。`run_prompt=true` 的命令会继续执行普通 Agent
查询，最终 `result` 对应该查询；REPL、headless text/json、stream-json 与 SDK 共用同一
命令处理与脱敏语义。详细示例见 `docs/context-and-memory.md`。

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

## 11. TypeScript SDK

`typescript-sdk/` 是零运行时依赖的 Node/TypeScript 客户端，不复制 Python
Agent 逻辑，而是复用同一 `stream-json` v1 子进程协议：

```ts
import { KrelcodeClient } from "@krelcode/sdk";

const client = await KrelcodeClient.start({
  workspace: ".",
  mock: true,
  noSession: true,
  noContext: true,
  noMcp: true,
});
try {
  console.log(await client.run("解释 README"));
} finally {
  await client.close();
}
```

可通过 `KRELCODE_PYTHON` 指定能运行 `python -m krelcode` 的解释器；API key
继续通过 `options.env` 或宿主环境注入，不会进入 `argv`。Node 24 可直接运行
`npm test`，测试使用内置 TypeScript stripping；`npm run typecheck` 需要本机
安装开发依赖 `typescript` 与 `@types/node`。
