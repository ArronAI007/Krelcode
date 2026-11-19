# Krelcode 设计文档（v1，已确认的技术决策）

参照 pi（分层包）与 DeepSeek Harness（typed events）的解耦思想：
**pi 式分层骨架 + 轻量事件总线**。不做插件微内核（过度设计）。

## 已确认的技术栈决策

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 语言 | Python 3.12（Anaconda），pytest 测试 |
| 2 | 运行时依赖 | 零第三方依赖（urllib/dataclasses/tomllib/asyncio/zoneinfo）；依赖被锁在适配层内 |
| 3 | 并发模型 | **asyncio 全面异步**；LLM 接口为 `stream() -> AsyncIterator[LLMEvent]`；阻塞 IO（urllib、input、subprocess）经 `asyncio.to_thread` / async subprocess 桥接 |
| 4 | 解耦强度 | 分层包 + 事件总线；agent 循环只依赖端口（接口），具体实现由组装根注入 |
| 5 | 配置/会话 | TOML 配置 + JSONL 会话 |
| 6 | 工具协议 | OpenAI function-calling 格式为内部规范，provider 差异在 llm 适配层消化 |

## 模块结构与依赖规则

```
src/krelcode/
├── core/        # 内核：零依赖，只有数据类型与事件总线
│   ├── messages.py   # Message / ToolCall（含 OpenAI 格式互转）
│   ├── events.py     # AgentEvent 家族 + EventBus
│   ├── hooks.py      # HookPort / HookResult：agent 只依赖的窄端口
│   └── errors.py     # KrelcodeError / LLMError / ConfigError
├── llm/         # 统一 Provider 机制（模型适配层）
│   ├── base.py       # LLMProvider/PreparedLLMRequest 契约、LLMEvent、finish/usage、collect()
│   ├── transport.py  # HTTP 传输抽象(urllib→asyncio桥) + SSE 解析器
│   ├── registry.py   # Provider 工厂：名字+配置 → 实例；resolve_settings 优先级解析
│   └── providers/    # 每个 adapter 独占一种 wire format
│       ├── openai.py     # OpenAI 兼容：SSE 流式、tool_call 增量聚合
│       ├── anthropic.py  # Anthropic：content blocks、tool_use/result、system 抽离
│       └── mock.py       # 脚本化响应，记录调用（测试/离线体验）
├── agent/       # 执行推进层：只 import core/端口，不认识具体 UI/Provider
│   ├── runtime.py    # AgentRuntime：turn/steering/cancel/close 统一门面
│   ├── loop.py       # AgentLoop：单轮模型-工具循环
│   ├── context.py    # Context v3 配置：分层工作区上下文与 token 预算
│   ├── tool_pool.py  # TurnToolPool：每轮工具快照与动态目录
│   └── prompts.py    # 系统提示词组装
├── tools/       # 工具层（实现 Tool 端口）
│   ├── base.py       # Tool 协议 + ToolResult + ToolContext
│   ├── registry.py   # ToolRegistry -> OpenAI tools schema
│   └── builtin/      # 每个工具一个文件，互相独立
├── skills/      # 文件系统型 Agent Skills：发现/解析/渐进加载/资源/脚本
├── plugins/     # 轻量本地插件：manifest → 隔离 import → Tool 适配/命名空间
├── safety/      # 策略层（实现 ApprovalPolicy 端口）
│   └── policy.py     # Decision / AllowAllPolicy（Step 6 加沙箱与交互审批）
├── session/     # 交互会话层 + 状态适配器
│   ├── input.py      # InputEnvelope → InputProcessor → InputAction
│   ├── attachments.py # @文本/图片展开（工作区约束与预算）
│   ├── state.py      # SessionViewState：idle/running/stopping 与 turn 现场
│   ├── commands/     # CommandSpec/Registry → dispatcher/handlers
│   │   ├── builtin.py # 29 个内置命令元数据与 help 渲染
│   │   ├── registry.py # 解析、别名、隐藏/启用、运行态与来源冲突
│   │   ├── sources.py # Skill / MCP Prompt → dynamic CommandSpec
│   │   └── handlers/  # 会话、上下文、Skill、MCP 等执行器
│   ├── controller.py # SessionController：命令/任务/steering，不依赖终端
│   ├── runner.py     # headless/REPL 运行策略，不导入具体 UI
│   ├── restore.py    # 会话恢复
│   └── recorder.py   # JSONL 持久化（事件总线订阅者）
├── ui/          # 终端交互：输入、渲染、展示型命令
│   └── repl.py
├── entrypoints/ # 进程入口：light cli fast-path → full main → lazy app
│   ├── cli.py   # --version 零配置/零 Agent 依赖；其余 argv 原样下发
│   └── main.py  # 完整参数解析 + doctor/init/review/REPL 路由
├── app.py       # 组装根(composition root)：唯一知道所有具体类的地方
└── __main__.py  # 与控制台脚本共用 entrypoints.cli:main
```

依赖方向（严格单向）：

```text
InputEnvelope → session/input → InputProcessor → InputAction queue
                                      │
entrypoints → session/runner → session/controller → agent.runtime → ports/core
                                                        ↑
ui ──动作消费/事件总线/渲染──── AgentLoop ──────────────┘

llm/tools/mcp/skills/safety/session persistence = 可替换适配器
app.py = composition root，只负责创建与注入，不属于业务层
```

`session.input` 只依赖 core/session，`session.controller` 不导入 `ui`，`ui` 不导入
`app/entrypoints`，`core` 不依赖任何上层。
这些边界由 `tests/test_architecture.py` 的 AST 依赖守护测试固定。

## 四个核心端口（契约）

```python
# llm/base.py —— 模型端口
class LLMProvider(Protocol):
    name: str; model: str; capabilities: ProviderCapabilities
    async def stream(self, messages: Sequence[Message],
                     tools: Optional[Sequence[ToolSpec]] = None) -> AsyncIterator[LLMEvent]: ...
# LLMEvent = TextDelta(text) | StreamFinished(message, finish_reason, usage)

# tools/base.py —— 工具端口
class Tool(Protocol):
    name: str; description: str; parameters: dict   # JSON Schema
    read_only: bool = False                         # 调度/审批能力标注
    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult: ...

# safety/policy.py —— 审批端口
class ApprovalPolicy(Protocol):
    async def check(self, tool_call: ToolCall, ctx: ToolContext) -> Decision: ...
# Decision(allowed: bool, reason: str)
# 实现：AllowAllPolicy（--no-sandbox）/ SandboxPolicy（默认）：
#   · 只读工具(get_time/read_file/grep/glob)放行；write/edit 由路径约束兜底放行
#   · run_command → safety.commands.classify_command()：DENY/ASK/ALLOW 三级
#   · run_command 子进程执行前剥离敏感环境变量（*_API_KEY/*_TOKEN/*_SECRET 等），
#     HOME/TMP/cache 重定向到 `.krelcode/sandbox/`，防 `env` 顺手读出宿主凭据（Step 44/52）
#   · 敏感文件读保护：read_file 拒读纯密钥文件、对 .env/krelcode.toml 脱敏，
#     grep 跳过敏感文件（Step 45）
#   · 输出/历史脱敏：SecretRedactor 持有已解析 key，renderer/recorder/-m 输出
#     统一 replace 为 ***（Step 46）
#   · 上下文级脱敏：工具结果进入 loop.history/回传模型前先经 SecretRedactor
#     处理，堵住明文 key 随 run_command(cat config) 等路径泄入 LLM 上下文的漏洞（Step 49）；
#     子代理独立历史同样经 redactor（SubagentManager spawn 注入，Step 50）
#     - 按控制符切段(punctuation_chars)逐段分级，整条取最严格；重定向升级为 ASK
#     - 无条件 DENY：sudo/dd/mkfs/shutdown、rm 灾难目标、curl|sh、fork 炸弹
#     - ASK：执行代码/装依赖/git 写操作/删文件/网络/未知命令
#   · 统一规则：allow/ask/deny × session/workspace × command/command_program/path/tool
#     优先级 deny > ask > allow；显式 ask 与受限模式不能被 auto-approve 越过
#   · ASK 处理链：统一规则/旧版 allow 规则 → auto-approve → 交互 approver(y/n/a)
#     → 都没有则拒绝（fail-closed）；拒绝原因经 PermissionDenied 事件 + is_error 回填 LLM

# safety/sandbox.py —— 运行时执行边界（Step 61，与审批策略正交）
# ExecutionSandbox(workspace_root, backend=auto, network=False)
#   · 所有 shell/exec 都先 audit：越界/敏感路径、shell 包装递归、命令替换、已知网络命令
#   · macOS Seatbelt：workspace rw；系统 runtime ro；user Home denied；network denied
#   · Linux bubblewrap：unshare-all/cap-drop；workspace bind；runtime/extra roots ro
#   · auto 会真实 probe 后端，不可执行时退回 static 并在启动日志告警
#   · static 只是兼容护栏，无法抵御解释器代码的动态绕过，不宣称内核级隔离
#   · ToolContext 默认自动挂载；Subagent isolated worktree 用 rebind 切换根目录

# core/events.py —— 事件总线
class EventBus:
    def subscribe(self, handler) -> Callable[[], None]  # 返回退订函数
    async def emit(self, event: AgentEvent) -> None      # handler 可同步可异步
```

## 事件清单（UI/会话/审计的消费面）

RunStarted · StepStarted · QueryPhaseChanged · LLMStreamDelta · LLMResponseReceived ·
ToolCallStarted · ToolProgress · ToolOutputDelta · ToolCallCompleted · PlanUpdated ·
UserInputRequested · PermissionDenied · RunFinished · ErrorOccurred

## 主循环语义

```
run(user_input):
    history += user 消息; 失败计数清零
    intent, turn_pool = infer_intent() + snapshot(ToolRegistry)
    for step in 1..max_steps:
        system_hint = turn_pool.system_hint()  # 仅本轮，不写入持久历史
        assistant = llm.stream(history + system_hint, turn_pool.tool_specs())
        history += assistant
        if 无 tool_calls: return 文本             # 结束
        for 连续只读调用组:
            并发 execute；每个完成即 emit ToolCallCompleted
        for mutating 调用:
            单独串行 execute；完成即 emit ToolCallCompleted
        按原序 history += role=tool 消息          # 执行随机完成，回填永远保序
        if 某签名连续失败 ≥ repeat_failure_limit: return "判定为循环" 中止
    return "达到最大步数" 提示
```

- **单轮工具池**：`TurnToolPool` 在一轮开始时固化 Registry 快照；provider schema、
  执行查找、动态工具目录和审批描述符共用同一份 pool，避免“隐藏 schema 但仍可执行”。
- **动态工具目录（Step 100）**：候选集先经过 disabled/allowlist/feature/run-mode/
  MCP-health/permission/TurnIntent 过滤器，再分为 `core`、`discoverable`、
  `deferred` 三档。`core` 工具带完整 Schema；`discoverable` 目录只暴露名称和单行
  描述；MCP、Plugin 默认 `deferred`。模型调用 `tool_search(query, load=true)` 时，
  该工具只能看到过滤后的候选集，不能拿到可变 Registry。加载结果在下一模型 Step
  才进入 provider `tools`；执行边界同时校验“已加载”和“最近一次请求已暴露”，
  因而同一 request 内刚加载的工具不能并发执行。Registry、Schema、目录、搜索和
  `/tools` 输出统一按工具名稳定排序，避免注册顺序改变 Prompt Cache 前缀。
- **执行调度**：连续只读工具并发；mutating 工具和未知/被过滤工具作为 barrier 串行。
  因此 read/grep/glob/repo_map 等可提速，同时 write/edit/shell 不越过模型给出的顺序。
- **实时事件**：只读组用 `asyncio.as_completed` 在每个工具完成时立即推送 UI，历史仍
  等整组完成后按 tool_call 原序回填。
- **ToolUseContext**：每次调用从基础 `ToolContext` 派生上下文快照，携带当前
  `ToolDescriptor`（来源、MCP server、readOnly）；审批和工具执行共享同一视图。
  readOnly 只用于并发调度/展示，MCP 自报的 `readOnlyHint` 不会自动绕过审批。
- **当前边界**：Provider 契约在 `StreamFinished.message.tool_calls` 才提供完整参数，
  因此 Krelcode 暂不在 SSE 参数增量阶段提前执行工具；并行/串行调度发生在完整
  assistant 响应结束之后。

卡死检测签名 = 工具名 + sort_keys 序列化的参数；成功即清零该签名计数。

启动时上下文装配（agent/context.py，Context v3 默认，失败安全）：
规则、git 分支/porcelain 状态、目录树分别建模为 `ContextSection`，带 priority
与来源元数据；统一受 `[context].max_tokens` 预算约束，默认规则(10) > git(20) >
tree(50)，超限时先从低优先级内容截断/省略。最终包成 `<workspace_context>` 注入
provider-bound context；`WorkspaceContext.summary_lines()` 提供来源与 token 统计，
REPL `/context` 查看、`/context refresh` 重采。运行时 Hook、Memory 与 Working
Context 使用显式 untrusted data 边界，不写入持久历史。

错误恢复原则：工具抛异常/被拒绝/未知工具，都转成 `ToolResult(is_error=True)`
回填给 LLM，让它自我修正，而不是中断循环。

## 统一 Provider 机制（pi-ai 同款思想）

core 的 Message/ToolCall 是 **wire-free 规范格式**；任何 provider 专属的 JSON
结构都不允许出现在 core/agent/tools 层。每个 adapter 独立负责四件事：

1. **消息互转**：规范历史 ⇄ provider wire format
   - OpenAI: role 消息 + tool_calls/tool_call_id
   - Anthropic: system 顶层抽离、content blocks、连续 tool_result 合并进单条 user 消息
   - Gemini: contents/parts、systemInstruction 独立字段、functionCall 无 id
     → 合成 gem-N（转换历史时经 assistant.tool_calls 重建 id→name 映射，
     functionResponse 按 name 配对）、连续 tool 结果同样合并
2. **流式翻译**：provider SSE 事件 → 统一 `TextDelta` / `StreamFinished`
   - OpenAI: choices[].delta.content / delta.tool_calls（按 index 聚合分片 arguments）
   - Anthropic: content_block_delta(text_delta / input_json_delta.partial_json)
   - Gemini: candidates[].content.parts[]（text / functionCall 整块到达，无增量拼接）
