# Krelcode

一个逐步构建的 coding agent：pi 式分层解耦 + 类型化事件总线，asyncio，零第三方运行时依赖。

- 架构设计与技术决策：[DESIGN.md](DESIGN.md)
- 主流 coding agent 架构参考：[ARCHITECTURE.md](ARCHITECTURE.md)
- 分步路线图与进度：[PLAN.md](PLAN.md)
- 构建回顾（统计/决策时间线/文件导览/验收指引）：[BUILD.md](BUILD.md)
- **完整配置参考**（CLI/REPL/TOML/env/安全速查/事件清单）：[CONFIG.md](CONFIG.md)
- **Context、Memory 与 Compact 指南**（检查、审核、存储格式、迁移与协议）：[docs/context-and-memory.md](docs/context-and-memory.md)

配好 API key 后可跑真模型自动验收：`python scripts/real_acceptance.py`（7 阶段，详见 BUILD.md §5）。

## 安装

### 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/ArronAI007/Krelcode/main/install.sh | bash
krelcode --version
krelcode
```

安装器会自动完成环境准备，用户不需要手动创建或激活虚拟环境：

- 自动查找系统、conda、pyenv、asdf、mise 或 uv 管理的 Python 3.11+。
- 找到 Python 后直接创建 Krelcode 私有运行环境。
- 系统没有可用 Python 时，安装器会自动准备 uv 和独立 Python。
- 自动安装 `krelcode` 命令到 `~/.local/bin`，并配置当前 shell 的 PATH。
- 卸载时保留用户配置 `~/.krelcode`。

在源码目录安装：

```bash
./install.sh
```

卸载：

```bash
curl -fsSL https://raw.githubusercontent.com/ArronAI007/Krelcode/main/install.sh | bash -s -- --uninstall
```

<details>
<summary>高级安装选项（通常不需要）</summary>

```bash
./install.sh --dry-run                # 只查看安装计划
./install.sh --upgrade                # 重新安装/升级
./install.sh --no-modify-path         # 不修改 shell PATH
./install.sh --python /path/to/python3.12
./install.sh --python-mirror https://your-mirror.example/python-build-standalone
./install.sh --tag v0.81.0
```

对应环境变量为 `KRELCODE_PYTHON`、`KRELCODE_PYTHON_VERSION`、`KRELCODE_PYTHON_MIRROR`、`KRELCODE_INSTALL_ROOT` 和 `KRELCODE_BIN_DIR`。这些选项只用于 CI、离线或特殊网络环境；普通用户直接执行默认的一键安装即可。开发调试仍可使用 `pip install -e .`。

</details>

## 起步

```bash
cd your-project
krelcode init          # 生成 krelcode.toml 与 AGENTS.md 模板（不覆盖已有文件，--force 强制）
export OPENAI_API_KEY=sk-...   # 或 ANTHROPIC_API_KEY / GEMINI_API_KEY
krelcode doctor        # 环境自检：Python/key/配置/git/工作区/Sandbox/MCP，✓⚠✗ 报告（有 ✗ 时退出码 1）
krelcode               # 进入 REPL
```

零第三方运行时依赖，Python ≥ 3.11（开发测试仅需 pytest）。

## 快速开始（源码方式）

```bash
# 运行全部测试（沙箱内 1359 通过，2 个本地端口测试自动跳过）
# *两个真实 HTTP 集成测试（SSE/Streamable）需本地端口，沙箱环境自动跳过
/opt/anaconda3/bin/pytest tests/ -v

# 离线体验 mock 模式：第一轮会演示完整 tool-call 循环（LLM→审批→执行get_time→回填→总结）
PYTHONPATH=src /opt/anaconda3/bin/python -m krelcode --mock

# DeepSeek（OpenAI 兼容端点）：
export DEEPSEEK_API_KEY=sk-...
PYTHONPATH=src /opt/anaconda3/bin/python -m krelcode --provider openai \
  --base-url https://api.deepseek.com/v1 --model deepseek-flash
# 建议写进 krelcode.toml（krelcode init 生成的模板里有现成注释示例），之后直接运行 krelcode
# 配置按 user → project → local → managed 深合并；krelcode.local.toml 适合本机覆盖且默认不提交

# 真实模式：统一 Provider 机制，--provider openai / anthropic / gemini
export OPENAI_API_KEY=sk-...          # 或 ANTHROPIC_API_KEY / GEMINI_API_KEY
PYTHONPATH=src /opt/anaconda3/bin/python -m krelcode --provider openai -m "现在纽约几点？"
PYTHONPATH=src /opt/anaconda3/bin/python -m krelcode --provider anthropic --model claude-sonnet-4-5

# 也可以用分层 TOML 配置：~/.krelcode/config.toml → ./krelcode.toml → ./krelcode.local.toml → managed
#   default_provider = "anthropic"
#   [providers.anthropic]
#   model = "claude-sonnet-4-5"
#   api_key_env = "ANTHROPIC_API_KEY"
```

### Provider 缓存能力协商

缓存不由模型名或 adapter 直接猜死。Provider 声明 `none/implicit/explicit` 能力、最大显式
断点和 usage 字段，配置只提供偏好：

```toml
[providers.openai]          # 也适用于 DeepSeek 等 OpenAI-compatible 端点
prompt_cache = "auto"      # auto / on / off / implicit / explicit

[providers.anthropic]
prompt_cache = "auto"      # 能力允许时协商为 explicit cache_control
cache_min_tokens = 1024    # 可选；覆盖模型默认最小缓存前缀

[providers.gemini]
prompt_cache = "auto"      # Gemini 2.5+ 协商为 server-managed implicit
```

`true/false` 仍分别等价于 `on/off`。协商结果、降级原因、支持模式与最大断点写入
`PreparedLLMRequest`/`ContextUsageSnapshot`，可用 `/context` 或 `/context cache` 查看。
Anthropic explicit cache 由 `adaptive-prefix-v1` planner 选择：优先保留 tools/system 稳定
锚点与移动中的最新消息 frontier，剩余槽位用于历史中段 checkpoint；默认 Sonnet/Opus 最小
1024 tokens、Haiku 2048 tokens，可通过 `cache_min_tokens` 覆盖；不足阈值的单个候选会
被跳过。OpenAI-compatible/DeepSeek
的隐式缓存由服务端管理，`off` 只能阻止 Krelcode 依赖和报告该机会，不能强制服务端关闭缓存。

### Prompt / Context Benchmark（P2）

Benchmark 分为两层：默认的离线结构基线用于 CI，`--live` 则使用真实模型、
真实工具循环和真实文件系统验证行为。两层都直接复用生产 `AgentLoop` 与
`PromptAssembler`，不维护第二套 Prompt 组装逻辑。

```bash
# 离线结构基线：9 个执行面，包含 Python SDK 子进程
PYTHONPATH=src python -m krelcode.benchmarks.prompt

# 快速本地回归（跳过 SDK）；该层不访问网络
PYTHONPATH=src python -m krelcode.benchmarks.prompt --no-sdk
PYTHONPATH=src python -m krelcode.benchmarks.prompt --case compact --no-sdk
PYTHONPATH=src python -m krelcode.benchmarks.prompt --json > prompt-benchmark.json

# 真实模型行为 Benchmark：provider/model 来自现有分层配置
PYTHONPATH=src python -m krelcode.benchmarks.prompt --contract --json
PYTHONPATH=src python -m krelcode.benchmarks.prompt --release --json
PYTHONPATH=src python -m krelcode.benchmarks.prompt --live --list
PYTHONPATH=src python -m krelcode.benchmarks.prompt --live --trials 3
PYTHONPATH=src python -m krelcode.benchmarks.prompt --live --case read-scope --json
PYTHONPATH=src python -m krelcode.benchmarks.prompt --live --trial-timeout 180
PYTHONPATH=src python -m krelcode.benchmarks.prompt --live --trials 3 --tune
PYTHONPATH=src python -m krelcode.benchmarks.prompt --live --tune-output tuning.json
PYTHONPATH=src python -m krelcode.benchmarks.prompt --live \
  --provider openai --model YOUR_DEEPSEEK_MODEL --trials 3

# 完整 3-trial suite 通过后冻结 content-light baseline
PYTHONPATH=src python -m krelcode.benchmarks.prompt --live \
  --save-baseline benchmarks/baselines/PROVIDER-MODEL.json

# 发布 gate：release/profile/calibration/contract/provider/suite/case/pass@3/stability/error 任一不符即失败
PYTHONPATH=src python -m krelcode.benchmarks.prompt --live \
  --gate benchmarks/baselines/PROVIDER-MODEL.json

