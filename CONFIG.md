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
| `--auto-approve` | — | ASK 级操作自动批准（DENY 级仍拒绝） |
| `--permission-mode default\|read-only\|auto-approve\|plan` | 配置/`default` | 工具权限模式；`read-only`/`plan` 只暴露只读工具 |
| `--sandbox-backend auto\|static\|seatbelt\|bubblewrap` | auto | 运行时隔离后端；显式后端不可用则启动失败 |
| `--sandbox-network` | — | 允许 Sandbox 子进程访问网络（默认关闭） |
| `--no-sandbox` | — | 同时关闭审批策略与运行时 Sandbox（AllowAll，自担风险） |
| `--no-mcp` | — | 不启动配置中的 MCP 服务器 |
| `--resume` | — | 恢复会话：裸旗=最新，`--resume <path>`=指定 |
| `--no-session` | — | 关闭会话 JSONL 录制 |
| `--no-checkpoint` | — | 关闭文件检查点（/undo） |
| `--auto-commit` | — | 每轮有变更自动 git 提交 |
| `--version` | — | 打印版本退出 |

## 2. REPL 命令

| 命令 | 说明 |
|------|------|
| `/help` | 帮助 |
| `/history` | 当前对话历史 |
| `/thinking` | 展开最近一次思考（正文默认折叠） |
| `/tool-output [编号\|last]` | 列出工具输出（默认折叠）；按编号或 `last` 在灰色背景面板中展开 |
| `/queue [delete <n>\|move <n> <m>\|clear]` | 查看或管理 `Alt+Enter` 排队的下一条消息 |
| `/tools` | 已注册工具（含 MCP 远程与子代理工具） |
| `/model [provider/]model` | 查看/热切换模型（预算联动，失败不破坏现状） |
| `/cost` | token 用量（本轮+累计）与估算成本 $ |
| `/context` | 上下文统计（消息数/估算 tokens/预算） |
| `/context refresh` | 重采目录树/git/规则文件并重建 system prompt |
| `/compact` | 立即压缩历史（驱逐+LLM 摘要，不等预算） |
| `/sessions` | 列出工作区全部会话 |
| `/resume <n\|latest\|path>` | 会话内切换 |
| `/session` | 当前会话文件路径 |
| `/export [路径]` | 导出会话 Markdown（缺省 `.krelcode/exports/`） |
| `/undo` · `/undo list` | 回滚上一轮文件改动 · 查看检查点栈 |
| `/agents` | 子代理状态（含排队深度） |
| `/skills [关键词]` | 列出或搜索 Skill |
| `/skills reload` | 重读全部 Skill 目录并刷新 system prompt 索引 |
| `/skills show <name>` | 查看元数据、资源与完整 `SKILL.md` |
| `/skill <name> [参数]` | 用户主动加载并执行 Skill；参数替换 `$ARGUMENTS` |
| `/mcp` · `/mcp reload` | MCP 服务器状态 · 重读配置热重载 |
| `/allow [add\|remove <程序名>]` | 命令审批白名单管理（持久化） |
| `/stop` | 中止运行中任务（历史自动修复） |
| `/reset` | 清空历史 |
| `/exit` | 退出（Ctrl-D 同） |

**运行中（steering）**：`Enter`=插话（当前 turn 下一步看到，`[用户插话]` 前缀注入）；
`Alt+Enter`=排队为下一条独立消息，当前 turn 自然完成或中止后自动发送；
`Esc`/`Ctrl-C` 中止当前任务；`/stop` 中止；`/exit` 停止并退出。
空闲时 `Ctrl-C` 清空当前输入但**不退出**，`Ctrl-D` 或 `/exit` 退出。
审批请求由 REPL 前台通过异步 broker 接管；`y`/`n`/`a` 不会被写入用户问题历史。
**消息语法**：`@路径` 注入文本文件（8K/5 个预算）或附加图片（png/jpeg/webp/gif，5MB/4 张）。

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
prompt_cache = true                  # 3 断点 ephemeral 缓存（默认开）
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