3. **语义规范化**：finish_reason（end_turn→stop, tool_use→tool_calls, max_tokens→length）；
   usage（prompt_tokens→input_tokens、Gemini usageMetadata.promptTokenCount 等，
   缺失不补造）；Gemini 有 functionCall 时 finishReason 也是 STOP → 规范化为 tool_calls；
   **思考流**（reasoning_content/reasoning、Anthropic thinking 块）→ 统一
   ThinkingDelta 事件——只用于 UI 展示，永不进入 message.content/history
4. **错误规范化**：非 2xx → `LLMHTTPError(status, body, provider)`；网络错误 → `LLMError`

配套机制：
- **ToolSpec 统一工具描述** `{"name","description","parameters"}`：registry 产出，
  adapter 各自包装（OpenAI function.parameters / Anthropic input_schema）
- **transport 抽象**：urllib 同步实现 + `asyncio.to_thread` 桥；测试注入 FakeTransport 全离线
- **工厂与配置**：`resolve_settings()` 按 显式参数 > TOML([providers.NAME]) > 环境变量 > 默认 解析；
  `create_provider(settings)` 按名建实例；`register()` 是第三方 provider 扩展点；
  `list_providers()` 枚举全部已注册 provider 与默认 key 环境变量（Step 51）
- **provider 自描述组装**（Step 51）：`create_provider` 不再按名字写分支；
  provider 用 `STANDARD_CONFIG`（是否接收 model/api_key/base_url/timeout/generation）
  与 `EXTRA_CONFIG_KEYS`（构造函数额外接受的 TOML 键）声明自己，工厂 generic 组装
- **装饰器透明性**：`RetryingProvider` 用 `__getattr__` 代理 inner 的 `api_key`/`base_url`
  等属性——否则脱敏层经包装链看不到真实 key，安全网会静默失效（Step 51 修复）
- **能力协商**：`ProviderCapabilities.supports_tools` 为 False 时 AgentLoop 不传 tools
- **可复用请求能力**（Step 88）：支持预算治理的 adapter 额外实现
  `prepare_request() -> PreparedLLMRequest` 与 `stream_prepared()`。前者一次性完成
  messages/tools → wire payload，后者只执行该不可变请求；URL/headers/body 不进入 repr
  或快照，Agent 层只读取 body 字节数、token 估算、payload hash、输出上限和缓存段元数据。
  `RetryingProvider` 对同一 prepared body 重试，未实现该能力的旧 Provider 自动回退到
  `stream(messages, tools)`。
- **非法工具参数约定**：arguments 非合法 JSON 时 adapter 填 `INVALID_ARGUMENTS_KEY` 保留原文，
  循环层转成 is_error 工具结果回填，让 LLM 自我修正

新增一个 provider = 新建一个 adapter 文件 + registry 里注册一行（声明 STANDARD_CONFIG/EXTRA_CONFIG_KEYS 即可接入 TOML 配置），其余层零改动。

## 上下文管理（Step 7）

- **token 估算**（core/tokens.py，agent/tokens.py 兼容转发）：CJK ≈ 1 token/字，其他 ≈ 4 字符/token，
  精度足够做预算决策；消息含 tool_calls 参数序列化
- **两道防线**（Step 25 / Step 89）：超预算先做**Tool Microcompact**——最近
  keep_recent_tools（默认 6）条、当前轮、错误、写入/编辑和 plan/ask_user 结果保护；
  更早的大输出按结构摘要 → 单行引用分级（消息本身保留 → tool_call/result 配对永远完整；
  零 LLM 调用、幂等、短于 200 字符的结果跳过）；仍超预算（或 /compact 强制）才走 LLM 摘要
- **预算压缩**（agent/compression.py ContextManager）：超 `--context-tokens`（默认 10 万）时，
  在高水位触发、向低水位目标选择最远安全 turn 边界；中段历史合并为一条
  `Structured Compact v2` 交接消息，当前 turn 永不切割。保留区绝不以孤儿 tool
  消息开头；模型失败时由 Working Context/history 生成确定性降级摘要；太短的历史
  （<6 条）不压缩，防抖动
- **大输出落盘**（loop）：工具结果 > 12K 字符（可调）时全文写入
  `<workspace>/.krelcode/tool_outputs/`，历史只留 truncate_middle 版 + 文件引用；
  落盘失败退回纯截断
- 压缩发生会 emit 内容无关的 `ContextCompressed`，携带策略、摘要来源/generation、高/低水位、来源节省量与 checkpoint ID；REPL `/compact` 经 `maybe_compress(force=True)` 手动触发（不必等预算超限）
- 跨平台：run_command 超时终止在 POSIX 走进程组 killpg(SIGKILL)，Windows 退化为杀主进程（os.killpg/SIGKILL 不存在时不触碰）

### 真实请求装配与预算（Step 88）

```text
persistent history
  → PromptAssembler (PromptFragmentRegistry + ContextSources)
      → PromptPlan (fixed L0-L12 order + cache boundary)
      → ProviderMessages (system + exact history + tools)
  → provider.prepare_request(messages, tool_specs)   # optional, assemble once
      ├─ opaque HTTP request {url, headers, body}
      ├─ body bytes / token estimate / SHA-256
      ├─ output limit
      └─ content-free cache segments
  → ContextUsageSnapshot
  → full-request budget check
      ├─ fits: stream_prepared(prepared)
      └─ over: tool microcompact → rebuild/recheck
                ├─ fits: stream_prepared(final)
                └─ still over: LLM compact once → rebuild → stream_prepared(final)
```

- 输入预算与模型窗口分开约束：`request_input <= context_budget`，且
  `request_input + output_reserve <= model_window`。`output_reserve` 在 Provider 已知
  真实输出上限时使用该值；旧 Provider 才退化为 `window - context_budget`。
- Anthropic 的缓存段来自最终 wire payload：system 稳定 block、最后一个 tool schema、
  最后一条 message block，均最多 3 个显式 `cache_control` 断点。OpenAI/DeepSeek 标记为
  `implicit` prefix-cache 机会；Gemini 标记 `none`。segment 只保存 kind/token/char/hash，
  不保存正文。
- 自动压缩最多执行“探测 → compact → 重新装配”一次。若历史太短或没有可压缩中段，快照
  会以 `input_fits=false` 明确暴露越限，而不是反复压缩或伪造成功。
- `ContextUsageSnapshot` 新增 provider/wire fidelity/cache mode/body bytes/body tokens/
  wire overhead/payload digest/tool/message cacheable tokens/output limit/input fits 与
  cache segments；TUI、`/context`、recorder、stream-json 和 SDK 共用 `as_record()` 安全记录。
- 历史归档、token 统计与逻辑消息仍然是 wire-free 规范模型；真实 body 只在单次调用期间
  存在于 `PreparedLLMRequest`，不写入 history/session。

## Working Context 与 Tool Microcompact（Step 89）

Step 89 把“当前任务知道什么”和“长期记忆什么”彻底分开，并把工具输出压缩从
粗暴驱逐升级为可解释的分级策略。

```text
WorkingContextStore (session-scoped, not memory)
  entries: goal | plan | decision | file | command | error | observation | open_issue
  upsert(id) + bounded content/metadata + provenance + sequence
  ├─ begin_turn(user goal)
  ├─ record_plan / decision / error
  ├─ record_tool_result(ToolCall, ToolResult.data)
  └─ snapshot() → max entries/chars/tokens, redacted, grouped XML-like text
        ↓ volatile runtime layer (cacheable=false, priority < tool directory)
PromptAssembler → PromptPlan → ProviderMessages
                         └─ ContextAssembly（/context 兼容归因视图）
```

- `WorkingContextStore` 生命周期属于当前 session；`/reset` 清空，`/resume`、
  `/fork`、`/rewind` 从权威 history 重建。它不是 `MemoryStore`，不跨进程持久化，
  不进入 stable prompt cache 前缀。
- 条目按稳定 id 去重更新；文件使用 `file:<path>`，计划使用 `plan:current`，目标使用
  `goal:current`。所有正文与 metadata 都经 SecretRedactor + 内部密钥形态兜底；快照按
  kind 优先级丢弃低价值条目并显式报告 `truncated/dropped_entries`。
- `ToolResult.data` 不再只给 UI。AgentLoop 只提取有界、脱敏的 `tool_name`、
  `tool_is_error` 与 `tool_meta`（path/exit_code/count/bytes/…）写入 tool Message 元数据，
  会话 codec 可恢复；provider wire 不读取这些字段。

```text
ToolMicrocompactor (zero LLM, in-place, pairing preserved)
  recent keep_recent tools ────────────────► full text
  current turn / error / write/edit / plan/user input ► protected
  old large result (newer half) ───────────► structured summary + bounded preview
  old large result (older half) ───────────► one-line reference + full-output path
```

- 小于阈值、已有全文落盘引用且压缩无收益的结果不处理；重复执行幂等。压缩旧结果时清除
  会重复占用 wire 的 rich blocks，但保留 `全文已存盘` 路径供模型按需 `read_file`。
- 真实请求超预算的顺序固定为：构建 prepared request → 计算完整预算 → microcompact →
  重新装配/重算 → 仍超限才调用一次 LLM summary compact。这样大多数长工具会话无需摘要
  模型调用，也不会因压缩策略重复循环。
- `ContextMicrocompacted` 仅暴露 trigger、前后 token、保护/摘要/引用计数；不包含工具
  正文。事件接入 `/context`、TUI Context 面板、JSONL recorder 与 stream-json
  `context/microcompacted`，Renderer 不污染正文。
- `/compact preview` 使用 deepcopy 执行同一 microcompact 预览，不修改 history；正式
  `/compact` 仍是显式强制完整压缩入口。

## Structured Compact v2（Step 90）

Step 90 把自由文本摘要升级为可验证、可合并、可恢复的交接记录。摘要不是另一份
会话历史，而是当前任务继续执行所需的最小结构化现场。

```text
high watermark reached
  → choose safe turn boundary (never include active turn)
  → target low watermark; preserve last turn if target unreachable
  → Tool Microcompact (no LLM, protected recent/current/error/write results)
  → still over budget: LLM structured summary
       ├─ strict CompactSummary v2 JSON (machine validated)
       ├─ merge previous summary + Working Context + history fallback
       └─ invalid/failed model output: deterministic fallback
  → [system] + [CompactSummary v2] + [tail turns]
  → persist pre-compact history + checkpoint metadata
```

`CompactSummary v2` 的稳定字段为 `goal`、`decisions`、`files`、`commands`、`tests`、
`errors`、`open_issues`、`next_steps`、`handoff_notes`。文件带 path/status/note，
测试带 name/status/detail；模型输出必须包含全部字段，非法版本、类型、枚举或重复 key
会触发降级。wire 中使用短别名并在 `Message.provider_meta` 保存 generation/source 等
非 provider 元数据，减少重复 compact 的 token 成本。

- 每次 compact 都从已有摘要继续合并；旧 goal/file/test/error/open issue 不会因模型
  本轮“没说”而消失，generation 单调递增，compacted message 计数累计。
- 自动模式只在已完成 turn 的 user 边界切割，当前 turn（含 steering 后的全部消息）
  保持原样；若中段只剩上一条摘要，不会重复摘要同一内容。
- 高低水位是触发与目标而非两个阈值循环：高水位判断完整请求预算，低水位用于选择
  更激进的合法切点；若无法达到低水位，选择最激进的合法 turn 边界。
- compact 前完整 history、摘要、边界、水位、来源节省量和 Working Context 快照写入
  `.krelcode/compact-checkpoints/`，原子替换、fail-open、SecretRedactor 脱敏。
- `/compact preview` 展示按来源（tool results/conversation/total）的预计节省量与
  low-watermark 是否可达；正式 `/compact` 展示 checkpoint ID。
- `ContextCompressed`、TUI Context 面板、recorder、stream-json 和子代理 feed 只传播
  内容无关元数据，不复制摘要正文。`/rewind` 匹配摘要 digest 后优先恢复 compact 前
  完整现场，再重建 Working Context 和压缩状态。

## 交互与会话（Step 8）

- **StreamRenderer**（ui/render.py）：EventBus 订阅者，LLMStreamDelta 逐字流式打印
  （助手文本用 `⏺`，工具结果用 `⎿`）；工具活动/审批/压缩事件打断流式行后续接；
  RunFinished 兜底打印未经流式的系统消息（max_steps/卡死中止），并避免模型自带
  尾部换行时产生空行。write/color 可注入，测试无需终端
- **REPL**（ui/repl.py）：输入先经 InputProcessor 分类，业务/UI/Control 命令同步分流，
  自然语言启动后台 turn；EventBus 将 LLM 文本、thinking、工具开始/完成等事件实时
  推给 StreamRenderer，REPL 本身不阻塞等待最终字符串。启动信息卡片展示 provider/模型/工作区/工具/会话；输入区
  用横跨终端的上下两条 `─` 边界包住提示符（pi 风格），空闲提示符 `❯`、运行中
  提示符 `↳`；输出前用 ANSI 清除输入行与下边界，提交后光标越过下边界，避免正文
  覆盖边框。POSIX TTY 使用零依赖字符级行编辑器（cbreak + `os.read`）：输入 `/`
  时按前缀实时过滤命令并在下边界外渲染候选列表，Tab 补全唯一命令；运行中只提示
  `/stop`、`/exit`。输入框下方固定预留 8 行候选区，菜单变化时只在该区域原地覆盖；
  撤销 `/` 会清空候选区但保持区域高度不变，因此不会把历史搜索内容推到终端
  scrollback。真实 TTY 的输入读取放在 daemon 线程，避免退出时被阻塞的 `input()`
  拖住，测试仍通过 async input_fn 注入
- **diff 上色**：edit_file 结果的 unified diff 红绿高亮；tty 检测 + NO_COLOR 尊重；
  错误信息不走 diff 渲染
- **SessionRecorder**（session/recorder.py）：JSONL 双记录——审计事件流
  （user_input/tool_call/tool_result(截断500)/permission_denied/context_compressed/run_finished）
  + 每轮 RunFinished 落完整 history 快照；`--resume [latest|path]` 取最后一条快照恢复；
  损坏行跳过、写失败静默（持久化永不阻断对话）