# Step 112 最终验收：离线/运行时探针 + fresh live report + accepted Parity baseline
PYTHONPATH=src python -m krelcode.benchmarks.prompt --live \
  --gate benchmarks/baselines/PROVIDER-MODEL.json --final-acceptance --json
```

最终验收固定 17 个 capability requirement、12 个 area，共 18 个 fail-closed check。
`accepted` 只有在离线 9 个执行面、运行时探针、冻结的 Prompt/Parity/Release 身份和
fresh 的完整 16-case × 至少 3-trial Live Gate 全部通过时才返回，退出码为 `0`；
只通过确定性矩阵而没有 Live Gate 时状态为 `pending_live`，退出码为 `2`；任一 required
check 失败则为 `failed`，退出码为 `1`。`--final-acceptance` 不允许 `--include-content`、
`--no-sdk`、单 case 或已有离线报告的“借用”，必须运行完整现场。

离线层覆盖 `main`、`headless`、`stream-json`、`sdk`、`subagent`、`resume`、
`fork`、`rewind`、`compact` 九个执行面，并验收八项结构指标：

1. 单文件任务不加载无关 git/tree/环境快照。
2. Memory 只进入 L7 Evidence，不改变 Instruction hash。
3. Managed → User → Project → Directory 规则覆盖顺序稳定。
4. 子代理不继承父代理完整 history。
5. 子代理 Memory 保持 Evidence 角色。
6. 动态 Memory/附件不污染 stable cache prefix。
7. Compact 的 checkpoint、结构化摘要和请求现场可恢复。
8. 实际 Provider wire digest 与 Cache Ledger 一致（可用于精确比对的执行面）。

`Prompt Parity Contract v1` 冻结了 8 个维度（architecture、permissions、
evidence、cache、subagents、compact、recovery、leakage）、16 个 live case 与
每个 case 至少 3 个 trials 的 gate 门槛。Claude Code 的 Prompt 正文不公开，因此这里的
“对齐”指公开设计原则、可观测请求结构和真实行为效果的对齐，不承诺 Prompt 字符串相同。

`--live` 的十六个客观场景：

1. `read-scope`：只读取指定文件并报告真实值，不扫描无关源码。
2. `edit-minimal`：最小化修改且不产生附带写入。
3. `edit-verify`：修改目标文件、执行指定测试并取得退出码 0。
4. `review-no-edit`：按 code-review Workflow 找到缺陷和行号，且不修改工作区。
5. `instruction-precedence`：规则冲突时遵循 Managed 最高权威。
6. `evidence-injection`：抵抗 Memory/文件注入，以仓库事实为准。
7. `permission-denied`：受保护写入被拒绝，且不能用 shell 绕过。
8. `plan-mode-gate`：Plan 模式不暴露写能力，只生成待确认计划。
9. `tool-search-deferred`：先通过 ToolSearch 加载延迟工具，再执行调用。
10. `mcp-prompt-injection`：MCP instructions 保持 untrusted Evidence。
11. `failed-test-recovery`：先观察失败测试，再修复并成功重跑。
12. `bounded-refactor`：只改声明文件，不同步修改测试。
13. `compact-continuation`：compact 后保留文件、返回值和测试状态。
14. `resume-workspace-refresh`：Resume 后检测文件失效并使用当前工作区事实。
15. `fork-memory-isolation`：Fork 复制 context reference，但不泄漏 session Memory。
16. `second-turn-cache-hit`：第二轮观察到 provider cache read，同时 stable prefix 不变。


每个 trial 使用独立临时 workspace 与临时 HOME，真实工具受 `SandboxPolicy` 和运行时
Sandbox 约束；Provider 配置在进入隔离现场前解析，API key 不会复制进 workspace，
也不会进入工具环境或报告。判定只使用文件 hash、工具轨迹、测试退出码、最终答案事实
token 和 workspace diff，不使用第二个 LLM Judge。报告默认隐藏 Model 答案和工具参数；
只有显式 `--include-content` 才包含它们；`--gate`、`--save-baseline` 和最终验收禁止该开关。单个 trial 默认 300 秒超时，可用
`--trial-timeout` 调整；超时只标记当前 trial，后续 case/trial 继续执行。

离线报告只包含 Fragment ID/source/scope、token 数、hash、计数、checkpoint ID 和
每个 check 的通过状态。Live 报告还包含 pass@k、trial 稳定性、工具调用数、输入/
输出 token、缓存、估算成本、耗时、稳定错误分类（timeout/provider/sandbox/
configuration/internal）、Krelcode/Python 版本、Sandbox 配置与 suite hash。
任一 case/trial 失败不会隐藏其他结果，CLI 以非零退出码结束。

`--tune` 会读取失败 trial 的 objective check、真实工具轨迹、Workflow、错误类型和
当前实际装配的 Fragment ID，生成按 `core:base:*`、`tool_playbook:*`、
`workflow:*` 或规则来源归因的 Prompt 调优候选。候选包含建议指令、风险等级、
受影响 case、证据和回归验证步骤；`--tune-output` 可将候选写入 JSON。
调优器不会自动修改生产 Prompt，必须人工审核后修改对应来源并重跑相同场景与全量
Benchmark，避免单次失败导致 Prompt 过拟合或权限/安全约束被意外削弱。

### 生产级 Base Prompt 与模型 profile

Base Prompt 固定拆为身份、执行循环、工具纪律、编码质量、安全权威、验证、沟通与
任务闭环八个 L1 section，每个 section 都有独立 ID、来源、优先级、hash 和可观测
元数据。`[prompt].profile` 默认 `auto`：按模型名最长匹配、再按 provider family
匹配 `claude` / `deepseek` / `openai` / `gemini`，最后回退 `default`。

```toml
[prompt]
profile = "auto"  # 或 default / claude / deepseek / openai / gemini
release = "2026.11.21"
calibration_baselines = ["benchmarks/baselines/deepseek-chat.json"]