[context]                            # 启动时工作区上下文（Context v2）
max_tokens = 6000                    # rules + git + tree 的统一 token 预算
include_rules = true                 # ~/.krelcode/AGENTS.md + 项目规则
include_git = true                   # 分支与 porcelain 状态
include_tree = true                  # 剪枝后的目录树
tree_max_depth = 3
tree_max_entries = 150
rules_max_chars = 8000

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
- 子代理：`subagent_started`、`subagent_finished`
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
├── sessions/          会话 JSONL（审计事件流 + 每轮 history 快照）
├── checkpoints/       文件快照（/undo 数据源，NNNN-<ts>/meta.json + files/）
├── tool_outputs/      超长工具输出全文（历史里留截断+路径引用）
├── exports/           /export 缺省输出目录
├── sandbox/            隔离 HOME/TMP/cache（命令与 Skill 脚本使用）
└── allow_rules.json   审批白名单（{"allow_programs": [...]}，仅用户可改）
```

全局：`~/.krelcode/config.toml`（配置）、`~/.krelcode/AGENTS.md`（全局规则，
与项目级 `AGENTS.md`/`KRELCODE.md` 分层注入，项目规则在后=更具体者优先）。

Skill 发现目录（后者覆盖前者同名项）：`~/.agents/skills`、`~/.krelcode/skills`、
`<workspace>/.agents/skills`、`<workspace>/.krelcode/skills`，再追加
`[skills].paths` / `--skills-dir`。每个 Skill 是包含 `SKILL.md` 的目录。

## 6. 安全策略速查（审批策略 + 运行时 Sandbox，默认开启）

| 操作 | 处置 |
|------|------|
| 只读工具（get_time/read_file/grep/glob/find_refs/repo_map/check_syntax，以及 list/load/read Skill） | 放行 |
| write_file/edit_file（普通路径） | 放行（工作区约束兜底） |
| write/edit 受保护路径：`.git/`、`.krelcode/`、`krelcode.toml`、`.env*`、`*.pem/*.key/*.p12/*.pfx`、`id_rsa*`、`secrets.*`、`credentials*`、`.npmrc/.netrc/.pgpass/.htpasswd` | ASK（非交互拒绝） |
| run_command 只读命令（ls/cat/grep/git status·log·diff…） | 放行 |
| run_command 执行代码/装依赖/删文件/git 写操作/网络/未知命令/重定向 | ASK |
| run_skill_script（Skill 内 bundled 脚本） | ASK；参数数组不经过 shell，子进程走运行时 Sandbox |
| run_command：sudo/su、dd/mkfs/diskutil、shutdown/reboot、rm 灾难目标、curl\|sh、fork 炸弹 | **DENY（不可越过，--auto-approve 也拒）** |
| ASK 处理链 | allow_rules 命中 → --auto-approve → 交互 [y/n/a] → 都没有则拒绝（fail-closed） |
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

`RunStarted` · `StepStarted` · `LLMThinkingDelta` · `LLMStreamDelta` ·
`LLMResponseReceived`(usage) · `ToolCallStarted` · `ToolCallCompleted`(is_error) ·
`ApprovalRequested` · `PermissionDenied` · `ContextCompressed`(before/after) ·
`HookTriggered` · `SubagentSpawned` · `SubagentFinished` · `UserSteered` ·
`RunFinished` · `ErrorOccurred`

JSONL 审计记录类型：`user_input` `tool_call` `tool_result` `permission_denied`
`hook_triggered` `context_compressed` `subagent_spawned` `subagent_finished` `user_steered`
`run_finished` `history`(快照)。

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
`system/turn_started`、`step/started`、`assistant/text_delta|thinking_delta|message`、
`tool/started|completed`、`permission/requested|denied`、`context/compressed`、
`hook/triggered`、`subagent/started|finished`、`user/steered`、`error/occurred`；
最后以 `system/session_end` 结束。所有事件都带 `turn_id`、单调 `sequence` 和
`session_id`，user 对应的全部事件及 result 还带同一 `request_id`。

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