- 消息编解码（session/codec.py）是 session 私有格式，与 provider wire format 无关

## 进阶能力（Step 9-11）

- **MCP 客户端**（mcp/）：stdio 子进程 + 换行分隔 JSON-RPC 2.0；
  initialize 握手 → tools/list 发现 → tools/call 调用；reader task 按 id 配对
  Future；服务器 EOF/崩溃时 pending 全部立即失败（不悬挂）；
  MCPTool 适配器实现本地 Tool 端口（远程 inputSchema 直通统一 ToolSpec，
  命名 `<server>__<tool>` 并清洗非法字符），注册进同一 ToolRegistry，
  AgentLoop 对"远程/本地"零感知；TOML `[mcp_servers.*]` 配置驱动，
  单服务器失败不阻断启动，退出时优雅关闭
- **repo_map**（tools/builtin/repo_map.py）：零依赖代码地图——Python 走 ast
  （类/函数/方法、参数签名去默认值、嵌套缩进），JS/TS 走正则；
  剪枝目录、跳过语法错误文件、20K 字符预算截断；行号可接 read_file(offset)
- **多会话**（session/manager.py + REPL）：/sessions 按 mtime 列会话
  （轮数/消息数/首条输入预览），/resume <n|latest|path> 会话内切换，
  与 --resume 共用 restore_session；当前录制不中断

## MCP 进阶（Step 12）

- **传输抽象**：MCPClient 只做 JSON-RPC 协议层（id 配对 Future、超时、
  disconnect 时 pending 全部失败）；MCPTransport 端口两实现：
  · StdioTransport：子进程管道 + asyncio reader task
  · SSETransport（HTTP+SSE 2024-11-05 旧规范）：GET 长连接收 `endpoint` 事件
    （urljoin 解析 POST 目标）与 `message` 事件；reader 跑独立线程，经
    loop.call_soon_threadsafe 回投；connector 可注入（单测零 socket）
  共享 SSEEventParser（sseparse.py）；HTTP 传输的线程模型统一为
  「线程做阻塞 IO，call_soon_threadsafe 回投消息」（Future 不能在外部线程 set_result）
  · StreamableHTTPTransport（2025-03-26 规范，url 默认）：单端点 POST，
    响应按 Content-Type 二态处理（application/json 单条 / text/event-stream 流），
    Mcp-Session-Id 会话头自动捕获携带，404=会话过期→fail-closed 断连，
    通知回 202，流内服务器请求自动应答 -32601；JSON-RPC batch 数组逐条分发
    · 可恢复性（Step 52）：SSE 解析 `id:`/`retry:`，GET 监听流断流后指数退避
      重连并携带 `Last-Event-ID`（服务器补发断线期间事件）；405/401/403 不重试
    · 协议版本（Step 52）：请求携带 `MCP-Protocol-Version`，initialize 协商后
      切换为服务器返回版本（兼容 2025-06-18 起的强制头，旧服务器忽略）
    · 会话终止（Step 52）：close() 尽力 DELETE（带 Mcp-Session-Id），失败静默
    · 监听流重连参数可配置：TOML `listener_retries` / `listener_retry_delay`
  配置 `command` → stdio；`url` → streamable-http（默认），`transport = "sse"` 回退旧规范
- **capabilities 门控**：initialize 握手的 capabilities 决定注册哪些工具——
  tools → 每远程工具一个 MCPTool；resources → list_resources/read_resource；
  prompts → list_prompts/get_prompt；全部 `<server>__<tool>` 命名进同一注册表
- **反向请求**（Step 35）：服务器→客户端的 sampling/createMessage（回调
  app 注入的 sampler 闭包，用共享 LLM 完成补全）、roots/list（工作区根）、
  ping 由 MCPClient 统一应答，未知方法回 -32601；initialize 时按配置声明
  sampling/roots 能力。协议时序教训：notifications/initialized 必须 await
  实发（fire-and-forget 的调度延迟会让它晚于后续请求到达，违反规范）
- **热重载**（MCPSupervisor）：sync() 配置 diff（dataclass 相等判断：
  未变保持连接、变更先停后启、移除即注销）；check_and_restart() 复活崩溃
  服务器并重注册工具；工具注销走 registry.unregister_prefix("<server>__")；
  REPL /mcp 查看状态、/mcp reload 重读配置触发同步