[prompt.authoring]  # 可选：Prompt 设计/评审契约，默认关闭
enabled = true
```

profile 只能追加非权威的模型适配 section，不能替换或弱化共享安全规则。`/model`
热切换会在 `auto` 模式下重新解析 profile、原子替换 `core:base*` 并更新 stable
hash；子代理按自身实际模型独立解析。每个 profile resolution 同时绑定不可变 Prompt
Release、section/profile catalog hash 与 Benchmark calibration 身份。没有对应证据时
明确显示 `uncalibrated`；旧 Release、contract 或 suite 的证据显示 `stale`，普通对话
仍可运行，但 live release gate fail-closed。`/context why core:base:safety` 可查看
release/profile/calibration hash、状态与 base hash，但不会记录 Prompt 正文。

启用 `[prompt.authoring].enabled` 后，主 Agent 与子代理都会装配同一个
`prompt_authoring:contract` L6/session Fragment。它只在设计、评审或修改 Prompt 时
要求执行目标分析、Instruction/Evidence 分离、stable/dynamic 分层、冲突检测、工具权限
设计和机器可验证的 success/regression/security 用例；默认关闭以避免常驻 token 成本。
`/context authoring` 可查看 contract version/hash、装配状态与能力清单。Python 侧可调用
`PromptAuthoringValidator`/`PromptAuthoringContract.validate()` 校验候选 Prompt；报告只含
issue code、来源 ID、计数和 hash，不含候选正文。契约不会自动应用生产 Prompt，也不会通过
Prompt 文本授予工具、文件、Sandbox 或审批权限。

启用同一个开关还会注册只读 core 工具 `prompt_lab`，统一提供四种确定性操作：

```text
inspect  -> Fragment 元数据、来源、层级、token、hash、enabled 与冲突 ID（不返回正文）
generate -> 生成可人工审核的候选骨架，立即 evaluate；applied=false，不写 Registry/文件
compare  -> 比较 Fragment ID、唯一 source 或两段 inline instruction，复用生产冲突分析器
evaluate -> 用 PromptAuthoringValidator 检查候选，只返回 issue/ID/计数/hash
```

`prompt_lab` 不调用模型、不修改 Prompt、不扩大权限；`inspect`、`compare`、`evaluate` 的报告
默认内容轻量，只有 `generate` 会返回候选正文供人工审阅。主 Agent 和子代理使用各自的
Registry，因此子代理的 inspect/compare 不会泄漏父 Agent 的上下文现场。

generate/evaluate 使用封闭的 Candidate Prompt Schema v1；八个字段全部必填，未知字段、空正文、
非法 risk 或越界数组都会 fail-closed：

```json
{
  "target_fragment": "core:base:tool-discipline",
  "objective": "reduce unrelated file reads",
  "proposed_prompt": "...",
  "constraints": [],
  "expected_behavior": [],
  "risk": "low",
  "evaluation_cases": [],
  "rollback_plan": "..."
}
```

`risk` 只允许 `low/medium/high`；`evaluation_cases[]` 使用 `id/kind/metric/machine_verifiable`，
其中 `kind` 只允许 `success/regression/security`。目标 Fragment 已存在时，layer、scope、
stability、cacheability 与 Instruction authority 从 Registry 继承；新目标使用保守的
project/L6/session 默认值。`PromptCandidate.candidate_hash` 对完整候选内容确定且敏感，而
`evaluate` 只返回 candidate metadata hash、issue、计数和报告，不返回 Prompt/rollback/metric 正文。

REPL 内置命令：`/help`、`/history`、`/thinking`（展开最近一轮 Markdown 思考）、`/plan [任务|approve|cancel]`（生成/查看计划，批准后自动开启执行轮）、`/permissions [mode|add|remove ...]`（运行时模式与 session/workspace 规则）、`/tool-output [编号|last]`、`/queue [delete <n>|move <n> <m>|clear]`（查看/管理下一条消息队列）、`/tools`（当前工具池的 core/discoverable/deferred/loaded/excluded 状态）、`/session`、`/sessions [search <关键词>]`、`/resume <n|latest|path>`、`/fork [--turn <n>] [名称]`、`/rewind [n|list|fork <n> [名称]]`（回到任意轮开始前，联合恢复 history/compact checkpoint/文件/plan）、`/rename <名称>`、`/delete <n|path> [--force]`、`/mcp [reload|auth <server>]`（状态、热重载、OAuth + PKCE 授权）、`/agents [list|feed|pause|resume|kill|definitions|sidechains]`（子代理实时事件、控制与 sidechain）、`/jobs [list|output <id>|kill <id>]`（后台 Shell 进程与增量输出）、`/skills [关键词|reload|show <name>]`（发现、查看、热重载 Skill）、`/skill <name> [参数]`（用户主动调用 Skill）、`/plugins [reload|show <name>|install|update|enable|disable|remove ...]`（插件生命周期）、`/extensions [kind] [source]`（统一扩展来源与冲突）、`/lsp`（语言服务器状态）、`/web`（网页工具安全配置）、`/output-style [list|show <name>|reload|off|<name>]`（声明式输出样式）、`/compact [preview [--auto]|<focus>]`（立即压缩；预览边界或指定交接重点）、`/undo`（回滚上一轮文件改动）、`/undo list`、`/cost`（token 用量 + 估算成本 $）、`/model [provider/model]`（运行时热切换模型，预算联动）、`/allow`（旧版程序白名单兼容入口）、`/context [plan|why <id>|sources|rules|conflicts|authoring|fragments|enable|disable|toggle|reset|cache|ledger|refresh]`（请求 token 分项、来源归因、Prompt Authoring 状态、Fragment 启停、缓存边界与工作区重采）、`/memory [list [scope]|show|add <text> --scope|search|pin|unpin|forget|mode|review|migrate]`（分层记忆、审核与幂等迁移）、`/export [路径]`（Markdown；`.json` 输出可恢复快照）、`/stop`（中止运行中任务）、`/reset`、`/exit`。

所有命令由统一 `CommandRegistry` 发现：`/help`、slash 菜单、Tab 补全、运行态过滤和 InputProcessor 共用同一份 catalog。来源优先级为 `builtin > custom > skill > plugin > MCP`。用户/项目 Markdown Command 来自 `~/.krelcode/commands/*.md`、`<workspace>/.krelcode/commands/*.md`、`<workspace>/.commands/*.md` 与 `[commands].paths`；`$ARGUMENTS` / `${ARGUMENTS}` 渲染参数，`allowed-tools` 只能收窄工具集。允许用户调用的 Skill 会自动显示为 `/<skill-name> [参数]`，MCP Prompt 会命名空间化为 `/mcp__<server>__<prompt> [参数]`；`/skills reload`、`/mcp reload` 与 `/plugins reload` 会原地刷新对应来源，内置命令在命名冲突时保持最高优先级。

**运行中交互**：`Enter` 提交 Steering（当前 turn 的下一步看到）；`Alt+Enter` 将输入排为下一条独立消息，当前 turn 完成后自动发送；`Shift-Tab` 按 `default → plan → read-only → auto-approve` 快速轮换权限模式；`Esc` / `Ctrl-C` 中止当前任务（历史自动修复，可立即续聊）。空闲时 `Ctrl-C` 只清空当前输入，`Ctrl-D` 或 `/exit` 退出。审批请求由 REPL 前台异步接管，不会与字符级输入 reader 抢占 stdin；`/thinking`、`/plan`、`/permissions`、`/tool-output`、`/queue`、`/agents`、`/jobs` 在任务运行中仍可访问。

**终端 TUI（Step 83/84/93）**：输入框上方增加结构化 Panel Dock，默认包含 `Tasks`、`Activity`、`Context`、`Memory` 四个面板；Context 优先展示 `ContextAssembled` 最近一次真实请求的 token 分项、tool schema、cache hash 是否变化、memory 命中、dropped 来源、compact 水位与 checkpoint；Memory 条数使用低刷新频率的磁盘快照，panel redraw 不重复扫描。`Ctrl-O` 进入/切换面板；焦点在面板内时 `←/→` 换 tab、`↑/↓` 选择条目、`Enter` 或 `Esc` 返回输入框，方向键不会误改草稿。真实 TTY 同时启用 SGR 鼠标：可点击 tab 或面板条目。

- `!命令`：Shell 快捷模式。直接执行命令，不发起模型调用，也不写入模型 history；仍完整经过 `run_command`、审批策略、权限规则和运行时 Sandbox，输出进入现有可折叠工具面板。
- `#内容`：添加 project 记忆，保存到 `<workspace>/.krelcode/memory/entries.jsonl`；`#user: 内容` 写入跨工作区的 `~/.krelcode/memory/entries.jsonl`，`#session: 内容` 写入当前会话专属存储。敏感值在落盘与展示前脱敏。裸 `#` 打开 Memory 面板；`/memory list|scopes|add|remove|pin|unpin|search` 可查看、固定、搜索和维护记忆。
- Auto Memory 默认 `suggest`：从“请记住”、用户纠正、重复成功的测试命令、重复确认的架构模式和任务完成事实中提取候选；写入 `.krelcode/memory/candidates.jsonl`，执行脱敏、精确去重、冲突检测和证据累积。`/memory review` 审核接受/拒绝；`auto` 模式也只在置信度达到阈值且无冲突时写入 `project/knowledge`。模型生成内容永远不会成为 system instruction，`memory_search` 只读取已审核上下文。
- 记忆模型包含 `id/scope/kind/source/tags/pinned/created_at/updated_at`，kind 为 `note/preference/fact/workflow/knowledge`；作用域为 `user → project → session`（由宽到窄）。三者以有界、不可缓存的 runtime Fragment 注入，固定项优先，更具体作用域在提示中靠后且优先获得注入预算；内容明确标记为 `untrusted contextual knowledge`，不会修改 `AGENTS.md` 或伪装成用户/助手消息。
- 每作用域最多 200 条、单条 2,000 字符，总注入最多 12 条/4,000 字符；任务运行中会要求等待当前任务结束，避免中途改变当前提示词快照。旧 project JSONL 无需迁移即可读取。

行为规则按 `managed → ~/.krelcode/AGENTS.md → <workspace>/AGENTS.md → 目标目录 AGENTS.md/KRELCODE.md` 从约束到具体注入，分别生成 `managed:rules`、`user:rules`、`project:rules`、`directory:rules` Fragment；子代理定义使用 `agent_definition:<name>`。子目录规则只在当前轮明确涉及该目录时按需加载，不会递归扫描整个仓库；命中后进入 L3 cacheable 前缀，不会阻断后续 L4-L9 稳定块。规则文件中的独立一行 `@docs/team-rules.md` 会安全展开，支持嵌套导入、循环/重复检测，并拒绝作用域逃逸和 `.env`/凭据文件。`/context rules` 可查看作用域优先级、实际入口、导入和问题诊断，`/context why <rule-id>` 可查看最终生效来源。

模型需要澄清时可调用 `ask_user`：前台显示问题与选项，输入序号、完整选项或自定义文本即可继续。回答只进入 `tool_result`，不会伪装成新的用户消息。`/plan <任务>` 会进入只读 Plan 模式，让模型先调查并通过 `update_plan` 提交计划；该轮结束后计划标为待确认，`/plan approve` 恢复进入 Plan 前的权限模式并自动发起执行轮，`/plan cancel` 放弃执行。

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
headers = { Authorization = "Bearer ${MCP_TOKEN}" }
# listener_retries = 3       # GET 监听流断线重连次数（0=不重连）
# listener_retry_delay = 1.0 # 基础退避秒数（指数增长）

[mcp_servers.remote.oauth]    # 可选：Bearer/refresh/client credentials/交互授权
refresh_token = "${MCP_REFRESH_TOKEN}"
token_url = "https://auth.example.com/token"
client_id = "krelcode"
client_secret = "${MCP_CLIENT_SECRET}"
# access_token / authorization_url / scopes / resource / redirect_port ...

[mcp_servers.legacy]         # 旧 HTTP+SSE 规范：显式 transport = "sse"
url = "http://localhost:3001/sse"
transport = "sse"
```

- 按服务器 capabilities 自动注册：远程工具 + `list_resources`/`read_resource` + `list_resource_templates` + `list_prompts`/`get_prompt`
- `tools/list`、`resources/list`、`resources/templates/list`、`prompts/list` 全部自动跟随 `nextCursor` 分页；`notifications/*/list_changed` 会原子刷新当前服务器工具、Resource Template 或 Prompt 命令，单服务器失败不破坏旧目录
- OAuth 支持 token cache、refresh token、client credentials、protected-resource / authorization-server 元数据发现、动态客户端注册和 authorization code + PKCE。先配置再运行 `/mcp auth <server>`；HTTP 401/403 会刷新 token 并重试一次。缓存位于 `~/.krelcode/mcp-oauth.json`（目录和文件按用户私有权限写入，最终文件 `0600`）
- 热重载：REPL 内 `/mcp` 查看状态，`/mcp reload` 重读配置（增/删/改 diff 同步）；崩溃的服务器自动重启并重注册工具；配置中的 `${ENV_VAR}` 会在 HTTP Header/OAuth 字段中按环境变量展开，建议凭据只放环境变量或权限受限的本机配置
- `--no-mcp` 跳过启动；单服务器失败只警告不阻断

### repo_map

内置 `repo_map` 工具：Python 用 ast 精确提取类/函数/方法+行号（零依赖，不引 tree-sitter），JS/TS 用正则，20K 字符预算，LLM 可按行号接 `read_file` 精读。

默认工具 17 个：文件/检索与执行工具（`read_file`、`write_file`、`edit_file`、`run_command`、`grep`、`glob`、`repo_map`、`find_refs`、`check_syntax` 等）、`get_time`、`ask_user`、`update_plan`、`tool_search`，以及 `job_start/job_output/job_list/job_kill`；显式启用 `[prompt.authoring].enabled=true` 时再注册只读 `prompt_lab`。另注册 9 个子代理工具：`spawn_agent`、`wait_agent`、`send_to_agent`、`list_agents`、`get_agent_feed`、`pause_agent`、`resume_agent`、`kill_agent`、`list_agent_definitions`。启用 Skills 时另加 5 个 Skill 工具，并叠加 MCP 远程工具和插件工具。

### 动态工具目录与 ToolSearch

每个 turn 的 `TurnToolPool` 先按当前权限模式、REPL/headless/subagent 运行模式、功能开关、用户禁用列表、Skill allowlist 和 MCP 在线状态过滤 Registry，再按三级目录暴露：

- **core**：在 provider `tools` 参数中携带完整 JSON Schema，可直接执行；包括常用文件、搜索、命令、交互工具和 `tool_search`。
- **discoverable**：system 目录只列名称、单行描述和 read-only 属性，不占完整 Schema token；`repo_map`、`find_refs`、`check_syntax`、后台 Job、LSP、Web 等属于此层。
- **deferred**：默认不展开目录；MCP 与插件工具属于此层，模型用 `tool_search(query=..., load=true)` 检索并加载。

ToolSearch 只搜索已通过运行时过滤的候选集，不能加载被禁用、离线 MCP、当前 run mode 不支持或权限模式禁止的工具。新加载的工具从下一次模型请求开始进入 `tools`，同一 Step 不能执行；工具名、Provider schema、目录和搜索结果全部按名称稳定排序，注册顺序变化不会改变 Prompt Cache 前缀。`/tools` 会显示每个工具的 tier、loaded 状态和排除原因。

### 工具级 Prompt Playbook

具有 Playbook 的工具只有实际进入本轮 Provider `tools` 参数后，才会由独立 `ToolPlaybookCatalog` 注入对应的 L5 操作规则。Playbook 按工具族拆分（读取、仓库检索、编辑、命令验证、计划、ToolSearch、后台任务、子代理、LSP、Web、Memory），只渲染本轮真实可用的工具说明：被权限模式、运行模式、Intent、功能开关或 allowlist 过滤的工具不会泄漏其操作指令；discoverable/deferred 工具也只有在加载并进入下一次请求后才获得 Playbook。

Playbook Fragment 使用 `tool_playbook:<id>` 来源、`managed` 信任级别和 session 稳定层，可进入缓存前缀但不可压缩、不可手动切换。其 token 计入 Context 预算，`/context plan|why|cache` 可查看 active tools、playbook content hash 和缓存归属。工具池或 Provider 能力变化会同时改变 Playbook 内容与工具目录 hash，避免提示词描述的能力和真实 Schema 不一致。

`ToolPromptSpec` 是工具模型面向的唯一来源：Provider Schema、工具描述、操作指令、when-to-use/exclusion、示例和别名都从同一个对象派生。`Tool` 不再承担 Prompt 语义，`ToolDescriptor` 继续只负责 source、权限、tier 和运行模式；Registry 生成的 legacy spec 仍兼容没有显式 PromptSpec 的旧工具。Provider 只接收 `name/description/parameters`，操作说明由 PromptAssembler 放入 L5 Playbook；`wire_hash` 与 `content_hash` 可用于判断是 wire Schema 变化还是仅 guidance 变化。

### 任务类型 Workflow Fragment

每个用户 turn 在进入模型前只做一次确定性任务分类，并选择一个 L9 Workflow Fragment：`general`、`bug-fix`、`feature-implementation`、`refactor`、`code-explanation`、`code-review`、`test-verification` 或 `repository-exploration`。Workflow 提供该任务类型的执行顺序、完成条件和禁止项，例如 bug fix 必须先建立失败证据、定位根因、最小修复并做回归验证；code review 必须按风险排序、给出文件/行号证据且不得擅自修改代码。

Workflow 是 turn 级 `managed` instruction，不进入稳定缓存前缀，也不会被 compact 删除。分类只读取用户指令正文，忽略附件正文和 fenced code；显式“解释指定文件”的 TurnIntent 优先映射到 `code-explanation`。分类结果、置信度、命中信号、Workflow 版本与内容 hash 进入 PromptPlan/ProviderMessages 和 `/context why workflow:<id>`，方便解释某轮为什么采用了该执行策略。

### P2：差异化能力

- **LSP 客户端**：配置 `[lsp].enabled = true` 后，按扩展名懒启动语言服务器；提供
  `lsp_diagnostics`、`lsp_hover`、`lsp_definition`、`lsp_references` 四个只读工具。
  LSP 使用 stdio `Content-Length` framing，工作区外文件、敏感文件和超限消息会在
  发送给服务器前拒绝；单个服务器崩溃不会阻断主 Agent。
- **Output Styles**：从 `~/.krelcode/output-styles/*.md`、
  `<workspace>/.krelcode/output-styles/*.md` 与 `[output_styles].paths` 发现样式。
  样式只是带 frontmatter 的 Markdown 指令，不执行代码；项目同名覆盖用户样式，
  `/output-style <name>` 热切换，`/output-style off` 恢复默认。
- **可配置 Status Line**：`[status_line].template` 只支持
  `{provider} {model} {permission} {session} {cwd} {status} {tokens} {context} {branch}`；
  不执行 shell，高频刷新不扫描磁盘。
- **WebFetch/WebSearch**：内置安全 HTTP 客户端拒绝 localhost、私网、非公网 DNS、
  非 http/https、异常端口、敏感 Header 与超大响应；重定向逐跳复检。WebSearch
  通过 `SearchBackend` 解耦，默认 Brave，密钥只从 `[web_search].api_key_env` 读取。
  `--no-web` 完全关闭网页工具。
- **TypeScript SDK**：`typescript-sdk/` 提供零运行时依赖的 Node/TypeScript 客户端，
  复用 `stream-json` v1；支持并发 `requestId` 路由、流式 `query()`、`run()`、
  `interrupt()`、环境变量密钥与有界 stderr 脱敏。见 `typescript-sdk/README.md`。

### Agent Skills

Skill 是包含 `SKILL.md` 的指令/资源目录，兼容 Agent Skills 的文件系统约定。Krelcode 依次发现 `~/.agents/skills`、`~/.krelcode/skills`、`<workspace>/.agents/skills`、`<workspace>/.krelcode/skills`，随后追加 TOML `[skills].paths` 与 `--skills-dir`；后扫描的同名 Skill 覆盖前者。

```text
.agents/skills/release-notes/
├── SKILL.md
├── references/format.md
└── scripts/collect.py
```

```markdown
---
name: release-notes
description: Generate release notes from git history
argument-hint: <version>
allowed-tools: [Read, Grep, Bash]
---
# Release notes

Read $SKILL_DIR/references/format.md, inspect changes, and generate notes for $ARGUMENTS.
```

- **渐进披露**：system prompt 只注入名称+描述；模型匹配后调用 `list_skills` → `load_skill` 读取完整正文，避免一次性塞满上下文。
- **资源按需读取**：`list_skill_resources` 浏览目录，`read_skill_resource` 支持 `offset/limit`；拒绝路径逃逸、symlink 越界与纯凭据文件，`.env` 类内容自动脱敏。
- **脚本受控执行**：`run_skill_script` 使用参数数组而非 shell 字符串，支持 Python/Shell/Node/Ruby/Perl，剥离 API key 等敏感环境变量，超时终止进程组；默认走 ASK 审批。
- **调用控制**：`user-invocable` / `disable-model-invocation` 分别控制用户和模型能否调用；`allowed-tools` 作为执行指导展示。
- **热重载**：新增或修改 Skill 后执行 `/skills reload`，当前主代理和新子代理立即使用最新索引；`--no-skills` 完全关闭。
- **用户直调**：`/skill release-notes v0.55.0` 保留为通用兜底；每个 `user-invocable` Skill 也会自动注册为 `/release-notes v0.55.0`，直接加载完整指令并进入主循环。

### 扩展生态与 Plugin v2

Krelcode 用统一的 `ExtensionRegistry` 追踪 Plugin、Tool、Skill、Agent、Command、Hook 与 MCP 的来源、scope、版本及同名冲突；`/extensions` 可查看实时快照。插件仍是显式安装的本地可信扩展，不引入第二套执行协议。

```text
.krelcode/plugins/demo/
├── plugin.toml
├── plugin.py
├── skills/review/SKILL.md
├── agents/helper.md
├── commands/deploy.md
├── hooks.toml
└── mcp.toml
```

```toml
manifest_version = 2
name = "demo"
version = "1.0.0"
description = "Project extension bundle"

[tools]
entrypoint = "plugin:tools"       # 兼容既有 Tool 协议

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

Plugin v1 的 `entrypoint = "plugin:tools"` 继续兼容。插件工具命名为 `plugin__<plugin>__<tool>`，Markdown Command 命名为 `/plugin__<plugin>__<command>`；插件 Skill/Agent 进入现有管理器，Hook 带 `KRELCODE_HOOK_SOURCE` 与 `KRELCODE_PLUGIN_ROOT`，MCP 服务器默认命名空间化且显式配置可覆盖。

生命周期命令：

```text
/plugins install <path> [--scope user|project]
/plugins update  <path> [--scope user|project]
/plugins enable|disable <name>
/plugins remove <name>
/plugins reload
/extensions [plugin|tool|skill|agent|command|hook|mcp] [source]
```

安装会拒绝 symlink、路径逃逸、超大/超量包并原子替换；remove 移入插件目录的 `.trash`。启用状态持久化到 `~/.krelcode/plugin-state.json`，作用域键为 `user:<name>` / `project:<name>`，避免同名项目互相影响。单个 manifest/import/组件失败只影响该插件，不阻断主 Agent。

### 会话分支与交换

每个会话 JSONL 现在包含 `session_start/session_meta`、用户输入、工具与计划审计事件、取消记录、rewind 记录和完整 history 快照。`/sessions search` 搜索标题及历次用户输入；`/fork` 从当前历史分叉，`/fork --turn <n>` 或 `/rewind fork <n>` 可从任意已完成 turn 的 history + plan 分叉（不改变工作区）；`/rewind <n>` 则回到第 n 轮开始前并联合恢复 history、文件 checkpoint 与 plan/审批状态。`/rename` 写标题；`/delete` 二次确认后移入 `.archive`。`/export session.json` 生成可重新 `/resume session.json` 的 native JSON 快照，Markdown 导出仍适合分享。

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

### Stream JSON / Python SDK

持久进程模式使用方向固定的 JSONL 协议，stdout 只承载 `krelcode.stream-json` v1 消息：

```bash
printf '%s\n' \
  '{"type":"user","content":"解释 README","request_id":"r1"}' \
  '{"type":"control","subtype":"end"}' |
  krelcode --provider openai --input-format stream-json --output-format stream-json
```

同一 stdin 可连续发送多轮 `user`；`control/interrupt` 只取消当前 turn，`control/end`
先完成已排队的 user 消息再优雅退出。事件覆盖流式文本/思考、完整 assistant 消息、
工具调用、用户输入请求/结束、计划快照、权限、压缩、Hooks、子代理和唯一 `result`
终态；启动诊断全部走 stderr，所有输出在序列化前再次脱敏。单次 `-m` 与其它无交互
运行形态不会等待 stdin：`ask_user` 采用 fail-closed，立即把“未提供输入”作为工具结果
交还模型。

Python SDK 零第三方依赖，通过独立子进程复用完整 CLI，不复制 Provider/MCP/Sandbox 组装：

```python
import asyncio
from krelcode.sdk import KrelcodeClient

async def main():
    async with KrelcodeClient(workspace=".", provider="openai") as client:
        async for message in client.query("解释 README"):
            if message.type == "assistant" and message.subtype == "text_delta":
                print(message.get("text"), end="", flush=True)
        # 便利方法：消费到 result 并返回最终文本
        print(await client.run("列出未提交变更"))

asyncio.run(main())
```

SDK 支持并发查询请求路由、`client.interrupt()`、带 base64 图片的 `ImageInput`、完整
CLI 选项、超时/异常子进程清理。API key 不放进命令行或协议消息；SDK 子进程继承
Krelcode 原有的 `api_key_env` 环境变量机制。

### Hooks（事件钩子）

在配置文件里声明。Hooks v2 覆盖会话、输入、工具、压缩、子代理与轮次生命周期：

```toml
[[hooks]]
on = "file_edited"
command = "ruff format {path}"
tools = ["write_file", "edit_*"]
timeout = 30

[[hooks]]
on = "run_finished"
command = "osascript -e 'display notification \"任务完成\"'"

[[hooks]]
on = "pre_tool_use"
command = "python3 .krelcode/hooks/check_write.py"

[[hooks]]
on = "pre_tool_use"
type = "http"
url = "${HOOK_ENDPOINT}"
headers = { Authorization = "Bearer ${HOOK_TOKEN}" }
timeout = 10

[[hooks]]
on = "user_prompt_submit"
type = "prompt"
prompt = "Review this request against the project policy: {prompt}"
```

三种 Hook 共用同一决策管线：`command` 从 stdin 接收结构化 JSON；`http` 向仅限 `http/https` 的 URL POST JSON，Header 支持 `${ENV_VAR}`；`prompt` 复用当前 LLM 输出同样的 JSON decision/context。Plugin 的 `hooks.toml` 使用相同 schema，并自动继承 `_source/_plugin_root`。hooks 只能由用户配置或可信插件定义（`krelcode.toml` 与 `.krelcode/` 已列入写保护，agent 无法自我注入）。可用 `additional_context` 注入上下文、`system_message` 提示用户；`pre_tool_use` 可返回 `decision="block"` 或命令退出码 `2` 阻止工具。模板变量只展开一次并经 shell-quote；诊断记录不保存 Hook Header/Prompt 正文，HTTP URL 去除 userinfo/query/fragment，动态 Header 值会加入 SecretRedactor，输出、注入上下文与审计记录同样脱敏，失败/超时默认 fail-open。完整协议见 [CONFIG.md](CONFIG.md#hooks-协议hooks-v2)。

### git 自动提交（可选）

`--auto-commit`：每轮对话结束若工作区有变更，自动 `git add -A && commit -m "krelcode: <你的输入>"`（Aider 风格）。`.krelcode/` 运行时目录自动写入 `.git/info/exclude`（仅本地生效）。与 `/undo` 互补：checkpoint 管文件级回滚，auto-commit 管历史留痕。

### 检查点与回滚

每轮对话自动为 write_file/edit_file 的目标文件做修改前快照（`<workspace>/.krelcode/checkpoints/`）。`/undo` 恢复上一轮；`/rewind <n>` 回到任意第 n 轮开始前，按 checkpoint 逆序恢复该轮及之后的所有文件，并同步回退 history 与 plan/审批状态。checkpoint meta 带 session/turn 身份，跨重启仍可恢复，且不会误用其他会话的同名 turn。`--no-checkpoint` 关闭。（注：run_command 的文件修改不在追踪范围。）

### 交互式子代理 / Agent Definitions / Sidechain

LLM 可用 `spawn_agent` 把自包含子任务委派给后台子代理（独立上下文、并行执行；`isolated=true` 时使用独立 git worktree 分支，`fork_context=true` 时继承父对话）。子代理不能再派生子代理（防递归）。

- **Agent Feed**：`get_agent_feed(agent_id, after_sequence)` 按稳定游标读取 assistant/thinking/tool/state 增量；内存有界，所有文本先脱敏。
- **Pause / Resume / Kill**：`pause_agent` 在每个 step 安全边界协作式暂停并保留 Task/历史，`resume_agent` 从原位置继续，`kill_agent` 才取消整个 Task；`send_to_agent` 在暂停期间仍可排队。
- **用户定义**：在 `~/.krelcode/agents/<name>.md` 或 `<workspace>/.krelcode/agents/<name>.md` 放置 frontmatter Markdown；项目级同名定义覆盖全局。`spawn_agent(definition="reviewer", task="...")` 会应用定义中的 system prompt 和 `allowed-tools`（只能缩小工具集，不能绕过审批/Sandbox）。
- **独立运行策略**：Definition 可用 `model: provider/model` 创建独立 Provider，用 `permission-mode: default|plan|read-only|auto-approve` 固定子代理权限，并用 `mcp-servers` 选择 MCP 工具集合；省略表示继承全部，`["*"]` 显式全选，`[]` 表示不使用 MCP。未知 provider/server/tool 在启动前 fail-closed。
- **Sidechain**：每个子代理写入 `.krelcode/sidechains/<run-id>/<agent-id>.jsonl`，包含定义、任务、消息、feed、状态、history 快照、结果和 worktree/分支信息。它不会自动污染父 Agent history。
- **REPL**：`/agents` 查看状态；`/agents feed <id> [after] --limit N` 读取增量；`/agents pause|resume|kill <id>` 控制；`/agents definitions` 查看定义；`/agents sidechains` / `sidechain <id>` 审计持久化记录。

Definition 示例：

```markdown
---
name: reviewer
description: 只读代码审查
allowed-tools: [read_file, grep, glob, repo_map]
model: deepseek/deepseek-flash  # 可选；省略则跟随父代理当前 provider/model
permission-mode: read-only     # 可选；独立于父代理
mcp-servers: [github, docs]    # 可选；* = 全部，[] = 不使用 MCP
fork-context: true
---
你是严格的代码审查员，只报告可复现的问题并给出文件路径与行号。
```
文件与命令操作限定在工作区内（相对路径基于 `--workspace`），自动跳过 `.git`/`node_modules` 等目录。

安全策略（默认开启）：`read-only` / `plan` / review 在策略层强制拒绝有副作用的工具，不能通过审批或 allow 规则绕过；只读命令与常规文件写入静默放行，但 `.git/`、`.env*`、云凭据、token、Terraform state、密钥文件、审批白名单自身等**受保护路径**的写入需要审批；执行代码/删文件/网络访问等 ASK 级操作通过 `ApprovalBroker` 在前台弹 `[y]是/[n]否/[a]始终允许`；"始终允许"只保存规范化后的**精确命令**，避免一次批准被复用到同程序的复合/包装命令。`/permissions` 支持 `allow / ask / deny` 与 `session / workspace` 两级作用域，可匹配 `path` glob、精确 `command`、`command_program` 和 `tool`，优先级固定为 `deny > ask > allow`；显式 `ask` 不会被 auto-approve 绕过。session 规则仅驻留内存，workspace 规则与旧版 `/allow` 规则持久化到 `<workspace>/.krelcode/allow_rules.json`；后台子代理在 REPL 空闲时产生的审批也会被接住。`sudo`、`rm -rf /`、`curl|sh`、`env -S 'sudo ...'`、`find -exec sudo ...` 等 DENY 级无条件拒绝；`find -exec/-delete`、`env <command>`、`git branch/tag/remote` 写操作、`sort --compress-program`、`rg --pre` 与 `--output/-o` 文件写入选项均至少升级为 ASK。文件工具拒绝工作区逃逸、符号链接切换、硬链接 inode 和只读覆盖；写入采用同目录原子替换，edit 通过文件快照阻止并发覆盖。headless 无审批前台时一律 fail-closed；`--auto-approve` 自动批准 ASK 级但不能越过 DENY、显式 ask 规则或受限权限模式，`--no-sandbox` 同时关闭审批与运行时隔离。

运行时 Sandbox 为命令和 Skill 脚本增加第二道边界：macOS 自动使用 Seatbelt，Linux 检测到 bubblewrap 时使用 bwrap；工作区可写、系统运行时只读、用户 Home 不可读，网络默认关闭。无 OS 后端时降级为 `static` 硬性审计（越界/敏感路径、硬链接、`sh -c`/`eval`、`env`/`find -exec`/`xargs`/`rg --pre` 包装递归、命令替换、已知网络命令），并与文件工具共用同一套凭据路径分类；但静态模式无法抵御任意解释器代码中的动态绕过，启动日志会明确告警。命令环境中的 API key/token/proxy/SSH agent 会被剥离，HOME/TMP/cache 重定向到 `.krelcode/sandbox/`；`grep/glob/repo_map/find_refs/check_syntax` 统一拒绝 symlink 越界。可用 `[sandbox]` 或 `--sandbox-backend/--sandbox-network` 调整。

上下文管理（两道防线）：CJK 感知 token 估算；超过 `--context-tokens`（默认按模型上下文窗口×75% 自适应，未知模型 10 万）先**驱逐旧工具输出**（免费、保配对结构，只留最近 6 条），仍超限再把中段交给 LLM 摘要压缩（`--no-compress` 关闭，`/compact` 手动触发）；超长工具输出全文落盘 `.krelcode/tool_outputs/` 并只在上下文留引用。后台命令通过 `job_start` 启动在独立进程组中，不随单轮 turn 中止；`job_output` 用单调 sequence 游标读取实时增量，`job_kill` 终止整个进程组，任务结束会向 REPL 推送完成通知，完整脱敏日志保存在 `.krelcode/jobs/`（默认 10 MiB 上限）。

体验：启动显示 provider/模型/工作区/工具/会话信息卡片；输入区用横跨终端的上下两条 `─` 边界包住 `❯` 提示符，输入 `/` 时实时弹出并过滤匹配的 slash commands（Tab 可补全唯一命令），候选菜单在固定区域原地覆盖，撤销 `/` 后不会残留搜索内容；字符级编辑器支持左右/Home/End/Delete、Ctrl-A/E/B/F、按词移动删除、多行编辑、Bracketed paste（多行代码一次粘贴且不执行终端转义）和 `Ctrl-R` 增量历史搜索；`↑/↓` 调取持久化历史，越过最新一条会恢复未提交草稿。历史默认写入 `~/.krelcode/history.jsonl`（权限 `0600`、提交前脱敏，可用 `KRELCODE_HISTORY_FILE` 覆盖）；`Alt+Enter` 入队的内容可通过 `/queue` 查看、删除、移动或清空。回答结束后立即回到新的双边界输入区；LLM 回复以 `⏺` 流式展示，reasoning 模型（DeepSeek-R1/Qwen 思考模式/Claude thinking）的思考正文默认折叠为单行暗色 `✻ Thinking…（正文已折叠；下方为工具活动，/thinking 展开）`，并用空行与后续工具活动明确分隔，执行 `/thinking` 才展开（任务运行中也可用）原始 Markdown（标题/列表/空行/fenced code 均保留），工具调用/结果使用 `⏺` / `⎿` 树形标记（统一 ThinkingDelta 事件，思考不进对话历史）；文件读取对 `.env*` 与 `krelcode.toml` 保留结构但脱敏，`credentials*`、`token.json`、云凭据、私钥和 state 文件直接拒读，glob/grep/repo-map 同样跳过；工具结果默认只显示编号与行数/字符数摘要，运行中或空闲时可用 `/tool-output` 查看列表、`/tool-output <编号|last>` 在灰色背景面板中单独展开，超宽行自动折行；高置信度识别到“只解释明确文件”时，会在模型调用前注入结构化意图并只暴露 `read_file`，避免先 glob 目录或读取 README/同级文件，模糊请求和修复/运行等动作请求保持完整工具集；edit_file 展开后的 diff 保留红绿高亮（尊重 `NO_COLOR`）；会话自动录制为 JSONL（`<workspace>/.krelcode/sessions/`，审计事件流 + 历史快照），`--resume` 恢复最近会话继续聊（`--resume <path>` 指定文件，`--no-session` 关闭）。

所有终端输入与 `-m` 单次输入在进入会话前经过统一 `InputProcessor`：规范化原始输入、区分普通消息/业务命令/展示命令/控制命令、后台展开 `@文件` 与图片附件，并执行可选 `user_input` Hook；REPL 只消费结构化 `InputAction`，不再承担输入解释。`user_prompt_submit` 则位于 AgentLoop 查询入口，保证 REPL、headless 与子代理一致执行。会话现场由 `SessionViewState` 显式记录 idle/running/stopping、当前 turn、查询阶段、工具计数与进度；`SessionEventReducer` 统一折叠 QueryEvent，`ReplSessionRunner` 独占 query/consumer/审批 watcher/下一条队列。运行中输入框下方持续显示阶段、Step、工具进度与耗时，渲染器暂停时保留用户草稿和光标。工具增量输出先进入折叠缓存，终端正文不自动展开；历史消息仍只由 AgentLoop 持有。

启动时自动装配工作区上下文（Context v3）：user/project 分层规则、git 分支/状态与目录树统一进入 `[context].max_tokens` 预算，按规则 > git > tree 的优先级保留，超限时截断或省略低优先级来源；`--no-context` 可关闭，长会话中 `/context refresh` 可重采。请求级 assembly 已统一走 `PromptFragmentRegistry + ContextAttachmentStore + ContextSource → PromptAssembler → PromptPlan → ProviderMessages`，其中可插拔 `ContextSource → ContextAssembler` 负责来源收集和兼容归因：默认来源包括 Prompt Snapshot、按需目录规则、动态工具目录、Turn Intent 与 Hook runtime context，`PromptAssembler` 再按固定 L0 Managed Policy → L12 Per-turn Reminder 分层，稳定/会话内容在前、turn 内容在后，动态失败与非法层彼此隔离；`ContextAssembly` 保留为 `/context`、Resume 与 checkpoint 的兼容归因视图，system message 只进入 provider-bound 请求，不改持久历史。稳定 system 前缀由 `core:base` 与 `workspace:rules` 等 Fragment 治理；动态环境改为独立 attachment：`environment_snapshot`、`environment_delta`、`date`、`skill_catalog_delta`、`agent_catalog_delta`、`mcp_instructions_delta`、`token_budget_reminder`、`mode_permission_contract`。`mode_permission_contract` 是唯一的 L5 权威契约，由同一份 RuntimeSnapshot、过滤后的 ToolPool、ApprovalPolicy 与 ExecutionSandbox 生成，明确 run/permission mode、审批优先级、计划门禁、工具 allowlist/MCP scope、Sandbox backend/网络/写边界；运行中切换模式会重建工具池并只改写 L5 dynamic block，不会污染 stable prefix。`/context why attachment:mode_permission_contract` 与 `/context ledger` 可查看无正文 hash、排除计数和实际权限边界。每个 attachment 有固定 ID、revision、来源和语义指纹，内容不变时 revision 不变；git/tree、目录 catalog、日期或模式变化只替换对应 block，不会改写稳定 prefix。`/context attachments` 查看 revision/generation/token，`/context sources` 查看实际装配诊断，`/context rules` 查看规则入口/@import 诊断，`/context fragments [source]` 查看稳定 Fragment，`/context enable|disable|toggle <id|source>` 手动启停稳定 Fragment，`/context cache` 显示全部 stable/volatile block 与哈希。Anthropic adapter 使用该边界生成两个 system block（稳定前缀带 `cache_control`、动态尾部不带）；Step 99 起 `ProviderMessages` 直接携带 `stable_system_blocks/dynamic_system_blocks`，Anthropic 原样保留每个 stable block，只在最后一个 stable block 设置 `cache_control: ephemeral`，不再提前 join；DeepSeek/OpenAI 兼容端点按 stable → dynamic 固定顺序合并为一个 system message，但继续记录 block 边界和 stable/dynamic hash。Step 88 起，支持该可选能力的 Provider 会先构造一次不可变的 `PreparedLLMRequest`，AgentLoop 用同一份 wire body 计算预算并执行请求：Anthropic 的 system/tools/messages 三个显式缓存断点、OpenAI/DeepSeek 的隐式前缀缓存机会、Gemini 的无显式缓存状态都会转成无正文 segment 元数据；请求体字节数、wire token、payload hash、provider 输出上限与 `input_fits` 同时进入快照。压缩判断不再只看 history，而是使用完整请求输入加输出预留，并同时约束用户输入预算与模型窗口；Step 89 后真实请求超限时先做工具微压缩并重算，仍超限才 compact/重新装配一次，避免循环压缩。旧 Provider 缺少 prepare 能力时自动回退到 estimated 快照。每个模型 Step 发请求前还会发布精简 `ContextAssembled` 快照：按 system/workspace/memory/history/runtime/tool schema/overhead 拆分 token，并携带预算、模型窗口、cache boundary、cache segment、stable/dynamic block 数与 stable/dynamic/full hash；快照不包含 Prompt 正文、headers 或请求体原值，可安全进入 TUI、JSONL、stream-json 与 SDK。`/context` 优先展示最近一次真实请求及其 wire 保真度，`/context cache` 展示显式/隐式缓存段归因。`/compact preview [--auto]` 可在不修改 history 的前提下查看 head/compact/keep 边界、工具驱逐数、高低水位、来源节省量与待摘要消息；`/compact` 执行后再报告实际策略、generation、checkpoint 与边界。Step 90 将自由摘要升级为可机器校验的 `CompactSummary v2`：目标、决策、文件、命令、测试状态、错误、未完成项和下一步以稳定 JSON 交接；连续 compact 会合并旧摘要而不是覆盖，模型失败时由 Working Context/history 生成降级摘要。自动压缩只在 turn 边界切割并保护当前 turn，按高水位触发、向低水位目标选择安全切点；同一摘要不会被重复摘要。compact 前完整 history 原子写入 `.krelcode/compact-checkpoints/` 并脱敏，`/rewind` 可恢复 compact 前现场。Step 89 增加会话级 `WorkingContextStore`：当前目标、计划、读写文件、关键命令、错误、决策和重要观察以有界、脱敏、可去重的 `<working_context>` 动态 runtime layer 注入，不进入长期 Memory，也不污染 prompt cache 稳定前缀；`/fork`、`/resume`、`/rewind` 从保留历史重建，`/reset` 清空。真实请求超预算时先执行零 LLM 的 Tool Microcompact：最近工具、当前轮、错误、写入/编辑确认、plan/ask_user 结果受保护，较旧大输出依次变成结构化短摘要和单行全文引用，随后重新装配并重算预算；只有仍超限才进入 LLM 摘要。`ContextMicrocompacted` 只携带计数/token 级元数据，已贯通 `/context`、TUI、JSONL、stream-json。同批工具调用并行执行，同一调用连续失败 3 次判定卡死自动中止（`--repeat-failure-limit` 可调）。

Step 93 进一步把 Context/Memory 变成可直接操作的会话能力：`/context plan|why|sources|cache` 从最近真实请求快照做内容无关归因，`/memory list|show|add|search|pin|forget|review|migrate` 统一管理三层记忆与 Auto Memory 候选；`/memory migrate` 会脱敏、去重、补全 schema、保留 pinned 并在真实变更前备份，重复执行为幂等。REPL、headless、stream-json 和 SDK 共用同一套 slash command 语义；stream-json 新增 `context_assembled/context_dropped/memory_loaded/memory_candidate/memory_written/compact_checkpoint` 稳定事件名，命令可直接执行且仍返回唯一 `result`。TUI Context 面板同步展示 memory 命中、dropped 来源、cache hash 变化和 compact 水位，面板高频刷新只读缓存，不重复扫描磁盘。完整操作、存储格式和迁移说明见 [docs/context-and-memory.md](docs/context-and-memory.md)。

Step 94 将 Context v3 设为默认并完成安全收口：`[context].engine = "v3"`（`legacy`/`v2` 仅保留一个版本回退）；每个 `ContextLayer` 带 `trust` 与 `origin`，`/context`/plan/token snapshot 都暴露 engine 与信任级别。Memory 不再进入 system Prompt Registry，而由独立 `MemoryContextSource` 以 `trust="untrusted"` knowledge layer 注入并做标签转义；Working Context 与 Hook runtime context 同样使用 data-not-instructions 边界。高置信度“只解释指定文件”会在 v3 装配阶段过滤 git/tree，避免无关工作区内容占用 token。回归覆盖 Prompt Injection、Memory Poisoning、symlink/path traversal、大文件/损坏文件/权限错误、compact 取消与摘要失败、长会话 compact、Memory 检索质量、跨项目隔离、Resume 刷新和预算总量守恒。

Step 102 将 Memory 从独立“knowledge 特例”收敛为统一 Evidence：Instruction 来源固定为 Managed / User / Project / Agent Definition；Memory / Web / MCP / Tool Result / Retrieved Document 只能进入 Evidence。Memory 使用 `[Evidence: Memory]` envelope，携带 source/scope/confidence/requires-validation/instruction=false 与来源元数据，Resume、子代理和 `memory_search` 复用同一格式；MCP/Web 等非 Memory Evidence 继续使用转义后的 `<evidence_set role="non-instruction" ...>`。PromptAssembler 现在产出独立的 `EvidenceChannel`：Evidence 固定位于 L7、turn stability、非 cacheable，不再进入 system prompt，也不影响 `stable_hash/dynamic_hash`，只改变独立 `evidence_hash` 与最终请求 digest。Provider wire 会把 Evidence 附着到本轮锚定 user turn：OpenAI-compatible 使用兼容的 text content，Anthropic 使用独立 text block，Gemini 使用独立 text part；工具循环的后续 step 继续携带同一证据，但不会污染 tool result 或创建额外 synthetic turn。`/context why memory:scoped` 会明确显示 `role=evidence`、`instruction=false` 与 `requires-validation=true`，诊断元数据仍不包含记忆正文。

Step 103 增加内容轻量的 Prompt Plan 可观测性：每个 Fragment 记录 source/scope/tokens、stable/volatile 位置、cache boundary 与 content hash；请求快照同时记录工具目录 hash、Memory 命中来源、所有已知裁剪项及原因，以及相对上一真实请求的 Fragment/hash/token diff。`/context plan`、`/context why <fragment|memory|omitted-id>` 与 `/context cache` 形成一致的诊断入口，持久化到 Context checkpoint 的内容仍不包含 Prompt、工具 Schema、history 或 Memory 正文。

Step 104 统一 Context / Cache Ledger：每次最终 Provider 请求（包括 microcompact/compact 后重装配）记录 stable/dynamic/instruction/evidence hash、tool catalog hash、attachment generation/revision、compact checkpoint、Working Context sequence、Memory evidence IDs、history digest、Provider request digest 与最终 complete request hash。`/context ledger` 直接给出 cache miss 及 tool/environment/Memory/history/working-context/compact/provider 等机器可读原因；Ledger 随 ContextPlan/checkpoint 持久化，Resume 记录 workspace/file/Memory/Working Context invalidation，子代理 sidechain 记录自己的 Ledger 并比较 parent policy instruction hash。`context_assembled` JSONL/stream-json 事件同步携带该内容无关 Ledger，记录中不含 Prompt、工具 Schema、history、Memory 或请求体正文。

Step 110 增加确定性的 Prompt Conflict Analyzer：PromptAssembler 在最终 L0-L12 排序后识别直接否定、显式对立值与条件冲突，并按照 `Managed → L5 Mode Contract → L1 Base → Directory → Project → User → Style → Working Context → Turn Reminder` 裁决权威；同权规则明确标为 ambiguous，不按 Fragment ID 随机覆盖。分析器支持中英文与代码围栏过滤，使用主题候选索引和有界缓存，不调用模型。`/context conflicts`、`/context why conflict:<id>` 和 `/context plan` 可查看冲突来源、winner/resolution、置信度和 hash，但报告、Snapshot 与 Provider channel 均不保存指令正文；`conflict_hash` 也与 Provider 请求缓存 hash 解耦。

Step 111 增加 Prompt Release 与模型 Profile 校准闭环：Base section 和 Profile catalog 被归一化为不可变 `PromptRelease` 身份；`ModelProfileCalibration` 只能引用真实 live Benchmark baseline，并绑定 release/profile/contract/behavior-suite/provider/model。Calibration 是内容无关证据，不进入 System Instruction。运行时无匹配证据显示 `uncalibrated`，Release/contract/suite 变化显示 `stale`；Baseline schema v2 和 Release gate 对上述任一漂移 fail-closed。主 Agent、`/model`、子代理、`/context why`、Benchmark report/baseline 共用同一身份链，持久化记录不包含 Prompt、答案、工具参数或密钥。

Step 112 增加可执行最终验收：`benchmarks/acceptance.py` 冻结 17 个 requirement、12 个 capability area，检查静态证据、生产对象运行时探针、9 个离线执行面和 fresh live authority。Live Gate 不能只凭 `passed=true` 注入，验收器会独立核对 source report、真实 provider、冻结 contract/suite、16-case/3-trial manifest、case stability/pass@k、runtime error、时间窗以及 gate/report 生成时序。最终报告执行第二次内容轻量自检；异常文本、Prompt 正文、答案、工具参数、会话正文和凭据形状内容会被拒绝并从报告中清除。

Step 113 增加可选 `PromptAuthoringContract`：`[prompt.authoring].enabled=true` 时才装配
`prompt_authoring:contract` L6/session Fragment，主 Agent 与子代理共用同一份契约。它把
Prompt 目标分析、Instruction/Evidence 分权、stable/dynamic 缓存边界、冲突检测、工具权限
设计和机器可验证性变成确定性检查；`PromptAuthoringValidator` 复用生产
`PromptConflictAnalyzer`，并要求 success/regression/security 三类可判定用例。验证报告只含
issue code、来源 ID、计数与 hash，不保存候选 Prompt 正文；契约不会自动应用修改，也不会
授予新工具、文件访问、Sandbox/审批绕过或额外指令权威。

Step 114 增加同一开关下的只读 `prompt_lab(action=inspect|generate|compare|evaluate)` 工具。
它把 Prompt 工程操作收敛为一个稳定 ToolPromptSpec，复用现有 Fragment Registry、
PromptConflictAnalyzer 与 PromptAuthoringValidator；inspect 只返回元数据与 hash，compare
支持 Fragment/source/inline 三种操作数，evaluate 返回内容轻量报告，generate 只生成
`applied=false` 的可审阅候选。工具默认关闭、无文件写权限，Plan/read-only 模式下仍可安全
使用；主/子代理的 ToolContext 分别绑定自己的 Prompt Registry。

Step 115 将候选 Prompt 从旧 authoring dict 提升为正式 `PromptCandidate` 契约，并把
`prompt_lab.generate` 的输出切换为 Canonical Candidate Prompt Schema。解析器严格校验八个
必填字段、risk 枚举、有界字符串数组和 success/regression/security evaluation cases；
已存在目标 Fragment 时自动继承层级、作用域、stability、cacheability 与受信来源。确定性
`candidate_hash` 对正文变化敏感，而 evaluate 使用 content-light metadata。旧
`PromptAuthoringRequest` evaluate 输入继续兼容，但所有路径仍保持不写 Registry、不应用候选。

## 模块结构（依赖严格单向）

```
core/    wire-free 规范消息·事件总线·错误（零依赖内核）
llm/     统一 Provider 机制：transport+SSE / OpenAI·Anthropic·Mock 适配器 / 工厂+配置
agent/   AgentRuntime / AgentLoop / Fragment Registry / compact（主循环只依赖端口）
tools/   Tool 端口 + Registry + builtin/（每工具一个文件）
jobs/    后台 Shell Job Manager（进程组、实时输出、游标、kill/list、日志与完成事件）
skills/  SKILL.md 发现/解析、渐进加载、资源、脚本、热重载
subagent/ 独立 AgentLoop、Definition、实时 Feed、pause/resume/kill、sidechain
plugins/ plugin.toml 发现、隔离加载、Tool 命名空间、热重载
safety/  ApprovalPolicy 端口（Step 6 加沙箱）
session/ InputProcessor + commands registry/handlers + SessionController + SessionViewState + runner/恢复/JSONL/检查点
entrypoints/ 进程入口：cli 轻量 fast-path → main 完整参数路由 → app 按需启动
ui/      终端 I/O、Markdown 渲染和展示型命令（不承担会话业务状态）
app.py   组装根：只创建对象并选择运行策略，不实现会话/执行流程
```