- **生产协议补齐（Step 96）**：tools/resources/resourceTemplates/prompts 列表统一
  分页（100 页上限、重复 cursor fail-closed）；notifications/*/list_changed 按服务器
  `asyncio.Lock` 串行刷新，工具目录先验证重复/命名冲突再原子替换；Resource Template
  注册为只读 `list_resource_templates` 工具。HTTP OAuth 在标准库内实现 token cache、
  refresh/client-credentials、protected-resource 与 authorization-server 发现、动态客户端
  注册及 authorization code + PKCE；401/403 刷新后仅重试一次。`~/.krelcode/mcp-oauth.json`
  按 server/url 哈希隔离并原子写为 `0600`，Header/OAuth 值支持 `${ENV_VAR}`。

## 子代理/后台任务（Step 13）

- **SubagentManager**（subagent/manager.py）：spawn(task) 创建独立 AgentLoop
  （自己的历史/事件总线/工具注册表；共享父级 provider、ToolContext、审批策略），
  asyncio.create_task 后台执行；wait(id, timeout) 用 `asyncio.shield` 包裹——
  等待超时绝不取消子任务；send_input 续聊（仅空闲时，上下文延续）；
  shutdown 统一 cancel
- **父代理工具**（subagent/tools.py）：spawn_agent / wait_agent / send_to_agent /
  list_agents，工具持 manager 引用（构造注入），ToolContext 保持纯工作区语义
- **消息队列与打断**（Step 23，对齐 Codex multi_agent 语义）：运行中
  send_input 默认入队（同一 Task 内排空，wait 语义 = 队列排空）；
  interrupt=true 取消当前 Task → sanitize_history 消毒（级联弹出尾部
  孤儿 tool / assistant(tool_calls)，防 provider 拒绝不配对请求）→
  尾部未答完的 user 消息与打断指示合并（防连续 user，Anthropic 要求
  角色交替）→ 开新 Task；队列中未处理消息保留
- **wait_many**：一次等多个子代理（asyncio.wait，超时不取消）；
  wait_agent 工具的 agent_id 支持逗号分隔，单 id 输出格式向后兼容
- **git worktree 隔离**（Step 41）：spawn(isolated=true) 在
  `.krelcode/worktrees/<id>`（分支 `krelcode/agent-<id>`，基于 HEAD）建独立
  worktree，子代理的 ToolContext 指向它——路径沙箱天然把写入限制在分支内，
  多个子代理并行写同名文件互不干扰；任务排空自动 commit（`-c` 身份兜底），
  shutdown 存档被取消的工作并回收 worktree（**分支保留**供 merge/cherry-pick）；
  `.krelcode/` 经 .git/info/exclude 本地排除（与 autocommit 共用
  ensure_local_exclude/git_is_repo，已从 AutoCommitter 提取为模块级函数）
- **子代理上下文管理**（Step 43）：SubagentManager.context_budget 由组装根
  注入（与父代理同一套预算解析：显式 > 模型目录 > 兜底）；每个子代理
  spawn 时以**当时的** self.llm 建独立 ContextManager——/model 热切换后
  新子代理自动用新模型；switcher 同步更新 subagents.llm
- **fork_context**（Step 42，依赖型子任务）：spawn(fork_context=true) 深拷贝
  父历史（replace 逐消息拷贝，去父 system 换子代理 system）；尾部消毒——
  spawn 时父历史尾部必然悬挂 assistant(tool_calls)（spawn 调用本身），
  sanitize_history 弹掉；若消毒后尾部是未答完的 user 消息（触发委派那句），
  弹出并与子任务合并（"[委派的子任务]" 标记）——保上下文又保角色严格交替；
  get_parent_history 以 getter 注入（/resume 重绑定 loop.history 后依然正确）
- **防递归**：子代理工具集 = default_tools()（不含 spawn 系），委派深度硬限制 1 层
- **可观测**：SubagentSpawned/SubagentFinished 上父总线——渲染器打状态行、
  recorder 落 JSONL 审计；REPL /agents 列状态
- 子代理系统提示词要求：专注委派任务、不向用户提问、最终消息即给父代理的报告

## 检查点与回滚（Step 16）

- **CheckpointManager**（session/checkpoint.py）：纯事件订阅者——
  RunStarted 开组（label=用户输入预览，记录 session/turn 身份）、
  ToolCallStarted 对 write_file/edit_file 的目标文件在**首次修改前**做字节级快照
  （同组同文件只记第一次，保最原始版本）、RunFinished/RunCancelled 关组
  （无文件改动的空组直接丢弃）
- 落盘 `<workspace>/.krelcode/checkpoints/NNNN-<ts>/`：meta.json（session/turn/文件清单）
  + files/ 备份；构造时扫描目录重建组栈 → /undo 与 /rewind 跨重启可用
- undo_last()：LIFO 弹出最近已关闭组，逐文件恢复（existed=True 还原备份、
  existed=False 删除新建），单文件失败不阻断其余
- 边界：run_command 的文件修改不可静态追踪，不在快照范围（文档注明）；
  路径解析与工具层同规则（越界路径静默跳过）
- REPL：`/undo`（回滚上一轮）、`/undo list`（查看检查点栈）、
  `/rewind <n>`（多轮批量逆序恢复）、`--no-checkpoint` 关闭

## 用量统计与 git 自动提交（Step 17）

- **UsageTracker**（agent/usage.py）：订阅 LLMResponseReceived，累计规范化
  usage（input/output/total tokens + 调用次数）；RunStarted 清零本轮账本，
  会话账本持续累计；provider 未上报 usage 时为 0（不估算不造假）。
  REPL /cost、`-m` 模式结束打印摘要
- **AutoCommitter**（session/autocommit.py，--auto-commit 开启）：
  订阅 RunStarted（记录 label=用户输入预览）与 RunFinished（触发提交）；
  `git status --porcelain` 有变更才 add -A + commit，消息 `krelcode: <label>`；
  首次提交前把 `.krelcode/` 追加进 `.git/info/exclude`（仅本地，幂等，
  不污染用户 .gitignore）；非 git 仓库/无变更/提交失败全部静默跳过；
  与 CheckpointManager 互补：checkpoint 管文件级回滚，auto-commit 管历史留痕

## 生产化加固（Step 18）

- **token 预算**：AgentLoop 累计每次 StreamFinished 的 usage
  （total_tokens 缺失时用 input+output），每个 step 的 LLM 调用**前**检查，
  超限走与 max_steps 相同的优雅中止路径（assistant 消息入历史 + RunFinished）
- **敏感文件写保护**（safety/policy.py）：write_file/edit_file 不再一律放行——
  classify_protected_path() 对 `.git/`（仓库内部，hooks 可执行任意代码）、
  `.env*`、密钥/凭据模式（*.pem、id_rsa*、.npmrc…）以及
  **`.krelcode/allow_rules.json`（防 LLM 给自己提权）** 返回保护原因 →
  走 ASK 审批链（非交互 fail-closed 拒绝）；纯路径计算不触网
- **headless JSON**：`-m --output-format json` 时 stdout 只输出一行
  `{ok, reply, usage, tools_used, denied, session}`；启动提示（MCP 连接、
  自动提交、会话恢复）全部改走 stderr；异常 → `{"ok": false, "error": ...}` + 退出码 1

## Hook 系统与安全补洞（Step 19 + Hooks v2）

- **写保护补洞**：`krelcode.toml`（agent 配置本体）与 `.krelcode/` 全目录
  （审批白名单/检查点/会话/工具输出）列入 SandboxPolicy 保护路径——
  堵住「LLM 改配置注入 hooks / 给自己提权 / 篡改回滚痕迹」的攻击面
- **Agent 端口**：`core/hooks.py` 只定义 `HookPort` 与 `HookResult`；AgentLoop
  不认识配置/子进程实现，应用组装根注入 `HookRunner`。
- **生命周期**：session_start/session_end、user_input/user_prompt_submit、
  pre/post_tool_use、tool_completed/tool_failed/file_edited、pre/post_compact、
  subagent_started/subagent_finished、run_finished。`user_prompt_submit` 位于查询
  引擎入口，因此 REPL、headless 与子代理一致执行。
- **结构化协议**：stdin 接收事件、工作区、data 与 tool 参数 JSON；stdout 支持
  additional_context / system_message / decision=block；`pre_tool_use` Unix 退出
  码 2 兼容 Claude Code 阻断语义。`tools` 支持 glob 匹配。
- **安全模型**：hooks 只能来自用户配置（配置即信任，直接执行不走审批）；
  子进程环境经 `scrub_environment` 剥离密钥/认证代理；模板变量只展开一次并
  `shlex.quote`，防止递归替换与命令注入；除显式 block 外，失败/超时 fail-open。
- **Context 集成**：session/compact Hook 上下文进入 `runtime_context`，每轮临时
  拼入 system hint，过期/压缩上下文不会污染可恢复历史；HookTriggered 事件进入
  UI 与 `hook_triggered` JSONL 审计。

## 韧性与自我修正辅助（Step 20）

- **RetryingProvider**（llm/retry.py）：LLMProvider 的透明装饰器，
  组装根按 `--retries`（默认 2）包装真实 provider（mock 不包装）：
  · 可重试：HTTP 408/409/425/429/5xx、网络/超时类 LLMError
  · 不重试：401/403/400、ConfigError（确定性错误重试无意义）
  · **emitted 标志**：已产出 TextDelta 后绝不重试（重放会重复输出）；
    常见瞬态故障（HTTP 层）都发生在首事件前，覆盖面足够
  · 指数退避 base*backoff^attempt（封顶 max_delay），sleep 可注入（测试零等待），
    on_retry 回调接 UI 日志
  · **Retry-After**（Step 36）：LLMHTTPError 携带响应头，retry_after 解析秒数
    （HTTP-date/非法值 → None 退回退避）；实际等待 = max(退避, Retry-After)，
    超过 120s 硬上限则快速失败（不让用户干等）
- **generation 参数透传**（Step 36）：TOML `[providers.X] generation = {...}`
  （temperature/top_p/max_tokens…）经 settings.extra 到各 provider——
  OpenAI/Anthropic 合并进 payload 顶层（显式键 model/messages/stream/max_tokens
  后置设置，**配置无法覆盖协议关键字段**）；Gemini 进 generationConfig 段
- **edit_file 失败辅助**：多处匹配 → 错误信息带每次出现的**精确行号**
  （LLM 可据此扩大上下文）；未匹配 → difflib.get_close_matches 给出
  最接近的行（带行号）提示缩进/拼写差异。目标：把「edit 失败 → 盲改重试」
  变成「edit 失败 → 一轮定向修正」

## 思考模型支持（Step 21）

- llm 层新增 `ThinkingDelta` 事件；DeepSeek/Qwen 的 `reasoning_content`、
  OpenRouter 系的 `reasoning`、Anthropic 的 `thinking_delta` 都在各自
  adapter 内翻译到统一事件（signature_delta 忽略）
- 约定：思考文本**不进** message.content、不进 history、不进会话快照——
  只经 `LLMThinkingDelta` 事件流向 UI。Step 54 起 renderer 在 UI 层缓存整轮
  thinking，默认只显示一行 `✻ Thinking… · 输入 /thinking 展开`；用户执行
  `/thinking` 时再按原始 Markdown 展开（运行中同样允许，立即读取当前缓存）（保留标题、列表缩进、空行与 fenced
  code，终端上色不改变文本结构）。同一轮多次 thinking 以空行拼接，新一轮
  `RunStarted` 清空旧缓存
- RetryingProvider/collect 等对 ThinkingDelta 天然透明
- **Anthropic 扩展思考回传**（Step 34）：thinking/signature/redacted_thinking
  块捕获后挂 `Message.provider_meta`（core 的不透明状态位：只携带、不解读，
  session codec 负责持久化）；回传时 thinking 块置于 assistant content 最前
  （工具调用续轮时 API 强制要求）。`thinking_budget` 配置启用
  （钳制 ≥1024，max_tokens 自动抬到 budget+1024 以上）

## 交互层增强（Step 22）

- **输入历史导航**：`ui/repl.py` 的 `InputHistory` 只保存当前 REPL
  会话中非空、非斜杠命令的用户原始输入（连续重复去重，最多 200 条）；
  `TerminalLineEditor` 解析 `ESC[A/B` 与 `ESCOA/B`，`↑` 向旧浏览、`↓`
  向新浏览，越过最新项时恢复导航开始前的草稿。历史在进入 agent 前记录，
  因此 `@文件` 仍保留用户原始写法，不会把展开后的文件正文塞进输入历史
- **@文件引用**（session/attachments.py）：统一输入层展开——进入 history 的已是展开后文本，
  provider/内核零感知。词首 `@path` 触发（`(?<![\w@])` 防 email 误伤）；
  路径与工具层同规则约束在工作区内；不存在/二进制/越界/超限一律保留原文
  并回显说明（fail-safe）；单文件 8K 字符、单消息 5 个文件的注入预算
- **会话导出**（session/export.py）：history → Markdown（System/User/Assistant/
  Tool Result 分节，工具参数与长结果截断）；`/export [路径]`，缺省落
  `.krelcode/exports/session-<ts>.md`
- **REPL 可测试化**：run_repl 增加 input_fn 注入点（默认仍是
  asyncio.to_thread(input)）；ScriptedInput 驱动下全部 /命令均有自动化回归
  ——UI 层从「只能人工演示」变为与内核同级的测试覆盖

## Steering 与中止（Step 26）

- **AgentLoop.steering_queue**：运行中用户插话入队；每个 step 开始前注入历史——
  尾部是 user 消息则合并（防连续 user，Anthropic 要求角色交替），
  尾部是 tool/assistant 则作为独立 user 消息追加（`[用户插话]` 前缀）；
  注入发 UserSteered 事件（渲染器提示、recorder 落盘）
- **取消修复**：run() 被 cancel 时 `_repair_after_cancel()`——
  sanitize_history（已下沉 core.messages，主代理取消与子代理打断共用）
  级联弹出尾部孤儿 tool / assistant(tool_calls)，若尾部是未答完的 user
  则补 assistant("[已被用户中止]") 保角色交替 → 修复后的历史可直接续聊
- **REPL 状态机**：常驻 reader task → inbox 队列；asyncio.wait 同时盯
  inbox 与运行中任务。IDLE 处理命令/启动 run；RUNNING：普通文本→steering、
  /stop→cancel+修复+提示、/exit→停止退出、其他斜杠→提示；EOF 停任务退出；
  任务结束后未送达的插话**可见地**丢弃（不静默留到下一轮）

## 代码智能工具（Step 27）

- **find_refs**：ast 级 Python 符号引用查找，[def]/[ref]/[import] 分类输出
  `路径:行号: [类型] 源码行`；与文本 grep 互补——不被字符串/注释里的
  同名词干扰；语法损坏文件静默跳过；剪枝与限额与 grep 一致
- **check_syntax**：批量 ast.parse 语法自检（不执行代码），错误输出
  `路径:行:列: 消息`；agent 批量编辑后的廉价验证手段

## Agent Skills（Step 58）

Skill 是用户可安装的指令包，不是 Python 插件；Krelcode 只解析和执行
`SKILL.md` 中声明的自然语言流程，不 import/exec Skill 本体。数据流严格分层：

1. **发现**：`SkillManager` 按 global → project → custom 扫描固定根目录和
   `[skills].paths` / `--skills-dir`；后者同名覆盖前者。无 frontmatter、
   非法名称或缺 description 的目录记录为 issue 后跳过，不阻断启动。
2. **索引**：system prompt 只注入 `name + description + source`，受数量/字符预算
   限制；这层渐进披露让模型先判断相关性，而不是把全部正文塞进上下文。
3. **加载**：`load_skill` 按需读取完整正文，替换 `$ARGUMENTS` /
   `${ARGUMENTS}` / `$SKILL_DIR` / `${SKILL_DIR}`，并附资源清单与
   `allowed-tools` 指导。`disable-model-invocation` 阻止模型主动加载，
   `user-invocable` 控制 `/skill`。
4. **资源**：`list_skill_resources` + `read_skill_resource` 支持分段读取；
   所有路径 resolve 后必须留在 Skill 目录内（防 `..` 与 symlink 逃逸）。
   纯凭据文件拒绝读取，`.env`/`krelcode.toml` 走现有 SecretGuard 脱敏。
5. **脚本**：`run_skill_script` 只接受参数数组，`create_subprocess_exec` 直达
   解释器；复用 run_command 的敏感环境变量黑名单，cwd 锁在 Skill 目录，
   超时按进程组终止。策略层把脚本执行归入 SHELL_TOOLS，默认 ASK。
6. **热重载与隔离**：`/skills reload` 原子替换 manager 内存索引并重写 system
   prompt；主代理与后续子代理共享 manager，子代理通过 provider 读取最新索引。
   `--no-skills` 同时关闭发现和 5 个 Skill 工具注册。

安全边界：Skill 正文和 bundled script 都来自用户安装内容，等同用户信任的
指令/代码；脚本仍必须审批。模型不能借资源 reader 越过 Skill 根目录，也不应
把 Skill 当作任意文件的通用读取后门。

## 扩展生态基础（Step 95）

Krelcode 仍保持“pi 式分层 + 轻量事件总线”，但把 Plugin v1 的单一 Tool 适配器升级为可观测的扩展平面。专用注册表继续负责执行，`ExtensionRegistry` 只保存跨切面元数据，不复制 Prompt、Hook 命令、MCP headers 或 Tool schema。

1. **来源追踪**：统一登记 Plugin、Tool、Skill、Agent、Command、Hook、MCP 的 source/scope/origin/version/plugin/enabled/priority。同名项按确定性优先级保留，并记录被遮蔽来源；`/extensions` 可过滤查看。
2. **Plugin v2 manifest**：同一 `plugin.toml` 可声明 `[tools]`、`[skills]`、`[agents]`、`[commands]`、`[hooks]`、`[mcp]`。组件路径只允许插件内相对路径，拒绝 `..`、绝对路径、越界 symlink；单插件失败隔离。
3. **组件复用现有端口**：Tool 继续返回 `ToolResult` 并接收统一 `ToolContext`；Skill/Agent 进入现有文件系统管理器；Markdown Command 走 `CommandSource.PLUGIN`；Hook 继承现有结构化 I/O、脱敏和审批旁路规则；MCP 进入 `MCPSupervisor`。插件不获得第二套运行时。
4. **生命周期**：`install/update/remove/enable/disable/reload` 由 `PluginManager` 管理。安装先校验清单、文件数/体积和 symlink，再 staging + 原子替换；remove 移入 `.trash`；启停状态写入 `~/.krelcode/plugin-state.json`，键包含 scope，避免 user/project 同名泄漏。
5. **热重载一致性**：一次 reload 原子刷新 Plugin Tool、Skill roots、Agent Definition roots、Plugin Commands、Hook specs、MCP 配置和 ExtensionRegistry。显式 TOML MCP 配置覆盖插件默认；配置/Plugin Hook 合并。
6. **配置分层**：默认配置按 `user → project → local → managed` 合并。字典递归合并，数组/标量由高层替换；`--config` 保持显式独占。managed 损坏 fail-closed，普通层损坏由 doctor 报告但不阻断其余可信层。`krelcode.local.toml` 被写保护且默认不提交。

安全边界：插件代码仍是用户显式安装的本地可信 Python，不等同于 MCP 远程工具的 readOnlyHint；其读写/命令能力继续经过 Tool 实现、权限模式、审批与 Sandbox。当前不自动安装插件依赖，也不提供不可信代码的进程级沙箱。

## 生产可用性 P1（Step 96）

P1 不新增第二套运行时，而是在既有扩展平面上补齐可运维能力：

1. **MCP 协议面**：所有 list API 统一分页；`list_changed` 通知只触发所属服务器的
   元数据刷新，失败保留旧工具/命令池；Resource Templates 通过专用只读工具暴露。
   OAuth token 获取与 JSON-RPC 传输解耦，`MCPClient` 只在收到 401/403 challenge 时
   刷新并重试一次，避免凭据逻辑侵入每个远程 Tool。
2. **命令来源**：新增 `CommandSource.CUSTOM`，从 user/project/configured Markdown 目录
   发现命令并支持 frontmatter、参数替换和 `allowed-tools`；`PLUGIN` 继续保留独立来源。
   固定优先级 `builtin > custom > skill > plugin > MCP`，所有前端继续消费同一 Registry。
3. **Hook 执行器**：`command/http/prompt` 共用 trigger、payload、decision 与脱敏管线。
   HTTP Hook 只允许 http/https，Header 从环境展开；诊断目标去除 userinfo/query/fragment，
   动态 Header 凭据加入 SecretRedactor。Plugin Hook 走同一 parser，并保留 source/root。
4. **Subagent Definition**：`model` 通过延迟 resolver 创建独立 Provider，不改变父级
   `/model`；`permission-mode` 写入独立 ToolContext；`mcp-servers` 在 spawn 前与活/配置
   服务器求交合并，省略=继承、`*`=全部、`[]`=禁用。任何未知 provider/server/tool 均
   fail-closed，sidechain/status 记录实际 model/provider/permission/MCP 集。

## 会话生命周期与交换（Step 77）

会话文件从“history 快照旁路”升级为可审计的多会话事实源。`session_start/session_meta`
携带 stable session id、title、parent id、provider/model/workspace；审计流继续记录输入、
工具、Hook、计划、用户澄清、子代理与 run 完成/取消。`SessionRecorder` 在正常完成与
取消时都刷新 history，崩溃/取消不会再丢失最后一轮快照。

`SessionLifecycle` 独占活动会话切换，保证 loop.history、EventBus recorder 与
AgentRuntime.session_id 三者一致：

```
/fork ─────── 当前内存 history → 新 JSONL(title, parent_id) → 切换 recorder/runtime
/resume ───── 目标 history → 新 JSONL(title, target id)       → 切换 recorder/runtime
/rename ───── 追加 session_meta 覆盖 title（recorder 统一脱敏）
/delete ───── 确认后移入 sessions/.archive（非不可逆删除，拒绝活动会话）
```

`SessionInfo` 从元数据和所有 user_input/history 构建轻量索引，支持
`/sessions search`。`/export *.json` 写出版本化 `krelcode-session` 快照，`load_history`
可直接读取并通过 `/resume` 建立新分支；Markdown 继续用于人读分享。分支采用 copy-on-resume
语义，原会话不变，避免恢复动作覆盖来源历史。

## 运行时权限与 Plan 审批（Step 78）

权限模式不再只是启动时写入的 `RuntimeSnapshot` 字段，而由
`safety.runtime.RuntimePermissionManager` 独占修改。`set_mode()` 同时更新
`loop.tool_context.runtime` 与已固化的当前 turn 策略快照；工具池仍是 turn 边界，
新模式下下一轮会重新计算真实可用工具。REPL 的 Shift-Tab 只产生
`ReplControl.CYCLE_PERMISSION`，由前台调用 manager，编辑器不依赖安全层：

```
default ──Shift-Tab──> plan ──> read-only ──> auto-approve ──> default
```

规则仓库保持向后兼容：旧 `allow_programs/allow_commands` 继续读写；新版 `rules[]`
增加 `allow/ask/deny`、`session/workspace` 和四种匹配 kind。`path` 在 symlink
解析后转成工作区相对路径并拒绝 `..`；`deny` 在只读工具与命令分级之前执行，
`ask` 不被 auto-approve 跳过，PLAN/READ_ONLY 仍由策略层二次拒绝副作用。

Plan 交互由 `PlanApprovalCoordinator` 独立维护：

```
/plan <request>
  → 保存进入 Plan 前模式，切到 PLAN
  → 模型只读调查 + update_plan
  → PlanUpdated 写入 coordinator，status=PENDING
  → /plan approve: status=APPROVED，恢复原模式，生成 approved_plan 执行 Prompt
  → REPL 自动启动下一轮；/plan cancel 清为 CANCELLED 且保留计划历史
```

`SessionViewState` 只镜像计划内容和 approval status，用于状态栏与 `/plan` 展示；
执行授权事实仍在 coordinator，命令 handler、REPL 和未来 TUI 可复用同一状态机。

## 对话级 Rewind 与历史分叉（Step 79）

Rewind 不进入 AgentLoop 或工具实现，而是 session 层的独立协调器
（`session/rewind.py`）。稳定身份从事件边界向下传递：
`RunStarted.turn_id/session_id`、`PlanUpdated.turn_id`、`RunFinished/RunCancelled.turn_id`
同时被 CheckpointManager、SessionRecorder 与 TurnTimeline 使用。时间线为每个已完成
turn 深拷贝 `history_before/history_after` 与 `plan_before/plan_after`；plan 边界还包含
审批状态和权限模式，避免只恢复清单却丢失 Plan 授权现场。

```
/rewind <n>
  ├─ 拒绝运行中/非法/未完成 turn
  ├─ 收集 n..last 的 session-scoped checkpoint
  ├─ 预检全部 backup/path，并保存当前文件安全快照
  ├─ 按 checkpoint seq 逆序恢复（跨多轮逐步退回边界版本）
  ├─ loop.history = history_before(n)
  ├─ plan_manager.restore(plan_before(n))
  ├─ 截断内存 TurnTimeline
  └─ recorder 追加 rewind + 权威 history 快照
```

文件层先在内存中预读所有目标版本并验证路径；任一备份缺失或越界时在写文件前失败。
写入异常会尝试恢复回退前的当前文件。checkpoint 带 `session_id + turn_id`，即使工作区
存在多个会话都曾使用 `turn-1`，也只恢复当前会话。history 回退保留**当前**
system prompt/技能索引，只回退对话消息，避免把重新采集的工作区规则一起退回旧版本。
`run_command` 的任意文件副作用仍不可追踪，命令输出会明确提示这一边界。

`/rewind fork <n>`（等价 `/fork --turn <n>`）取第 n 轮**结束后**的 history/plan 建立新
JSONL 分支并切换 recorder/runtime id；它明确不恢复工作区文件，因为“回到旧对话但保留
当前改动”是常见评审/对比场景。真正需要文件也回到历史边界时使用 `/rewind <n>`。
运行中的 rewind 一律拒绝；`/reset` 与 resume/fork 切换会清空旧时间线，防止把边界
错配到另一份 history。

## 后台 Shell Job Manager（Step 80）

后台进程由独立 `jobs.ShellJobManager` 拥有，不进入 AgentLoop 的 turn 生命周期，
也不依赖 Tool/REPL。`job_start` 只是 manager 的安全适配器：`ToolContext` 注入 manager，
命令仍先经过与 `run_command` 相同的 Sandbox 审计、风险分级和审批。

```
job_start(command, cwd)
  └─ SandboxPolicy → ExecutionSandbox.audit/prepare
      └─ ShellJobManager.start()
          ├─ create_subprocess_exec(start_new_session, stdin=DEVNULL,
          │                          stdout=PIPE, stderr=STDOUT, scrub env)
          ├─ reader task → SecretRedactor → EventBus ShellJobOutput
          │                 ├─ bounded in-memory chunks + sequence cursor
          │                 └─ .krelcode/jobs/<id>/output.log (bounded, redacted)
          └─ reaper task → wait process group → ShellJobFinished
```

- Job **不属于某一 turn**：turn 取消或结束不会终止 dev server/watcher；只有显式
  `job_kill`、进程自然退出或 `AgentRuntime.close()` 的 manager shutdown 才回收。
- 实时输出按读取块立即发布，不等待换行；每块带单调 `sequence`。`job_output` 接收
  `after_sequence`，只返回更新内容，并可用 `wait_seconds` 等待下一次输出（最多 30 秒）。
- 内存只保留有界 tail（默认 200k 字符）；页面请求过旧 cursor 时返回 `truncated=true`。
  完整输出写任务日志，默认 10 MiB 后停止继续写日志，但实时事件与进程执行不受影响。
- `job_list` 和 `job_output` 是只读工具；`job_kill` 只能终止当前 manager 持有的 job id，
  read-only/plan 模式仍禁止 start/kill。`cwd` 被限制在工作区内，后台 stdin 固定为 DEVNULL。
- 输出在进入事件、缓冲区、日志和完成 preview 前统一脱敏。reader 会暂存可能构成 secret
  前缀的尾部，避免 key 被拆分到两个 read 块后绕过字符串替换。
- `ShellJobStarted/Output/Finished` 是旁路事件：recorder 只落 started/finished 有界审计，
  不逐块膨胀会话；REPL 在空闲时也能显示完成通知；stream-json 输出跨 turn 的
  `job/started`、`job/output_delta`、`job/finished`。模型历史不会被自动注入输出，模型需
  主动调用 `job_output`，避免日志污染上下文。
- 兼容边界：manager 是进程内的，重启后不接管旧 OS 进程；旧日志可人工查看，但新进程
  不再 list/output/kill 它们。命令通过 `&` 自行派生的后台子进程若仍持有 pipe，主进程退出后
  manager 会结束残留进程组，因此正确用法是让目标命令在前台运行并由 `job_start` 托管。

## 交互式子代理（Step 81）

子代理不再只是 `spawn → wait` 的黑盒，而是有实时事件流、运行控制和可审计
sidechain 的后台 Agent。实现刻意拆成四个与 manager 解耦的组件：

```
subagent/
├── definition.py  AgentDefinitionManager：用户定义发现/覆盖/校验
├── feed.py        AgentFeed：稳定 sequence、有界缓冲、脱敏
├── control.py     AgentControl：协作式 pause/resume gate
├── sidechain.py   SidechainStore/Writer：start/feed/state/message/history JSONL
└── manager.py     生命周期 + child EventBus 适配 + 父工具/命令适配
```

`AgentLoop` 只新增一个通用 `step_gate: async () -> None` 端口；主 Agent 不传，
子代理传 `control.wait_if_paused`。因此 pause 不取消模型调用或正在执行的工具，
而是在当前原子操作结束、下一 step 开始前停住：

```
spawn(task, definition?)
  ├─ Definition allowlist ∩ tool_factory() → child registry/runtime
  ├─ child AgentLoop(step_gate=AgentControl.wait_if_paused)
  ├─ child EventBus → AgentFeed → SubagentFeedUpdated(parent EventBus)
  └─ SidechainWriter → .krelcode/sidechains/<run>/<agent>.jsonl

running ──pause()──> paused ──resume()──> running
   │                    │
   └──── kill() ────────┴──> killed (Task.cancel)
```

- **Feed** 把 `RunStarted/Step/Phase/LLMStreamDelta/ThinkingDelta/ToolProgress/
  ToolOutputDelta/ToolCallCompleted/RunFinished/Error` 映射为稳定 kind。sequence
  单调递增；`after_sequence` 支持增量读取；内存默认 2,000 条、单条 24,000 字符，
  超出后 `truncated=true`。分页 `next_sequence` 返回本页最后一条，绝不跳到全局最新。
- **Pause/Resume** 保留同一个 Task、history、工具上下文和队列。暂停期间
  `send_input` 入队，resume 后继续；`interrupt` 仍是取消当前轮并立即开新轮。
- **Kill** 与 interrupt/shutdown 分离：设置 `kill_requested`、取消 Task、状态进入
  `killed`，终态 `SubagentFinished(killed)` 只发一次；隔离 worktree 先存档到分支再回收，
  之后拒绝追加任务。
- **Definition** 从 `~/.krelcode/agents`、项目 `.krelcode/agents`、项目
  `.agents/agents` 发现 Markdown；项目覆盖全局。frontmatter 提供 description、
  allowed-tools、isolated、fork-context，正文是角色 system prompt。allowlist 只能
  缩小默认工具池；未知工具、坏 frontmatter 或空正文 fail-closed/隔离。
- **Sidechain** 记录 start、definition 元数据、初始/排队/打断消息、feed、状态转换、
  每轮完整 history 快照、结果、branch/worktree。写入前递归脱敏；侧链不自动注入父
  history，父 Agent 通过 `wait_agent` 或 `get_agent_feed` 主动消费。进程重启后可读取
  审计，但不自动接管旧 Task。
- 新增父级事件：`SubagentFeedUpdated(agent_id, sequence, kind, text, data)` 与
  `SubagentStateChanged(agent_id, status, reason)`；recorder、hooks、renderer、
  stream-json 都是旁路消费者。stream-json 用 `agent_sequence` 与 QueryEvent 外层
  `sequence` 区分，避免覆盖 envelope 排序。
- 父工具扩展为 `spawn_agent(definition=...)`、`get_agent_feed`、`pause_agent`、
  `resume_agent`、`kill_agent`、`list_agent_definitions`，原四工具语义保持兼容。
  `/agents` 在运行中可用，提供 list/feed/pause/resume/kill/definitions/sidechains。
- 安全边界不变：Definition 是可信本地配置，但不能绕过审批、Sandbox、read-only/
  plan 模式或子代理不可递归限制；敏感 feed/history 在进入 UI、JSONL、stream-json
  before 全部经过 SecretRedactor；流式 Feed 暂存 secret 前缀尾块，覆盖跨 chunk 拆分。

## Prompt/Context Fragment Registry（Step 82）

system prompt 不再由 App、Skill 热重载和 `/context refresh` 各自拼接。
`agent/fragments.py` 负责来源登记、作用域、手动启停与快照版本；请求级最终分层和消息生成
由 `agent/prompt_assembler.py` 独占，AgentLoop 只消费 `PromptPlan` 与 `ProviderMessages`。

```
Fragment sources
├─ core       → core:base                 locked · cacheable
└─ workspace  → workspace:rules           toggleable · cacheable
                         │
                         ▼
              PromptFragmentRegistry
                ├─ source/id overrides（id 优先）
                ├─ cacheable prefix first
                ├─ atomic group/source replacement
                └─ PromptSnapshot {text, generation, stable/full SHA-256}
                         │
                         │
                         ▼
             ContextAttachmentStore
                ├─ environment/date       dynamic · revisioned
                ├─ skill/agent/MCP delta  dynamic · revisioned
                └─ mode/token reminder    dynamic · revisioned
                         │
                         ▼
                  PromptAssembler
                ├─ fixed L0-L12 order
                ├─ stability/trust validation
                ├─ PromptPlan + cache boundary
                └─ ProviderMessages
```

Step 101 后，`skills:index` 与 `workspace:git/tree` 不再写入 Registry；它们迁移到
`ContextAttachmentStore`，与 date/catalog/reminder 一样作为独立 dynamic block 进入
`PromptAssembler`。Registry 只保留稳定 base/rules 与会话级手动启停语义。

- Fragment 有 `id/source/scope/layer/priority/content/stability/trust/cacheable/
  compactable/toggleable`；旧构造参数仍保持兼容。
  `core:base` 为不可关闭安全底座；其他内置 Fragment 均可按精确 id 或 source 启停，
  精确 id 覆盖 source 级设置，`/context reset` 清除运行时 overrides。
- cacheable Fragment 被渲染为全局连续前缀，即使以后注册动态 system Fragment，也不会
  阻断后续 workspace stable rules。`stable_hash` 只覆盖稳定前缀；git 状态/目录树由
  attachment 承载，变化只改变请求 `dynamic_hash`，不会使 Registry 或规则缓存失效。
- legacy snapshot 仍通过 `Message.provider_meta.prompt_cache_boundaries` 暴露字符
  offset；生产路径由 `ProviderMessages.system_blocks` 携带显式
  `stable_system_blocks/dynamic_system_blocks`。Anthropic 原样保留 block 数组，只在最后
  一个 stable block 上设置 `cache_control: ephemeral`；OpenAI/DeepSeek 等兼容端点按
  stable → dynamic 固定顺序合并，但仍保留 block 计数、边界与双 hash 供观测。
- App 启动、Skill 热重载、session restore 与 `/context refresh` 全部回到同一快照路径。
  workspace group/source 替换先完整校验再提交，避免冲突时留下半更新状态。
- `/context` 展示 Fragment/source 汇总；`/context enable|disable|toggle` 修改后立即重建
  history[0]；`/context cache` 展示边界、稳定/动态 ids 与哈希。
- compact 数据层新增 `CompactBoundary/CompactPreview`。预览先深拷贝 history，再执行同一
  套工具结果驱逐和 `_safe_cut`，因此可看到 head/middle/tail、是否回避孤立 tool、预计
  节省与待摘要正文，而真实历史完全不变。`/compact preview --auto` 对应自动预算判断，
  `/compact preview` 对应强制压缩预演。
- 正式 compact 仍由 `AgentLoop.compress_context()` 独占 Pre/PostCompact Hook、事件与
  历史修改；`/compact` 只调用该入口，然后读取 `last_compact_preview` 回报实际
  strategy、驱逐数与边界，不复制压缩逻辑。

## 终端 TUI Panel Dock、Shell 与 Memory 快捷模式（Step 83）

REPL 仍保留 scrollback 作为对话正文，只把“当前现场”放进输入框上方的固定高度 Dock。
这样不会为了全屏 alternate-screen 牺牲流式文本、历史滚动与原编辑器的稳定性。

```
ui/tui/
├─ model.py       Panel / PanelItem 纯数据
├─ layout.py      固定高度边框、CJK 截断、tab/item hitbox
├─ controller.py  焦点、选择、键鼠路由
├─ mouse.py       SGR mouse + CPR cursor origin codec
└─ panels.py      SessionViewState → Tasks / Activity / Context / Memory
          │
          ▼
PromptWriter [Panel Dock] → 上分隔线 → 输入 → 下分隔线 → 状态/slash menu
```

- **结构化任务区**：Tasks 直接消费 reducer 的 `plan_items/plan_statuses/active_index`，
  不用 renderer 文本反推状态；Activity 展示 turn phase、step、活跃工具、工具进度、
  队列深度和 Shell 快捷状态；Context 展示权限、历史 tokens/预算、Fragment/cache 与
  Memory 统计。
- **键盘导航**：`Ctrl-O` 是唯一入口；面板获得焦点后为模态导航，左右切换 tab、上下
  移动选择、Enter/Esc 回到输入框。cbreak 模式显式禁用 `VDISCARD`，否则终端驱动会
  吞掉 Ctrl-O。面板未聚焦时方向键、Shift-Tab、Tab、历史搜索等旧语义完全不变。
- **可点击组件**：真实 TTY 启用 xterm SGR mouse（1000+1006）。PromptWriter 在重绘后
  发出 CPR 查询，编辑器把 `ESC[row;colR` 换算为 Dock 顶行；点击坐标仅用于纯
  hitbox 命中，不依赖固定屏幕尺寸。退出/异常路径都关闭 mouse reporting。
- **Shell 快捷模式**：`!cmd` 由 InputProcessor 只做分类，REPL 在空闲时启动窄任务。
  `AgentLoop.execute_direct_tool()` 临时构造与普通 turn 相同的 RuntimeSnapshot、
  TurnToolPool 和 ToolContext，执行 `run_command` 时仍经过参数 schema、Hooks、
  风险审批、路径规则、Sandbox 和 secret redaction。执行事件临时旁路给 renderer，
  但调用与结果都不进入 `loop.history`；运行中拒绝并发执行，Esc/Ctrl-C 可终止进程组。
- **Memory 快捷模式**：`#text` 默认写 project 作用域；Step 86 进一步增加
  `#user:` / `#project:` / `#session:`。单条 2,000 字符、每作用域 200 条；常见
  token/key 形态和当前 SecretRedactor 值都会替换为 `***`。`#` 只打开面板。近期最多
  12 条/4,000 字符作为 `memory:scoped` 动态 runtime Fragment 注入；它与 `AGENTS.md`
  分离，不进入对话消息，也不会阻断 prompt cache 稳定前缀。

## Context 可观测性与基线（Step 84）

Context 可观测性复用请求组装边界，不复制 Prompt 拼接逻辑：

```
AgentLoop._llm_turn(step)
  ├─ compress_context()
  ├─ TurnToolPool.tool_specs()
  ├─ PromptSnapshot + turn runtime hints
  ├─ provider-bound messages
  ├─ ContextUsageSnapshot
  │    ├─ estimated_input_tokens（messages + tool schemas）
  │    ├─ system/workspace/memory/history/runtime/tool_schema/overhead
  │    ├─ budget/model window/output reserve
  │    ├─ stable/volatile + cache boundary/SHA-256
  │    └─ tool/fragment count + source token map
  └─ emit ContextAssembled → provider.stream(messages, tools)
```

- `core/context_usage.py` 是跨层稳定契约；`agent/context_usage.py` 只做派生计算。
  分项采用与 compact 相同的 CJK 感知估算器，并通过最大余数归一化保证各项之和
  精确等于 `estimated_input_tokens`。
- AgentLoop 保存 `last_context_snapshot` 供 `/context` 使用；`SessionViewState`
  保存最近快照供 TUI Context 面板使用。两者共享同一对象契约，不在 UI 重算。
- Recorder 写 `context_assembled` 精简记录；stream-json 编码为
  `context/assembled`，`snapshot` 可安全持久化/传输。Renderer 默认忽略该事件，
  不污染对话正文。
- 快照不含 system prompt、消息正文、工具参数或凭据，只有来源名、token 计数、
  预算与哈希，因此不会形成新的敏感内容泄漏路径。
- Step 84 只增加观测基线，不改变真实 Prompt 内容、工具 schema、预算判断或 compact
  行为；`/context` 在尚无快照时保持旧版估算输出兼容。

## PromptAssembler 与固定 Prompt 层级（P0）

```text
PromptFragmentRegistry + ContextSources
  → PromptAssembler.assemble()
      → PromptPlan
      → ProviderMessages
```

`PromptFragment` 统一携带 `id/source/scope/layer/priority/content/stability/trust/cacheable/compactable`。
固定层级为 L0 Managed Policy、L1 Base Agent Prompt、L2 User Global Instructions、L3 Project/Directory
Rules、L4 Persona/Output Style、L5 Mode/Permission/Tool Contract、L6 Skill/Agent/MCP Catalog、
L7 Memory Evidence、L8 Environment/Date/Session、L9 Plan/Working Context、L10 Recent/Retrieved
History、L11 Current User Request、L12 Per-turn Reminder。

- L0-L9（以及动态 L12 reminder）进入 system；L10/L11 由权威历史消息承载，不会复制进 system。
- 排序键固定为 layer → static/session/turn → priority → source/id；L0-L6 拒绝 turn fragment。
- `turn`、`data`、`untrusted` fragment 不得 `cacheable`；稳定断点只允许覆盖连续 cacheable 前缀。
- 当前 user 消息只出现一次，tool result 永远留在 history，不进入 system 或稳定前缀。
- `ProviderMessages` 是 Provider 的唯一输入；`ContextAssembly` 仅作为 `/context`、Resume 与
  checkpoint 的无正文兼容视图。

## 显式 system cache boundary（Step 99）

Provider 层不再把 system prompt 当作一个不透明字符串。`PromptAssembler` 先产出 provider-
neutral 契约：

```text
SystemPromptBlocks
├─ stable_system_blocks[]   # 连续可缓存 L0-L9 前缀，每个 Fragment 一个 block
├─ dynamic_system_blocks[]  # turn/session 动态尾部，永不设置显式缓存断点
├─ stable_hash
├─ dynamic_hash
└─ cache_control_boundary
```

- Anthropic 保留 block 数组，不提前 join；每个 stable block 独立序列化，只有最后一个
  stable block 获得 `cache_control: {"type": "ephemeral"}`。没有 stable block 时不生成显式
  断点，所有 dynamic block 保持无 `cache_control`。
- OpenAI-compatible 按 stable → dynamic 固定顺序合并成一个 system message。合并只发生在
  adapter 的 wire 序列化阶段，`PreparedLLMRequest`、`ContextUsageSnapshot` 与
  `ContextPlanSnapshot` 仍记录原始 block 数、边界和 `stable_hash/dynamic_hash`。
- `stable_hash` 只覆盖 stable block 文本，动态环境、Working Context、Memory 或 reminder
  变化不会使其改变；`dynamic_hash` 单独追踪动态尾部。hash、block 数量和 source/id 可进入
  `/context plan|cache`、TUI、JSONL、stream-json 与 SDK，但任何记录都不包含 block 正文。
- 旧 Provider、旧 system message 或只有 `prompt_cache_boundaries` 的调用由
  `resolve_system_prompt_blocks()` 生成兼容视图：显式 block 优先，否则从 system 消息按固定
  顺序恢复 stable/dynamic 视图。生产请求仍只允许由 `PromptAssembler` 产生显式 block。

## 增量 Context Attachment（Step 101）

动态上下文不再作为一个整体重建 system prompt。`ContextAttachmentStore` 为每个易变来源分配
固定 ID、kind、scope、trust、layer 与独立 revision；只要语义指纹未变化，revision 和
generation 都不推进。当前附件类型为：

```text
environment_snapshot / environment_delta   # L8，git/tree/root 当前现场与变化摘要
date                                       # L8，会话日期
skill_catalog_delta                        # L6，Skill 目录与本次增删
agent_catalog_delta                        # L6，Agent Definition 目录与本次增删
mcp_instructions_delta                     # L6，MCP initialize instructions（untrusted）
token_budget_reminder                      # L12，当前 turn 已用/剩余预算
mode_reminder                              # L12，run mode / permission mode
```

- `environment_attachments()` 对每个非 rules section 计算 SHA-256；只有 changed/added/removed
  section 会更新 `environment_snapshot` 与 `environment_delta`。稳定项目规则仍属于
  `workspace:rules` Fragment，环境变化不会使 Registry generation 或 stable hash 变化。
- catalog 只消费已经加载的 Manager/Supervisor 快照，不在 Prompt 装配时扫描磁盘或 MCP；
  catalog membership 变化与正文变化分别记录在无正文 metadata 中。
- 所有附件均 `cacheable=False`。`PromptAssembler` 将每个附件映射为一个独立
  `SystemPromptBlock`，因此环境变化只改变对应 block(s) 与 `dynamic_hash`，不会改变
  `stable_hash`，也不会复制进持久 history。
- 单文件解释意图仍会过滤 `environment_snapshot/environment_delta`，避免无关目录树扩大 token
  范围。MCP instructions 标记为 `untrusted`，Skill/Agent catalog 标记为 project scope。
- `/context attachments` 只展示 ID/revision/generation/layer/token；`/context cache` 从最近
  `PromptPlan` 展示包含附件在内的 stable/volatile block，绝不输出附件正文。

## ContextSource 与 ContextAssembler（Step 85）

临时 system context 的唯一组装边界从 AgentLoop 移到可注册来源集合：

```
AgentLoop._build_llm_request(step)
  ├─ PromptAssemblyRequest
  │    history + workspace_root + PromptSnapshot + TurnToolPool
  │    + TurnIntent + runtime hook context
  ├─ PromptAssembler.assemble()
  │    ├─ PromptFragmentRegistry：L0-L9 固定层级
  │    ├─ ContextAssembler：动态 ContextSource 收集与失败隔离
  │    ├─ PromptPlan：L0-L12 排序、稳定断点、stable/full SHA-256
  │    └─ ProviderMessages：system + exact history + tools
  └─ ContextUsageSnapshot → emit ContextAssembled → provider.stream
```

契约：

- `ContextSource` 只声明 `name` 和 `collect(ContextRequest)`。来源可以读取当前
  PromptSnapshot、工具目录、意图、历史与工作区根目录，但不能直接改 `AgentLoop.history`。
- `ContextLayer` 是装配单位，携带稳定 `id/source/category/content/priority`、cache 属性与
  token attribution。四个类别固定为 `system → workspace → memory → runtime`；同分类先按
  priority，再按来源注册顺序和来源内 layer 顺序，结果不依赖集合遍历或异步完成时序。
- `ContextAssembler` 负责征集 ContextSource、稳定排序与来源归因；`PromptAssembler` 是唯一
  请求级 Prompt/Message 装配入口。空 layer 忽略；重复 ID、非法 category/metadata/token
  attribution 被拒绝并记入有界 issue；可选来源抛出异常时只保留异常类型，不保留可能含敏感
  值的 message，也不会中断其它来源。
- `ContextAssembly` 包含最终文本、有序 layer、cache 边界/hash、token attribution、source/
  fragment 计数、issue 和 provider metadata；`stable_prefix_chars` 必须位于所有动态 layer
  之前，Anthropic block cache 与观测快照复用同一值。
- `ContextAssembly.messages_for()` 仅保留为兼容工具；生产请求统一由
  `PromptAssembler.provider_messages()` 生成。它返回新消息序列且不修改持久 history，因此每
  Step 的 tool directory、intent 和 Hook context 可以变化，turn 历史仍可恢复、可 fork。

默认来源与职责：

| Source | 产生内容 | 分类 |
|--------|----------|------|
| `prompt` | PromptSnapshot 的完整文本，按 Fragment 保留 token attribution；无 snapshot 时回退 system message | system |
| `directory_rules` | 当前轮目标路径祖先目录中的 AGENTS.md/KRELCODE.md，按需读取并展开 @import | workspace |
| `memory` | 三层 Memory 的 bounded 快照，trust=`untrusted`，绝不进入 system Prompt Registry | memory |
| `tool_directory` | 本轮动态工具目录，不含完整 JSON Schema | runtime |
| `turn_intent` | 高置信度“仅解释指定文件”等轮级约束 | runtime |
| `runtime_hooks` | SessionStart/Compact 等 Hook 注入内容 | runtime |
| `working_context` | 当前目标、计划、文件/命令/错误/决策的 bounded 快照（Step 89） | runtime |

`/context sources` 展示注册来源、最近装配 layer、priority/category/cache 状态与 issue，不输出
layer 正文。`ContextUsageSnapshot.source_count` 只统计实际贡献了非空 layer 的来源，token
来源映射继续保留 Fragment/Hook 级别的细粒度归因。

## Context v3 安全边界与默认切换（Step 94）

Context v3 以 `PromptAssembler` 为唯一请求级装配入口，`ContextAssembler` 保留为来源收集与
`ContextAssembly` 兼容归因层，同时把“来源”和“信任”拆开：

- `[context].engine` 默认 `v3`；`legacy`（别名 `v2`）只保留一个发布周期用于回退，
  未知值 fail-safe 回落 v3。engine 写入 `ContextAssembly`、`ContextPlanSnapshot` 和
  `ContextUsageSnapshot`，`/context` 与 stream-json 都可确认实际路径。
- `ContextLayer` 在 `id/source/category/origin` 之外增加 `trust`。system prompt 与
  项目规则使用 system/trusted；Memory、Working Context、Hook runtime context 使用
  `role="data-not-instructions"` 和 `trust="untrusted"`，内容中的 XML-like 标签先转义，
  不能提前关闭 envelope 或伪装成更高优先级来源。
- Memory 在 v3 中不再注册到 PromptFragmentRegistry，而由 `MemoryContextSource` 每次
  从 `MemoryStore.context_view()` 读取同一份选择结果并生成 memory layer。因此内存命中、
  token attribution、entry IDs 与真正发送给模型的内容不会发生二次读取漂移。
- 对高置信度“只解释明确文件”的请求，`V3PromptSnapshotContextSource` 过滤
  `workspace:git` 与 `workspace:tree`；工具池仍只暴露 `read_file`，目录规则只加载目标
  路径祖先。该约束发生在模型调用之前，而不是等模型误读后再拦截。
- Working Context 若当前仅包含与最新 user 消息逐字相同的 goal，则不重复注入；一旦出现
  文件、命令、错误或决策事实，仍以 volatile untrusted layer 进入请求。
- Memory JSONL 读取跳过损坏行；project/session 路径拒绝 symlink 或 parent-directory
  逃逸；checkpoint 文件 hash 只接受 workspace 内普通文件。规则加载继续拒绝越界 @import、
  超大文件、循环和敏感路径。
- compact 取消属于 `BaseException`，不会被摘要 fallback 吞掉；取消时 history 保持原样，
  summary provider 失败或返回非法 JSON 时只生成确定性 Working Context/history fallback。

Step 94 的离线验收覆盖 Prompt Injection、Memory Poisoning、symlink/path traversal、
超大/损坏/权限错误文件、compact 取消与摘要失败、长会话 token、Memory 检索质量、跨项目
隔离、Resume 现场刷新和完整请求 token 守恒。

## Memory 数据模型与作用域（Step 86）

Memory 从单一项目笔记升级为显式分层模型。记录和存储路径分离：记录只描述事实，
`MemoryStore` 根据 scope 选择持久化边界。

```text
MemoryEntry
├─ id / text
├─ scope      user | project | session
├─ kind       note | preference | fact | workflow | knowledge
├─ source     manual | legacy | future extractor
├─ tags[]
├─ pinned
├─ created_at / updated_at
└─ metadata{}
```

| Scope | 存储位置 | 生命周期 | 使用范围 |
|-------|----------|----------|----------|
| user | `~/.krelcode/memory/entries.jsonl` | 跨进程、跨工作区 | 当前 OS 用户私有 |
| project | `<workspace>/.krelcode/memory/entries.jsonl` | 跟随工作区 | 当前仓库/目录 |
| session | `<workspace>/.krelcode/memory/sessions/<session-id>.jsonl` | 跟随会话 | 当前逻辑会话 |

作用域按 `user → project → session` 由宽到窄排列。Prompt 渲染顺序相同，使更具体的
作用域靠后可见；12 条/4,000 字符的稀缺注入预算按 `session → project → user` 分配，
避免大量全局偏好挤掉当前任务记忆。

存储与兼容规则：

- 每个 scope 是独立 JSONL 文件，追加写入；超过 200 条后按最近记录原子压缩。
- 读取时以文件位置决定 scope，记录内伪造的 `scope` 字段不会让项目文件越权进入 user
  或 session 层。旧项目行缺少 scope/kind/source/tags/timestamps 时，按
  `project / note / manual` 原位读取，不需要批量迁移。
- 单条文本、tags 和展示内容都经过内置密钥模式与当前 `SecretRedactor`；不把敏感值写入
  新 JSONL、命令输出或 Prompt Fragment。
- `remove(id)` 只重写命中的 scope 文件，使用同目录临时文件原子替换；不会改动其他层。
- session scope 在 `--no-session` 或尚未绑定会话时驻留进程内存。正常启动、`/fork`、
  `/resume` 切换 recorder 时同步重新绑定 session id；旧会话文件不会被继续写入。
- `MemoryStore.fragment()` 生成 `memory:scoped` 动态 runtime Fragment，metadata 只包含
  各 scope 条数。它仍由 Prompt/Context Fragment Registry 管理，可手动启停且不进入
  prompt cache 稳定前缀。

交互入口：

- `#内容` = project；`#user: 内容` / `#project: 内容` / `#session: 内容` 显式选择 scope。
- `/memory [list]` 查看 ID、scope、kind 和内容；`/memory scopes` 查看条数与路径；
  `/memory add <scope> <text>` 写入；`/memory remove <id>` 删除；
  `/memory pin|unpin <id>` 调整搜索与注入优先级；`/memory search <关键词>` 检索。
- TUI Memory 面板展示 scope 与创建时间；命令和快捷写入后统一调用
  `sync_memory_fragment()`，下一次模型 Step 通过 ContextAssembler 看到新快照。

## Auto Memory 候选与审核（Step 91）

长期记忆写入分为 `extract → queue → review/promote` 三段。`AutoMemoryCoordinator`
作为 EventBus 旁路订阅 `RunStarted/RunFinished`，因此 agent 内核不依赖 session 层；
提取失败只记录诊断，不破坏当前任务。

```text
RunFinished
  └─ MemoryCandidateExtractor
      ├─ explicit remember      用户明确“记住”
      ├─ user correction        用户纠正
      ├─ successful test reuse  成功测试命令跨 turn 复用
      ├─ architecture pattern   架构陈述重复确认
      └─ task completion        最终答复中的稳定高价值事实
           ↓
MemoryCandidateQueue  (.krelcode/memory/candidates.jsonl, project-local)
  ├─ redact → exact digest dedupe → evidence append
  ├─ confidence threshold + conflict reconciliation
  └─ pending | accepted | rejected | superseded
           ↓ suggest / auto
/memory review  ........................ 人工接受或拒绝
           ↓
MemoryStore(project/knowledge)  → memory_search / memory Fragment
```

- 默认模式是 `suggest`：只生成候选，不写长期记忆；`auto` 仅在候选置信度达到
  `0.80`、证据成立且与已有候选/记忆无冲突时晋级；`off` 完全停止提取。
- 候选队列按 workspace 隔离，并记录 project id；模型生成的候选被强制为
  `project/knowledge`，不能借候选写入 user scope，也不能升级为 system instruction。
- 稳定知识必须带证据：turn/session、用户原句、工具名、命令与 exit code。重复成功的
  测试命令只累积 evidence，不重复建候选；不同正式测试命令被标为冲突并阻止自动晋级。
- 文本、证据、tags 与 metadata 在落盘前统一经过 SecretRedactor 和密钥模式过滤；覆盖
  API key、Authorization/Bearer、Cookie、常见平台 token 与私钥块。脱敏后无实质信息的
  候选直接丢弃。
- `memory_search` 是本地只读工具，返回带 `role="contextual_knowledge"` 和
  `trust="untrusted"` 边界的已审核记录；Memory Fragment 同样声明内容不是系统指令。

## 分层规则、目录规则与 @import（Step 87）

规则加载从 `context.py` 内部的简单文件拼接拆到独立 `agent/rules.py`，并把规则内容
与 Memory 明确分离：规则是持久行为约束，Memory 是任务事实/偏好记录。

```text
Rule precedence (broad → specific)
user       ~/.krelcode/AGENTS.md
project    <workspace>/AGENTS.md | KRELCODE.md
directory  <target ancestor>/AGENTS.md | KRELCODE.md

RuleLoader
  ├─ discover: 每个目标路径只检查 workspace 内祖先目录
  ├─ select:   每个目录 AGENTS.md 优先，其次 KRELCODE.md
  ├─ expand:   独立行 @relative/path，递归、按出现位置内联
  ├─ guard:    scope escape / sensitive files / cycles / duplicates
  ├─ bounds:   depth + file count + total character budget
  └─ RuleBundle { content, sources, issues, targets, truncated }
```

- `user` 与 `project` 入口在一次会话的稳定 PromptSnapshot 中；`directory` 由独立的
  `DirectoryRulesContextSource` 在每个模型 Step 前按当前轮路径收集，因此不会扫描整个
  仓库，也不会把无关子树规则带进上下文。规则层按 `user → project → directory` 渲染，
  更具体的规则靠后，表达覆盖优先级。
- 路径来源包括高置信度 `TurnIntent.target_files`、当前用户消息中的显式路径/附件，以及
  当前轮已经产生的工具参数（`path/file_path/cwd/...`）。即使目标文件尚不存在，也会从
  最近的已存在父目录开始检查规则。
- `@import` 只识别 Markdown 中独占一行的 `@path`，围栏代码内的示例保持原文，避免“文档
  展示导入语法”本身触发读取。相对路径基于导入文件目录；user 规则只能导入
  `~/.krelcode/` 内文件，project/directory 只能导入 workspace 内文件。
- 导入使用现有安全文本读取与路径 resolve；拒绝符号链接逃逸、硬链接/非普通文件、敏感
  文件、URL、循环和重复导入。单个规则文件最多 256KB，默认最多 5 层/32 个导入，
  展开后统一受 `rules_max_chars` 限制。所有失败转为不含正文的 `RuleIssue`。
- `RuleBundle` 同时供 `ContextSection.metadata`、`/context refresh` 和诊断命令复用。
  `/context rules` 只展示路径、scope、字符数、导入关系与 issue，不输出规则正文。
  `/context sources` 可看到动态 `workspace:directory-rules` layer 及其 token attribution。

## 统一输入处理管线（Step 67）

借鉴 Claude Code 将 `processUserInput` 与 REPL 分开的思路，终端不再直接解释原始
字符串。`session.input.InputProcessor` 接收任意来源的 `InputEnvelope`，完成确定性
处理后输出 `InputAction`：

```
raw input (terminal / API / IDE)
  └─ InputEnvelope
      └─ InputProcessor (async)
          ├─ strip / empty normalization
          ├─ classify: PROMPT | COMMAND | UI_COMMAND | CONTROL | SHELL | MEMORY
          ├─ @text/image expansion (asyncio.to_thread)
          ├─ optional user_input hooks (quoted vars, timeout, fail-open)
          └─ InputAction {text, command, arguments, images, notes, hook_context}
              └─ REPL / future frontend queue
```

- 命令不展开 `@`，避免 `/skill ... @file` 被提前注入；业务命令仍交给
  `SessionController`，`/thinking`、`/tool-output` 留在 UI，`/stop`、`/exit`
  归为 CONTROL 由前台快速处理。
- 附件与多模态 steering 共用同一动作模型；运行中插话可携带图片，且不把展开后
  文件正文写入输入历史。
- `user_input` Hook 只在普通 prompt 上执行，prompt 作为 shell-quote 变量传入；
  超时、失败和空输出均不阻断消息。成功 stdout 以 `<hook_context>` 包裹并从
  `app.py` 注入，业务层不感知 HookRunner。
- `SessionViewState` 只保存 REPL 观察面：phase、raw input、turn id、progress；
  AgentLoop 历史与 AgentRuntime 任务仍是各自的唯一事实源。

该拆分让 input processor 可脱离终端单测，也为未来 API/IDE 前端复用命令分类、
附件与 Hook 语义保留稳定入口。

## 统一命令系统（Step 68）

借鉴 Claude Code 的 command catalog/registry 思路，slash command 不再由 UI 元组、
`HELP_TEXT`、InputProcessor 常量和 SessionController 分支分别维护：

```
Command sources
  ├─ builtin (22)
  ├─ custom/user/project Markdown
  ├─ user-invocable Skills
  ├─ plugin Markdown
  └─ MCP prompts
        ↓
CommandRegistry
  ├─ name / alias resolution
  ├─ source priority + conflict detection
  ├─ enabled / hidden / running availability
  └─ shared catalog for menu, Tab, /help and InputProcessor
        ↓
CommandSpec.kind
  ├─ COMMAND    → SessionController dispatcher → isolated handler
  ├─ UI_COMMAND → REPL renderer
  ├─ CONTROL    → frontend fast-path
  └─ PROMPT     → Custom / Skill / Plugin / MCP prompt → run_prompt
```

- `CommandSpec` 统一携带 `name/aliases/description/argument_hint/kind/source/
  available_during_run/source metadata`。帮助、补全、运行态白名单和执行解析均从同一
  registry 派生，不再复制命令清单。
- 来源优先级固定为 `builtin > custom > skill > plugin > MCP`；冲突会记录到 `registry.conflicts`，
  低优先级命令不会覆盖内置命令。Skill 保留 `/skill <name>` 兜底，同时自动暴露为
  `/<skill-name>`；MCP Prompt 强制使用 `/mcp__<server>__<prompt>` 命名空间。
- `SessionController` 已从巨型条件分支收敛为解析器和 dispatcher；18 个业务命令由
  `commands/handlers` 中的独立函数实现。UI 命令和浏览器式终端状态仍留在 `ui/repl`。
- `/skills reload` 原子替换 SKILL 来源；Plugin reload 刷新 PLUGIN；`/mcp reload`
  重新拉取分页 `prompts/list` 并原子替换 MCP 来源。Custom Command 由
  `CustomCommandManager` 重扫目录并在 SessionController/REPL 原地同步。
  MCP Prompt 的单参数整体传递，多参数支持 `key=value` 或按声明顺序的位置参数。
- registry、InputProcessor 与 REPL 共享同一可变 catalog，因此重载后补全、帮助与执行
  立即一致，无需重启会话。

## 四层运行职责（Step 66）

在保留细粒度包结构的前提下，对外按四层理解：

| 层 | 主要对象 | 边界 |
|----|----------|------|
| 入口分流层 | `entrypoints.cli/main` | fast-path、参数解析；不创建 Agent |
| 交互会话层 | `ui.repl` + `session.InputProcessor/CommandRegistry/SessionViewState/SessionController/runner` | 输入与命令先结构化；终端只负责 I/O/渲染，会话控制器负责命令分发、turn 生命周期、steering、恢复 |
| 执行推进层 | `agent.AgentRuntime/AgentLoop` + 能力适配器 | 统一 turn/cancel/reload/close；Provider/工具差异在适配器消化 |
| 基础架构层 | `core`、配置、事件总线、JSONL、Sandbox、脱敏 | 提供无业务偏向的契约、持久化和安全边界 |

`app.py` 横跨四层完成对象装配，但不拥有会话命令或执行流程。headless 与 REPL
都通过 `AgentRuntime` 执行，因此未来增加 API/IDE 入口时可复用同一会话与执行门面。

## REPL 前台协调、审批与事件背压（Step 71）

交互层不再让安全策略直接 `input()`，也不让 QueryEngine 使用无界队列：

```
SandboxPolicy ──async request──> ApprovalBroker ──request queue──> REPL 前台
       ▲                               │                               │
       └──────── yes / no / always ────┴────────── y / n / a ──────────┘

AgentLoop ──EventBus(async handler)──> bounded QueryEvent queue(256)
                                          └─ drain in order ─> renderer ─> idle
```

- `ApprovalBroker` 是策略与终端之间的 rendezvous。策略任务只等待 Future；REPL
  字符编辑器继续拥有 stdin。无前台、EOF、中断、查询取消和进程退出全部
  fail-closed；后台子代理在 REPL 空闲时产生的审批同样由常驻 watcher 接收。
- `TerminalLineEditor` 将独立 `Esc`/`Ctrl-C` 转为非文本控制动作，将
  `Alt+Enter` 转为 `TerminalSubmission(queue_next=True)`，不会混入 prompt 文本。
  `InputProcessor` 仍负责附件和 Hook，最终用 `InputDelivery.STEER|QUEUE` 表达投递语义。
- `ReplSessionRunner` 独占 query、renderer consumer、审批 watcher 与下一条队列；
  REPL 主循环只处理 stdin/编辑器和 UI 回调。任务终态必须先 drain 全部 QueryEvent
  并完成 renderer，才切回 IDLE；QUEUE 消息在收尾后自动启动，STEER 消息注入当前
  turn 的下一步。
- `QueryEngine` 的 EventBus handler 是 async，并把事件放入有界队列，形成真实
  背压。完成标记由 task done callback 排在所有已产生事件之后，因此异常/取消
  也严格遵循“先 drain 事件，再传播终态”。消费者提前退出会取消 Agent task，
  避免孤儿查询。

## 事件与前台架构（Step 74）

第一阶段把“查询推进事实”与“终端表现”彻底分开：

```
AgentLoop ── EventBus ──> bounded QueryEvent
                              │
                              ├─ SessionEventReducer ──> SessionViewState
                              ├─ StreamRenderer ───────> 正文/折叠缓存
                              └─ ReplSessionRunner ────> query/approval/queue 生命周期
                                                        │
PromptWriter <── status callback/500ms ticker ──────────┘
```

- 新事件：`QueryPhaseChanged`（model/tool/finalizing）、`ToolProgress`（短状态）、
  `ToolOutputDelta`（增量正文）、`PlanUpdated`、`UserInputRequested`。全部是纯数据
  AgentEvent，并在 stream-json 中各有稳定 `type/subtype` 映射。
- `ToolContext` 提供受控 `emit_progress/emit_output` 端口；具体工具不接触 EventBus。
  AgentLoop 在每次工具调用时绑定 call id 并统一脱敏后再发布事件。
- `run_command` 改为分块读取合并后的 stdout/stderr，首块到达即可推进状态栏与折叠
  缓存；保留现有超时、进程组清理、输出上限与敏感环境变量剥离语义。
- `SessionEventReducer` 是唯一 QueryEvent → ViewState 映射点。状态栏持续显示当前
  阶段、Step、工具完成数/总数、实时输出字符与耗时。
- `ReplSessionRunner` 位于 session 层，不导入 UI。REPL 注入 renderer/approval/status/
  notice 回调；审批仍经 ApprovalBroker，runner 自身从不读取 stdin。
- `PromptWriter` 用 suspend/resume 让输出临时占用终端但保留草稿、光标与 slash 菜单；
  状态行参与整体重绘并受线程锁保护，避免输入线程和事件循环同时改光标。
- 工具增量正文只更新 `ToolOutputRecord`，完成时仍默认折叠；用户通过
  `/tool-output <编号>` 主动展开，防止实时输出重新污染正文。

## 交互式 Agent 能力（Step 75）

第二阶段补齐模型主动请求输入与结构化计划，同时保持内核不依赖 stdin/终端：

```
ask_user Tool ─┐
               ├─ ToolContext.request_user_input/emit_plan（窄端口）
update_plan ───┘                 │
                                 ▼
                         AgentLoop 脱敏/typed events
                                 │
                         UserInputBroker / EventBus
                                 │
                    ReplSessionRunner / reducer / renderer
```

- `ask_user` 的 schema 包含 `question/options/allow_free_text`。AgentLoop 先同步登记
  broker 请求，再发布带稳定 `request_id` 的 `UserInputRequested`，随后等待 Future；
  回答结束后发布不含正文的 `UserInputResolved`。这样 UI 不会因事件/等待注册竞态而
  丢失答案，敏感答案也不会重复进入 renderer 或 recorder。
- `UserInputBroker` 位于 session 层，支持排队、选项序号/精确值归一化、无效答案保留
  请求、取消全部等待、关闭 fail-closed。REPL 的字符编辑器只提交答案，不直接操作工具
  的 Future；回答作为 `tool_result` 回填，不写入 user history。
- `update_plan` 接收完整步骤快照，校验状态后发布 `PlanUpdated`。状态限定为
  `pending/in_progress/completed`，最多一个 `in_progress`；它与 `ask_user` 都声明为
  本地只读工具，因此在 read-only/plan 模式仍可用。
- `SessionViewState` 保存 plan 的 items/statuses/explanation 与 input 的 options/
  free-text 语义；`/plan` 在运行中和空闲态都可查看。任务结束保留最近计划，下一轮开始
  时重置。
- 无交互前台（print/JSON/单次 stream-json/子代理）不注入 UserInputPort；`ask_user`
  立即返回失败工具结果，避免后台任务永久挂起。

## 进程入口分流

控制台脚本和 `python -m krelcode` 都进入 `entrypoints/cli.py`，与 Claude Code
的 `cli → main` 分层一致：

```
argv
 └─ cli.py（仅标准库）
     ├─ argv[0] == --version → 直接输出版本（不读配置、不导入 Agent）
     └─ 其他 → 延迟导入 entrypoints/main.py
                ├─ 完整 argparse/help
                ├─ doctor / init → 仅加载命令所需模块
                ├─ review → 延迟加载 review runner
                └─ 对话 / -m → 最后才导入 app.py 组装 Provider/MCP/Sandbox/UI
```

这里不做两套参数定义：fast-path 只识别无歧义的版本请求，所有正式参数语义仍由
完整 parser 负责；未识别参数原样转发。未来 bridge/daemon/background/template
runner 可在 cli 层增加独立动态导入项，而无需把对应实现拉进默认启动路径。

## CLI 子命令（Step 28）

- `krelcode doctor`：离线环境自检（不发起网络请求）——Python 版本、
  provider key（脱敏 mask_key，全 key 不落屏）、默认 provider 可解析、
  配置文件（显式 --config 必须存在；缺省探测报告命中项）、git/工作区可写、
  MCP stdio 命令存在性（url 型仅提示）。三态 ✓/⚠/✗；有 ✗ → 退出码 1（CI 可判定）
- `krelcode init [--force]`：脚手架生成 krelcode.toml（provider/MCP/hooks
  注释模板）与 AGENTS.md 规则骨架；默认不覆盖已存在文件
- 子命令为可选位置参数（nargs="?"），既有 flag 式用法完全兼容

## 成本感知（Step 30）

- **Anthropic prompt caching**（adapter 内实现，其他层零感知）：默认开启，
  3 个 ephemeral 断点——tools 最后一个声明、system（block 化）、
  最后一条消息的最后一个 content block（str content 自动转 block；
  tool_result 合并消息只标末块）；`prompt_cache = false`（TOML）可关。
  缓存命中 input 按 ~10% 计价，长会话成本大幅下降；
  usage 新增规范化键 cache_read / cache_creation
- **模型目录**（agent/pricing.py）：价目 + 上下文窗口双表，共享「精确→最长前缀」
  匹配；`default_budget_for(model)` = 窗口×75% → --context-tokens 未显式指定时
  按模型自适应压缩预算（未知模型兜底 100k）；doctor 显示窗口与推导预算
- **OpenAI 缓存 usage 归一**：prompt_tokens_details.cached_tokens →
  cache_read，且从 input_tokens 扣除——统一为 Anthropic 语义（input 不含缓存），
  成本公式对两家一致且不双计；total_tokens 保留 provider 原值
- **价目表与成本估算**（agent/pricing.py）：内置主流型号 USD/1M 参考价
  （标注为估算，非计费依据）；匹配规则精确 → **最长前缀**
  （gpt-4o-mini-2024-07-18 → mini 价而非 gpt-4o 价）；未知模型返回 None，
  UI 不显示成本（不造假）；UsageTracker(model) 每次响应累计
  session.cost，/cost 与 -m [usage] 显示 `估算成本 $x.xxxx`，
  JSON 输出带 cost_usd 字段

## REPL 运维命令（Step 32）

- **/model [provider/]model**：运行时热切换——重建 provider（保留重试包装）→
  原子替换 loop.llm 与 ContextManager.llm → 同步 UsageTracker.model（成本核算）→
  预算未显式指定时按新模型窗口重新推导。缺 API key / 未知 provider →
  返回失败描述且**不破坏原 provider**（先建新再替换）；mock 基座要求显式
  provider（"openai/gpt-4o-mini"）
- **/allow [add|remove <程序名>]**：审批白名单的用户管理面（此前只能靠
  审批时答 always 累积）；持久化到 <workspace>/.krelcode/allow_rules.json
  （该文件对 LLM 写保护——管理权只属于用户）

## 多模态图片输入（Step 37）

- core 新增 `ImageRef(media_type, data_b64, source_path)` 与
  `Message.images`（仅 user 消息有意义）；wire 转换锁在各 adapter：
  OpenAI `image_url` data URI / Anthropic `source.base64` / Gemini
  `inline_data`；非 user 角色挂 images 时防御性忽略
- 入口：`@shot.png` 自动识别图片扩展名（png/jpeg/webp/gif）→ base64
  附件（5MB/4 张上限），正文留 `[图片 x 已附件]` 占位；文本与图片配额独立
- 持久化：JSONL 只存 source_path（每轮全量快照 × base64 会撑爆文件），
  恢复时按路径重载，源文件已删则静默丢弃；Markdown 导出以 🖼 占位

## review 子命令（Step 38）

`krelcode review` 是 headless 单次任务模式的垂直应用：
- **只读工具集**（read_file/grep/glob/find_refs/repo_map）——模型可拉取
  上下文核实疑点，但结构上没有写/执行能力，无需审批链即安全
- 输入组装：git diff（--staged 可选）+ 未跟踪文件名单 + --focus 重点；
  diff 经 truncate_middle 截断（100K 字符）
- 输出：文本模式打印评审 + [usage]；JSON 模式与 -m 同构
  （ok/review/usage/cost_usd/tools_used/denied）
- 复用失败安全的 _git runner；非仓库/无变更 → 友好提示，退出码 0

## 分层测试策略（每层可独立验证）

| 层 | 测试方式 |
|----|----------|
| core | 消息序列化 roundtrip；事件总线订阅/退订/异步 handler |
| llm | SSE 解析器逐帧断言；OpenAI/Anthropic adapter 注入 FakeTransport（流式、tool 聚合、错误路径全覆盖）；registry 优先级 |
| agent | 注入 MockProvider + 假工具，纯内存断言循环、错误恢复、max_steps、拒绝路径、事件序列 |
| tools | 不碰 LLM，直接 execute + tmp 目录 |
| safety | 构造 ToolCall 打 policy，断言 Decision |
| ui | （Step 8）喂录制事件流断言渲染 |

测试统一用 `asyncio.run(...)` 驱动协程，不引入 pytest-asyncio（保持零依赖）。

## Stream JSON 与 Python SDK（Step 73）

入口边界保持三层：

```
CLI / Python SDK
      │  UTF-8 JSONL (krelcode.stream-json v1)
session.stream_json          纯协议：输入解析 + QueryEvent 编码
session.stream_json_runner   会话策略：多轮、request_id、interrupt/end、终态
      │  QueryEvent(turn_id, sequence)
agent.QueryEngine / AgentRuntime / EventBus
```

- 协议层不导入 Provider、UI、MCP 或会话持久化实现；它只把内部 `QueryEvent`
  映射为稳定消息，保证每个 turn 恰好一个 `result`。
- runner 用独立 stdin reader 与 turn 内控制监视器，使 `interrupt` 能在流式事件
  等待期间真正调用 `AgentRuntime.stop()`；`end` 是优雅关闭，不丢弃已排队 user。
- SDK 通过 `asyncio.create_subprocess_exec` 启动 `python -m krelcode`，只负责
  请求路由、事件消费、超时和进程回收。它复用组装根，因此新增 Provider/工具/
  MCP 能力不会出现 CLI 与 SDK 两套语义。
- stdout 是纯协议面，所有启动日志走 stderr；JSON 对象在 dumps 前递归脱敏，
  SDK 对 stderr 异常尾部再做一次模式脱敏。API key 永不进入 argv。
- `result` 携带本轮/累计 usage、成本、LLM 调用数、耗时和 `duration_ms`；
  `success/cancelled/error` 都是可机器判定的唯一终态。

## P2 差异化能力的边界（Step 97）

- **LSP**：`lsp/client.py` 只负责 JSON-RPC framing、请求/通知和进程生命周期；
  `lsp/manager.py` 负责按扩展名懒启动与文档同步；`lsp/tools.py` 暴露窄的只读
  diagnostics/hover/definition/references。服务端 stdout/stderr 都有界，单服务器
  失败隔离，LSP 工具不直接写文件。
- **Output Styles**：样式是带 frontmatter 的 Markdown 指令包，来源固定为 user /
  project / configured；合并规则同名后写入覆盖前者。活动样式只替换一个带 source
  的 `style:output` PromptFragment，不改变 base prompt，也不进入可执行 Hook 链。
- **Status Line**：`ui/status_line.py` 是纯模板渲染器，变量白名单固定，控制字符被
  清理；REPL 每 150ms 只使用内存快照，Git 分支在会话启动时读取一次。
- **Web**：`web/http.py` 先解析 DNS 并对全部地址做公网校验，再用已验证 IP 建立
  连接并保留 TLS SNI；重定向逐跳重新校验，限制端口、Content-Type、Content-Length
  和解压后大小。`web/tools.py` 将网页内容包在 untrusted envelope 中，搜索由
  `SearchBackend` 协议解耦，密钥只从环境变量读取并放在内部 Header。
- **TypeScript SDK**：与 Python SDK 共享 `krelcode.stream-json` v1 子进程边界；
  负责 JSONL 路由、request_id 队列、中断、超时和 stderr 脱敏，不复制 Agent
  业务实现。

## Instruction / Evidence 权威分层（Step 102）

Prompt 来源按权威而不是按文件格式分类：

```text
Instruction sources
  Managed / User / Project / Agent Definition

Evidence sources
  Memory / Web / MCP / Tool Result / Retrieved Document
```

`agent/evidence.py` 定义 `SourceRole`、`InstructionSource`、`EvidenceSource`、
`EvidenceRecord` 与唯一 renderer。Evidence 使用
`<evidence_set role="non-instruction">` envelope，逐项携带 source/scope/kind/origin/
trust/requires-validation；所有正文和属性先做 HTML escape，因此工具输出或记忆正文不能
闭合 envelope 并注入新的 system 标签。

Memory 不再拥有独立的 authority 语义：

- `MemoryContextSource` 将 `MemoryEntry` 映射为 `EvidenceSource.MEMORY`。
- PromptAssembler 固定映射到 L7 Evidence、turn stability、非 cacheable；Memory 变化只
  影响 `dynamic_hash`。
- Resume 任务检索、子代理筛选记忆和 `memory_search` 复用同一契约。
- MCP initialize instructions 从 L6 catalog 降为 L7 Evidence；它仍是 server guidance，
  但不能伪装成受信任目录或行为指令。
- `/context why` 与 `/context sources` 展示 `role=evidence`、`instruction=false` 和
  `requires-validation=true`，metadata 只含 ID/scope/count/origin 等无正文字段。

Instruction 侧仍由 managed/user/project/agent-definition 分类器收口，Evidence 分类器对
`data`/`untrusted` 输入返回无指令来源，避免两套 taxonomy 在后继 adapter 中漂移。
