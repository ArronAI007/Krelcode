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
│   ├── tool_playbooks.py # 工具级 L5 操作规则与 advertised tools 选择
│   ├── workflows.py  # 任务分类、L9 Workflow Fragment 与执行策略
│   └── prompts.py    # 系统提示词组装
├── contracts/   # 跨层稳定契约：Runtime / Permission / Tool / Sandbox Port
├── core/        # 零上层依赖的基础类型、事件、消息、进程与序列化工具
├── tools/       # 工具层（实现 Tool 端口）
│   ├── base.py       # 历史兼容入口；新代码从 contracts 导入 DTO
│   ├── registry.py   # ToolRegistry -> OpenAI tools schema
│   └── builtin/      # 每个工具一个文件，互相独立
├── skills/      # 文件系统型 Agent Skills：发现/解析/渐进加载/资源/脚本
├── plugins/     # 轻量本地插件：manifest → 隔离 import → Tool 适配/命名空间
├── security_review/ # Security Review Workflow、只读工具池、结构化报告与 runner
├── safety/      # 策略层（实现 ApprovalPolicy 端口）
│   └── policy.py     # Decision / AllowAllPolicy（Step 6 加沙箱与交互审批）
├── session/     # 交互会话层 + 状态适配器
│   ├── input.py      # InputEnvelope → InputProcessor → InputAction
│   ├── attachments.py # @文本/图片展开（工作区约束与预算）
│   ├── state.py      # SessionViewState：idle/running/stopping 与 turn 现场
│   ├── commands/     # CommandSpec/Registry → dispatcher/handlers
│   │   ├── builtin.py # 40 个内置命令元数据与 help 渲染
│   │   ├── registry.py # 解析、别名、隐藏/启用、运行态与来源冲突
│   │   ├── sources.py # Skill / MCP Prompt → dynamic CommandSpec
│   │   └── handlers/  # 会话、上下文、Skill、MCP 等执行器
│   ├── controller.py # SessionController：命令/任务/steering，不依赖终端
│   ├── runner.py     # headless/REPL 运行策略，不导入具体 UI
│   ├── envelope.py   # SessionEnvelope：session/turn/sequence + 轻量 payload
│   ├── event_router.py # reducer、前台与观测 sink 的唯一事件分发点
│   ├── restore.py    # 会话恢复
│   └── recorder.py   # JSONL 持久化（事件总线订阅者）
├── ui/          # 终端交互：输入、渲染、展示型命令
│   ├── conversation_host.py  # 前台状态机、动作优先级、队列与 quick shell
│   ├── terminal_presenter.py # 输入框、状态栏、面板、审批与 diff 展示
│   └── repl.py     # 兼容入口；run_repl 仅构造 ConversationHost
├── bootstrap/   # 具体对象装配与进程生命周期
│   ├── composition.py # Provider/MCP/Hook/Tool/Runtime 具体工厂
│   ├── application.py # BootstrappedApplication：可运行应用图 + 资源释放
│   └── main.py        # AsyncMain：只负责 build→run→close
├── entrypoints/ # 进程入口：light cli fast-path → full main → lazy bootstrap
│   ├── cli.py   # --version 零配置/零 Agent 依赖；其余 argv 原样下发
│   └── main.py  # 完整参数解析 + doctor/init/review/security-review/REPL 路由
├── app.py       # 向后兼容 facade：重导出历史 app 符号
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
bootstrap/ = composition root，只负责创建与注入，不属于业务层
app.py = 向后兼容 facade；AsyncMain 只拥有进程生命周期
```

跨层 DTO 统一由 `contracts/` 提供；`tools` 不得反向依赖 `agent/session`，`safety`
不得依赖 `tools`，生产业务包不得依赖 `benchmarks`。`benchmarks` 只消费生产实现，
CLI 入口是唯一允许把 benchmark 命令接入进程的边界。

所有顶层生产包必须在 `tests/test_architecture.py` 的依赖白名单中声明允许的边。
测试会构建包级依赖图并用 Tarjan 算法拒绝任何强连通分量（SCC），同时禁止使用
`tools.base` 作为跨层 DTO 入口。`bootstrap/`、`app.py` 与各 `__main__.py` 是
组合根/进程边界；具体运行期装配由这些边界注入。`app.py` 不再持有装配实现。
`app.py` 不再持有装配实现。兼容 Facade 的旧路径、新路径、弃用版本、
计划删除版本和内部引用基线统一登记在 `architecture/compatibility_facades.toml`；
Facade 仅允许 re-export，新生产代码只能使用 `new_imports`。详见
`docs/compatibility-facades.md`。

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
#   · Windows static：cmd.exe 语法审计 + USERPROFILE/AppData 重定向；无内核 FS/网络隔离
#   · auto 会真实 probe 后端，不可执行时退回 static 并在启动日志告警
#   · static 只是兼容护栏，无法抵御解释器代码的动态绕过，不宣称内核级隔离
#   · ToolContext 默认自动挂载；Subagent isolated worktree 用 rebind 切换根目录

# tools/process.py —— 唯一跨平台进程语义
# ProcessPlatform(family, system, shell, shell_args, shell_dialect)
#   · POSIX: /bin/sh -c + start_new_session=True + killpg(SIGTERM/SIGKILL)
#   · Windows: COMSPEC /d /s /c + CREATE_NEW_PROCESS_GROUP + taskkill /T [/F]
#   · run_command/Job/Hook/Skill/MCP/LSP/SDK/ripgrep/context-git 共用同一入口
#   · graceful stop 有界等待，超时自动升级 force；返回内容轻量 capabilities

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
- **能力协商**：`ProviderCapabilities.supports_tools` 为 False 时 AgentLoop 不传 tools；
  `ProviderCapabilities.cache` 独立声明 none/implicit/explicit、显式断点上限和缓存 usage
  字段。`CacheCapabilities.negotiate()` 把用户偏好解析为不可变 `CacheNegotiation`，adapter
  只按 effective mode 生成 wire，不在不支持的端点上写 `cache_control`。
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
- 跨平台：POSIX 走独立 session/process group，Windows 走 new process group + `taskkill /T`；前台命令、后台 Job、Hook、Skill、MCP/LSP/SDK 与搜索子进程统一由 `ProcessPlatform` 启动和回收，graceful stop 超时自动升级 force

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
- 缓存模式先由 Provider/model 能力与用户 `prompt_cache` 偏好协商，再从最终 wire payload
  运行 `adaptive-prefix-v1` planner 并提取 segment。Anthropic 在 tools、stable system 与
  messages 的最终前缀顺序中选择不超过 4 个显式 `cache_control` 断点：优先稳定
  tools/system 锚点和当前消息 frontier，剩余槽位用于历史中段 checkpoint；低于模型最小
  缓存长度时不写该断点；dynamic system 虽不可缓存，仍计入其后的消息前缀长度。
  OpenAI/DeepSeek 标记为 `implicit` prefix-cache 机会；Gemini 2.5+
  标记为 server-managed `implicit`，旧模型为 `none`。segment 只保存
  kind/token/char/hash/stability/cumulative tokens，不保存正文。
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

`CompactSummary v2` 的模型契约固定为 `goal`、`completed`、`decisions`、`files`、
`tests`、`open_issues`、`next_steps`、`critical_constraints` 八个字段。文件带
path/status/note，测试带 name/status/detail；模型输出必须包含全部字段且不能携带
version、commands、errors、handoff notes 或其他扩展字段。缺少字段、非法类型、枚举、
超限数组或重复 key 都会触发降级。`source/generation/compacted_messages/created_at`
只作为内部审计元数据持久化，不会回灌给摘要模型。wire 中使用短别名并在
`Message.provider_meta` 保存审计元数据，减少重复 compact 的 token 成本。

- 每次 compact 都从已有摘要继续合并；旧 goal/file/test/error/open issue 不会因模型
  本轮“没说”而消失，generation 单调递增，compacted message 计数累计。
- 自动模式只在已完成 turn 的 user 边界切割，当前 turn（含 steering 后的全部消息）
  保持原样；若中段只剩上一条摘要，不会重复摘要同一内容。
- 高低水位是触发与目标而非两个阈值循环：高水位判断完整请求预算，低水位用于选择
  更激进的合法切点；若无法达到低水位，选择最激进的合法 turn 边界。
- compact 前完整 history、摘要、边界、水位、来源节省量、精确 Working Context 快照和
  任务文件 SHA-256 写入 `.krelcode/compact-checkpoints/`，原子替换、fail-open、
  SecretRedactor 脱敏。
- `/compact preview` 展示按来源（tool results/conversation/total）的预计节省量与
  low-watermark 是否可达；正式 `/compact` 展示 checkpoint ID。
- `ContextCompressed`、TUI Context 面板、recorder、stream-json 和子代理 feed 只传播
  内容无关元数据，不复制摘要正文。`/rewind` 匹配摘要 digest 后优先恢复 compact 前
  完整现场与 Working Context，再校验文件 Hash；变化或缺失的文件事实会失效并记录为
  open issue，压缩状态则从恢复后的 history 重新建立。

## 交互与会话（Step 8）

- **StreamRenderer**（ui/render.py）：EventBus 订阅者，LLMStreamDelta 逐字流式打印
  （助手文本用 `⏺`，工具结果用 `⎿`）；工具活动/审批/压缩事件打断流式行后续接；
  RunFinished 兜底打印未经流式的系统消息（max_steps/卡死中止），并避免模型自带
  尾部换行时产生空行。write/color 可注入，测试无需终端
- **REPL**（ui/conversation_host.py + ui/terminal_presenter.py）：`run_repl` 仅构造
  ConversationHost；Host 负责输入分类、业务/UI/Control 分流和前台状态机，Presenter
  负责输入框、状态栏、面板、审批与 diff 渲染。输入先经 InputProcessor 分类，
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
  · StreamableHTTPTransport（2025-06-18 规范，url 默认）：单端点 POST，
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
- **反向请求**（Step 35/P1-1）：服务器→客户端的 sampling/createMessage（回调
  app 注入的 sampler 闭包，用共享 LLM 完成补全）、roots/list（工作区根）、
  ping 由 MCPClient 统一应答，未知方法回 -32601；initialize 时按配置声明
  sampling/roots 能力。协议时序教训：notifications/initialized 必须 await
  实发（fire-and-forget 的调度延迟会让它晚于后续请求到达，违反规范）。
  MCP 2025-06-18 elicitation 通过 `session/elicitation_bridge.py` 接入
  UserInputBroker；协议层只暴露扁平 primitive Schema，拒绝嵌套/引用/组合、
  default 与敏感字段，并限制 16 KiB Schema、12 字段、2,000 字符 message/
  answer、50 enum 与每分钟 10 次请求。无前台运行时不声明 capability，违规调用
  自动 decline；正常应答按 accept/decline/cancel 三态二次校验后才回传。
- **Conformance matrix**：`mcp/conformance.py` 从握手结果和已加载目录生成
  content-light 报告，覆盖 2024-11-05/2025-03-26/2025-06-18、stdio/SSE/
  Streamable HTTP、生命周期、tools/resources/prompts、sampling/roots/
  elicitation、分页/list_changed、异常与尚未实现的 completion/logging/progress/
  resource subscribe。`/mcp conformance [server]` 不执行远端读取或工具调用。
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
- **fork_context**（Step 42 初版，Step 92 收口）：当前语义是兼容/血缘标记，
  不复制父历史；子代理仍只收到显式 task、继承指令与本任务相关 Memory。
  handle.forked 与 sidechain 的 parent_history_inherited=false 用于审计。
- **委派 Meta Prompt**（Step 118）：`subagent/prompt.py` 统一生成父 Agent 的
  spawn_agent 操作契约：先注入当前状态、已验证事实与假设、相关路径/符号/命令、
  禁止范围、交付物和验证方式；明确禁止让子代理猜测；提供正例/反例。
  fork_context=true 追加现场搬运指导，要求区分确认事实/未决假设，并写明已完成、
  剩余目标与阻塞点。该 Prompt 只进入 ToolPromptSpec/L5，不注入子代理 task 正文，
  不扩大工具、审批或 Sandbox 权限。
- **独立 Verifier**（Step 119）：`agent/verification.py` 定义内置 `verifier`
  Agent Definition 的 canonical Prompt。它不描述“如何实现”，而是先建模验证
  Agent 的失败模式：停在一份绿色输出、把无关失败合理化、把编译当行为正确、
  相信实现者总结、因迁移简单而跳过 rollback、因 refactor 而默许测试改写。
  `RECOGNIZE_YOUR_OWN_RATIONALIZATIONS` 为每种借口规定必须补取的证据；
  `OUTPUT FORMAT` 固定为可机器检查的 verdict/claims/checks/behavior/rollback JSON。
  检查矩阵覆盖 backend/API、frontend、iOS/Android、数据库迁移、data/ML、
  infrastructure、dependency upgrade 与 refactor；组合变更取并集。内置定义
  只开放读取和命令工具，不含 write_file/edit_file，项目/全局同名定义可覆盖。
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

- **输入历史导航**：`ui/terminal_presenter.py` 使用的 `InputHistory` 只保存当前 REPL
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
- **REPL 可测试化**：ConversationHost 接受 input_fn 注入（默认仍是终端行编辑器）；
  ScriptedInput 驱动下全部 /命令均有自动化回归
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

安全边界：插件代码仍是用户显式安装的本地可信 Python，不等同于 MCP 远程工具的 readOnlyHint；其读写/命令能力继续经过 Tool 实现、权限模式、审批与 Sandbox。普通目录发现不会联网补装依赖；显式 `registry install` 才递归安装签名依赖。当前仍不提供不可信代码的进程级沙箱。

### 插件信任、依赖图与可信 Registry（P1-2）

P1-2 把插件从“目录代码”提升为可验证的软件包，同时保持零第三方运行时依赖。

1. **签名边界**：`plugin.sig.json` 是 detached Ed25519 签名；签名 payload 包含 `plugin/version/registry/key_id/manifest_sha256/files_sha256/files_count`，采用排序 JSON。`files_sha256` 对排序后的相对 POSIX 路径、逐文件 SHA-256 和大小做二次 Hash，排除签名、registry 安装标记和编译缓存。客户端只存公钥；`public_key_from_seed` 与 `sign` 仅作为发布工具/互操作测试 helper。
2. **验签时机**：`PluginManager.load()` 在动态 import 之前验签；安装复制到 staging 后再次验签并比较完整包摘要，防止 TOCTOU。更新拒绝 SemVer 降级、撤销 key 后继续更新、空签名降级和静默更换 signer/registry。
3. **信任策略**：`off/prefer/signed/strict` 由 `PluginTrustPolicy` 解释；本地未签名开发插件在默认 `prefer` 下可用，但存在签名时必须有效。未知/撤销 key 与 Hash 不匹配不能通过模式绕过（`off` 除外）。
4. **依赖图**：manifest 的必需/可选依赖和冲突进入独立 `dependencies.py`。SemVer 解析不使用 `packaging`；缺失、版本不兼容、禁用、冲突和循环使用 Kahn 拓扑解析并 fail-closed。失败只阻断受影响子图，加载顺序稳定，可选依赖缺失不阻断。
5. **可信 registry**：`registry.json` 列出 name/version/path/yanked，`registry.sig.json` 对 index SHA-256 签名。`PluginTrustStore` 保存 registry 路径与按 registry 隔离的公钥，文件权限 0600；registry 包路径拒绝 `..` 和任一组件符号链接。远端下载不是本阶段的隐含行为，registry 当前是可挂载/同步的本地目录。
6. **生命周期与观测**：`PluginRecord` 增加 trust/signer/key/registry/integrity/dependencies/dependents；`/plugins verify`、`/plugins deps`、`/plugins registry`、`/plugins trust` 暴露内容轻量状态，不输出组件正文或私钥材料。

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

## MCP Prompt / Tool Description 安全边界（Step D）

MCP 服务器提供的是远端数据，不是本地指令。`mcp/security.py` 是 Tool description、
Schema annotation、Prompt catalogue、`prompts/get` 与 server instructions 的唯一加固
入口，避免每个适配器各自实现一套脱敏和截断逻辑。

1. **不可信文本**：先移除 NUL/控制字符、双向控制字符和不可见格式字符，再执行已知
   密钥与模式脱敏。Tool description、Prompt 描述和 instructions 对常见“忽略先前指令”、
   “切换 system role”、提示词/密钥外泄等模式做 defense-in-depth 中和；真正的安全保证
   来自 Instruction/Evidence 分权和固定 trust envelope，而不是关键词过滤。
2. **Tool description**：远端正文限制为 1,024 字符，并放入
   `[untrusted-mcp-tool-metadata]`；模型同时看到 `instruction=false` 语义和
   `requires_validation=true`。远端文本中的 `<`/`>` 被转义，不能伪造本地边界。
3. **inputSchema**：限制 source 128K、depth 24、nodes 2,048、单字符串 16K、字符串
   总量 96K、键/列表数量。description/title/examples/default 等模型可见注解单独脱敏与
   中和；语义字符串若含控制字符或指令型内容整份 Schema fail-closed。非法 Schema
   回退为 `{type:object, properties:{}, additionalProperties:false}`，执行直接拒绝。
   合法 Schema 计算 `source_digest/safe_digest`，内容轻量记录进入 Tool Prompt Surface。
4. **Prompt**：`prompts/get` 继续经过 Surface Contract、Schema、timeout、输入/输出上限
   和双层脱敏，随后再执行控制字符清理和有界投影。Tool 与 slash command 共用
   `render_mcp_prompt_evidence()`，输出的 `<evidence>` 和 `<mcp_prompt>` 均声明
   `trust=untrusted`、`instruction=false`、`requires_validation=true`；正文整体 HTML
   转义并限制 240K，禁止伪造结束标签。
5. **Catalogue / instructions**：`prompts/list`、command help、Prompt Surface hash、
   `/mcp` 可见描述和 L7 MCP Evidence 使用同一个清洗路径。observation 只保留字符数、
   digest、redacted/truncated/neutralized 状态和 signal 名称，不记录远端正文。

工具替换仍在 Registry 层原子完成；热重载后新 Tool 实例携带新的 description/schema
hash，因此 Provider tool catalogue 与 Prompt cache 身份会随安全化后的 wire 内容更新。


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
          ├─ ProcessPlatform(subprocess_kwargs, shell_argv, tree-kill)
          ├─ create_subprocess_exec(stdin=DEVNULL,
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
- 默认兼容边界：`[daemon] enabled=off` 时 manager 仍是进程内的；命令通过 `&` 自行派生的
  后台子进程若仍持有 pipe，主进程退出后会结束残留进程组，因此正确用法是让目标命令在前台
  运行并由 `job_start` 托管。启用 daemon 后使用下述持久化生命周期。

## 持久化 daemon（P0-4）

daemon 是 workspace 级、单实例的基础设施层，不进入 Agent Prompt/tool schema。它拥有
Job 与 Subagent 的真实生命周期，REPL/headless/SDK 只持有窄 RPC client：

```
DaemonClient ── authenticated JSON request ── DaemonServer
                                                ├─ ShellJobManager (live pipes/tasks)
                                                ├─ SubagentManager (lazy Agent runtime)
                                                ├─ JobRecordStore (metadata + seq JSONL)
                                                └─ AgentRecordStore (content-light state)

.krelcode/daemon/
├── daemon.json        instance/pid/endpoint；绝不包含 token
├── auth.token         随机 bearer token；POSIX 0600
├── daemon.lock        pid + instance + created_at，带 stale/PID-reuse 校验
├── jobs/<id>.json     durable metadata
├── jobs/<id>.events.jsonl  跨 client 的 sequence 输出
├── agents/<id>.json   status/task/definition/model/result/sidechain 引用
├── requests/          file-RPC fallback 请求
├── responses/         file-RPC fallback 响应
└── logs/daemon.log
```

**传输协商**：POSIX 优先 Unix domain socket（路径过长时使用 hash 后的 per-user temp 路径）；
Windows 使用 loopback TCP 随机端口；托管沙箱若禁止 bind，则回退 file RPC。三种 transport
都走相同的 `RPCRequest/RPCResponse`、协议版本、request id、4 MiB 上限和 bearer auth。
file RPC 使用原子替换传递单条消息，目录 0700；它不是网络服务，也不需要第三方依赖。

**所有权与退出语义**：

```
client start job/spawn agent
  └─ daemon owns process/task
      ├─ client disconnect → no effect
      ├─ AgentRuntime.close() → RPC adapter shutdown only
      ├─ explicit kill → process tree / agent Task 终止
      ├─ daemon stop → active work 终止并写 terminal record
      └─ daemon crash → next start reconcile running → lost
```

daemon 的 `idle_timeout` 只在没有运行中 Job/Subagent 时生效；运行中任务不会因客户端闲置
被杀。单实例锁记录 `pid + instance_id + created_at`，不能只凭“PID 存在”判断所有权；
PID 被复用时会回收 stale lock。daemon.json 写入不包含 token，客户端不能从状态/日志恢复
bearer 权限。

**Job durable 语义**：live `ShellJobManager` 继续负责 pipe、条件等待、进程树终止和内存
tail；`ShellJobStarted/Output/Finished` 旁路写入 metadata 与 append-only JSONL。重启后：
completed/failed/killed 可由 list/output 读取；running 记录标记 `lost`，保留最后 sequence 与
日志路径，并明确拒绝按旧 PID kill。这样既不丢失审计事实，也不把 PID 复用变成安全漏洞。

**Subagent durable 语义**：daemon 按需构造与生产 REPL 相同的 `SubagentManager`（同一
Tool/Sandbox/Permission/MCP/PromptAssembler 路径），保留 pause/resume/kill/feed/definition
与完整 sidechain。`agents/<id>.json` 只保存 content-light 状态与 sidechain 引用，不复制
Prompt/工具正文。客户端断开后 Task 继续；daemon 正常停止会终止运行 Task；daemon 崩溃时
旧 coroutine 不可能原地恢复，下一次启动标记 `lost`，completed Agent 的 result/feed 仍可从
metadata + sidechain 重放审计。父 Agent history 仍不自动进入子代理，sidechain 仍不自动回注主上下文。

**REPL 事件桥**：daemon Job 的实时输出按 sequence 轮询到 client 后重新发布为本地
`ShellJobStarted/Output/Finished`，因此既有 REPL 完成通知、recorder、hooks 与 stream-json
不需要知道 transport 是 socket 还是 file。模型上下文仍不会自动吸收 Job 日志，必须显式调用
`job_output` 保持游标。

## 交互式子代理（Step 81）

子代理不再只是 `spawn → wait` 的黑盒，而是有实时事件流、运行控制和可审计
sidechain 的后台 Agent。实现刻意拆成四个与 manager 解耦的组件：

```
subagent/
├── definition.py  AgentDefinitionManager：用户定义发现/覆盖/校验
├── builtin_agents.py Claude Code 兼容内置 Agent 的 canonical 行为契约
├── model_aliases.py  sonnet/haiku/opus 逻辑档位解析与配置覆盖
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
- **Definition** 先加载 Claude Code 兼容的 `Explore`、`Plan`、`general-purpose`、
  `claude`、`statusline-setup`、`claude-code-guide` 与 Krelcode `verifier`，再从
  `~/.krelcode/agents`、项目
  `.krelcode/agents`、项目 `.agents/agents` 发现 Markdown；项目覆盖全局与内置。
  frontmatter 提供 description、
  allowed-tools、isolated、fork-context、inherit-project-rules，正文是角色 system
  prompt。allowlist 只能缩小默认工具池；未知工具、坏 frontmatter 或空正文
  fail-closed/隔离。省略 definition 时默认 `general-purpose`。
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

### Claude Code 六内置 Agent 兼容（Step 121）

兼容层对齐可观察职责而不是复制私有 Prompt：`Explore` 与 `Plan` 使用 read-only
permission、无 MCP，并通过 `inherit_project_rules=false` 跳过 user/project/directory
规则（Managed 安全策略仍保留）；`general-purpose` 和 `claude` 继承全部子代理工具与
MCP，其中前者是缺省委派，后者是无专用匹配时的 catch-all；`statusline-setup` 只暴露
状态栏配置所需读写/语法工具并绑定 `sonnet` 档位；`claude-code-guide` 为 read-only 并
绑定 `haiku` 档位。用户同名 Definition 继续覆盖内置版本，`verifier` 作为 Krelcode
额外能力保留。

逻辑模型档位由 `subagent/model_aliases.py` 解析：`[agent_models]` 显式映射优先；
Anthropic 父 provider 默认映射到对应 Claude 模型；DeepSeek/OpenAI-compatible 父
provider 在无显式映射时继承父模型，避免要求无关 Anthropic key。`/statusline [要求]`
通过同一 `SubagentManager.spawn(definition="statusline-setup")` 路径启动子代理，
不绕过审批、Sandbox 或文件边界。

## Prompt/Context Fragment Registry（Step 82）

system prompt 不再由 App、Skill 热重载和 `/context refresh` 各自拼接。
`agent/fragments.py` 负责来源登记、作用域、手动启停与快照版本；请求级最终分层和消息生成
由 `agent/prompt_assembler.py` 独占，AgentLoop 只消费 `PromptPlan` 与 `ProviderMessages`。

```
Fragment sources
├─ policy     → managed:cyber-risk        L0 · static · locked
├─ core       → core:base                 L1 · locked · cacheable
├─ rules      → managed:rules              L0 · static
│              user:rules                 L2 · session
│              project:rules              L3 · session
│              directory:rules            L3 · request-selected · cacheable
└─ definition → agent_definition:<name>   L4 · session
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
  `stable_system_blocks/dynamic_system_blocks`。Anthropic 原样保留 block 数组，由
  `CacheBreakpointPlanner` 根据最终 wire、Provider 上限和模型最小阈值在 stable block 上
  设置零到多个 `cache_control: ephemeral`；OpenAI/DeepSeek 等兼容端点按 stable → dynamic
  固定顺序合并，但仍保留 block 计数、边界与双 hash 供观测。
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
常规规则、Skill、Agent、Plugin、Hook 和 Sandbox 加载成功时不逐项打印；启动卡只展示
模型、工作区、会话、工具与 Sandbox 摘要。空闲状态下 Dock 默认收起，`Ctrl-O`、计划、
审批或任务运行需要时再展开。宽终端显示 `KRELCODE` ASCII 字标，窄终端自动回退为
紧凑单行标题；字标使用 Hermes 首页品牌蓝 `#0000F2` 真彩色，其他提示仍沿用原有
青色层级。

```
ui/tui/
├─ model.py       Panel / PanelItem 纯数据
├─ layout.py      固定高度边框、CJK 截断、tab/item hitbox
├─ controller.py  焦点、选择、键鼠路由
├─ mouse.py       SGR mouse + CPR cursor origin codec
└─ panels.py      SessionViewState → Tasks / Activity / Context / Review / Memory
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
- **流式绘制互斥**：模型文本/思考增量写入期间，状态轮询只更新缓存，不恢复 Panel
  Dock 与输入框；`RunFinished` 后的状态更新再统一恢复。这样周期状态刷新不会把
  面板插进尚未结束的回答正文。
- **状态栏原位更新**：运行耗时和阶段文本变化时只重写状态行，不再重绘 Panel Dock
  与输入框上下边界，避免高频刷新在滚动区域留下重复分隔线。
- **可点击组件**：TUI 默认关闭鼠标上报，避免 xterm mouse tracking（1000+1006）劫持
  终端原生拖拽选择与复制；仅在 `--tui-mouse` 或 `[ui].mouse=true` 时显式启用点击。
  只有启用鼠标点击时，PromptWriter 才在重绘后发出 CPR 查询；编辑器把
  `ESC[row;colR` 换算为 Dock 顶行。键盘模式下不发送查询，避免异步 CPR
  响应变成输入框中的可见转义文本。点击坐标仅用于纯 hitbox 命中，不依赖固定
  屏幕尺寸。退出/异常路径都关闭 mouse reporting。
- **Shell 快捷模式**：`!cmd` 由 InputProcessor 只做分类，REPL 在空闲时启动窄任务。
  `AgentLoop.execute_direct_tool()` 临时构造与普通 turn 相同的 RuntimeSnapshot、
  TurnToolPool 和 ToolContext，执行 `run_command` 时仍经过参数 schema、Hooks、
  风险审批、路径规则、Sandbox 和 secret redaction。执行事件临时旁路给 renderer，
  但调用与结果都不进入 `loop.history`；运行中拒绝并发执行，Esc/Ctrl-C 可终止进程组。
- **Memory 快捷模式**：`#text` 默认写 project 作用域；Step 86 进一步增加
  `#user:` / `#project:` / `#session:`。单条 2,000 字符、每作用域 200 条；常见
  token/key 形态和当前 SecretRedactor 值都会替换为 `***`。`#` 只打开面板。请求只加载
  有界 `MEMORY.md` 索引；主题正文通过 `memory_search` 按需读取。Memory 与 `AGENTS.md`
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
- L5 由唯一的 `ModePermissionContract` 生成：输入是实际执行的 `RuntimeSnapshot`、过滤后的
  `ToolPool.exposure_snapshot()`、`ApprovalPolicy` 与 `ExecutionSandbox`，正文和 diagnostics
  metadata 共用同一 builder。契约覆盖 run/permission mode、审批优先级、DENY/ASK 例外、Plan 门禁、
  tool/MCP allowlist、Sandbox backend/network/write scope 和 subagent 降权；子代理的 permission
  mode、tool allowlist 与 MCP allowlist 都取父级和 Definition 的较窄边界，工具目录只能列
  capability，不能扩权。
- 排序键固定为 layer → static/session/turn → priority → source/id；L0-L4 承载稳定权威，L5-L9 可按请求现场使用 turn fragment，L10-L12 的 turn 语义由装配器强制校验。
- `turn`、`data`、`untrusted` fragment 不得 `cacheable`；稳定断点只允许覆盖连续 cacheable 前缀。
- `managed:cyber-risk` 是独立的 L0 网络安全底线 Fragment，固定先于 L1 Base Prompt。
  它按可观察行为区分防御性安全工作和有害操作能力：允许安全开发、漏洞修复、防御检测、
  隔离样本分析、CTF 与有明确范围的授权测试；拒绝恶意软件、凭据窃取、未授权访问、
  破坏性攻击、规避审计、供应链投毒等请求。该 Fragment 为 static/cacheable/Managed、
  不可关闭、不可 compact，并进入主 Agent、全部子代理、Review、Benchmark 生产探针和
  legacy 字符串 Prompt fallback。安全规则正文受硬性长度上限约束，Context Cache Ledger
  将其纳入 policy instruction hash。
- 运行中切换权限模式会同步当前 policy snapshot、丢弃旧 turn pool、刷新 L5 attachment；下一个
  provider step 使用新 ToolPool，stable hash 不变而 dynamic hash / contract hash 更新。
- 当前 user 消息只出现一次，tool result 永远留在 history，不进入 system 或稳定前缀。
- `ProviderMessages` 是 Provider 的唯一输入；`ContextAssembly` 仅作为 `/context`、Resume 与
  checkpoint 的无正文兼容视图。

### 生产级 Base Prompt 与模型 profile

L1 Base Prompt 不再是一个不可归因的大字符串，而由固定 `core:base*` section 组成：
身份、operating loop、tool discipline、coding quality、instruction authority/safety、
verification、communication 和 completion。每个 section 都是 static/cacheable/managed、
不可手动关闭，并携带 section ID、profile、provider/model、匹配方式与 base hash。

`ModelPromptProfile` 只允许追加 optional section。共享 base section ID 被保留，profile
尝试复用或替换时在构造阶段 fail-closed，因此模型适配不能削弱安全与权限约束。
profile 解析顺序为：显式配置 → 最长 model pattern → provider family → `default`；
DeepSeek 即使运行在 OpenAI-compatible provider 上，也会先由 `deepseek-*` 模型模式命中。

主 Agent 在 composition root 解析 profile 后通过 `base_fragments()` 注册；`/model` 在
`auto` 模式重新解析并原子替换保留的 `core:base*` 命名空间，再刷新 PromptSnapshot 与
stable hash。子代理不继承父 history，但在实际 child provider/model 确定后独立解析
profile；共享规则、L7 Evidence Memory 和 PromptAssembler 路径保持不变。

## 显式 system cache boundary（Step 99）

Provider 层不再把 system prompt 当作一个不透明字符串。`PromptAssembler` 先产出 provider-
neutral 契约。`ProviderMessages` 在兼容字段之上显式保留请求通道：

```text
ProviderMessages
├─ instruction_blocks[]      # 行为约束，保留 source/channel
├─ evidence_channel          # Memory/Web/MCP 等待验证材料 + user-turn anchor
├─ messages[]                # user/assistant/tool 语义消息
├─ tools[]
├─ cache_boundary
└─ system_blocks             # 仅 instruction：stable + dynamic
```

Evidence 不进入 system prompt。`EvidenceChannel` 保存 blocks、独立 hash 和本轮 user-turn
anchor；adapter 将它序列化到对应 user message：OpenAI-compatible 使用兼容的 text content
（有图片时使用 text block），Anthropic 使用独立 text block，Gemini 使用独立 text part。
工具循环后续 step 继续锚定原始 user turn，不会把 Evidence 附到 tool result，也不会创建额外
synthetic turn。Tool Result 仍保持 provider 原生 tool message 语义，不搬进 EvidenceChannel。

```text
SystemPromptBlocks
├─ stable_system_blocks[]   # 连续可缓存 L0-L9 前缀，每个 Fragment 一个 block
├─ dynamic_system_blocks[]  # turn/session 动态尾部，永不设置显式缓存断点
├─ stable_hash
├─ dynamic_hash
└─ cache_control_boundary
```

- Anthropic 保留 block 数组，不提前 join；每个 stable block 独立序列化。显式
  `cache_control: {"type": "ephemeral"}` 由 Step 108 planner 在最终 wire 上决定，可在
  多个 stable system block、tools 与历史消息上形成不超过 Provider 上限的断点。没有
  stable block、低于最小缓存长度或缓存关闭时，不生成显式断点；dynamic block 永不缓存。
- OpenAI-compatible 按 stable → dynamic 固定顺序合并成一个 system message。合并只发生在
  adapter 的 wire 序列化阶段，`PreparedLLMRequest`、`ContextUsageSnapshot` 与
  `ContextPlanSnapshot` 仍记录原始 block 数、边界和 `stable_hash/dynamic_hash`。
- `stable_hash` 只覆盖 stable instruction block 文本；`dynamic_hash` 只追踪 dynamic
  instruction 尾部。动态环境、Working Context 或 reminder 变化不会使 `stable_hash` 改变，
  Memory/Web/MCP Evidence 则同时不改变 `stable_hash` 与 `dynamic_hash`，只改变独立
  `evidence_hash`。hash、block 数量和 source/id 可进入
  `/context plan|cache`、TUI、JSONL、stream-json 与 SDK，但任何记录都不包含 block 正文。
- `SystemPromptBlocks` 只接受 instruction block；尝试放入 Evidence 会 fail closed。
  `ProviderMessages` 同时提供 `instruction_blocks`、`evidence_channel`、独立
  `instruction_hash/evidence_hash` 与无正文 `channel_record()`。
  `PreparedLLMRequest` 记录 evidence block count/chars/hash 与 anchor，便于验证 wire 是否
  正确携带独立通道。
- 旧 Provider、旧 system message 或只有 `prompt_cache_boundaries` 的调用由
  `resolve_system_prompt_blocks()` 生成兼容视图：显式 block 优先，否则从 system 消息按固定
  顺序恢复 stable/dynamic 视图。生产请求仍只允许由 `PromptAssembler` 产生显式 block。

## Provider 缓存能力协商（Step 106）

缓存能力与用户意图分离，避免把某一家 provider 的请求字段写进共享 Agent 层：

```text
Provider adapter
  └─ ProviderCapabilities.cache
       ├─ modes = none / implicit / explicit
       ├─ preferred_mode
       ├─ max_breakpoints
       └─ reports_cache_read / reports_cache_creation / supports_ttl
                    +
[providers.*].prompt_cache
       auto | on | off | implicit | explicit
                    ↓
            CacheCapabilities.negotiate()
                    ↓
            CacheNegotiation
              requested_mode / effective_mode / supported_modes
              reason / fallback / max_breakpoints
                    ↓
            PreparedLLMRequest
                    ↓
      ContextUsageSnapshot / JSONL / stream-json / SDK
```

- `auto` / `on` 选择能力允许的最强模式；Anthropic 为 `explicit`，OpenAI-compatible/
  DeepSeek 与 Gemini 2.5+ 为 server-managed `implicit`。
- 请求 `explicit` 但只支持 implicit 时显式降级并标记
  `explicit_unsupported_fell_back_to_implicit`；请求 `implicit` 不会反向启用显式控制。
- `off` / `false` 阻止 Krelcode 生成显式控制并关闭本端缓存段观测；普通 OpenAI-compatible
  API 无法强制服务端停止隐式前缀缓存，因此不会声称已经关闭服务端缓存。
- adapter 的 build/wire path 只读取 `cache_negotiation.effective_mode`。观测字段只包含模式、
  原因、布尔值和计数，不包含 Prompt、工具 Schema、headers 或 API key。
- Gemini usage 的 `cachedContentTokenCount` 与 OpenAI `cached_tokens` 一样归一为
  `cache_read`，并从 inclusive `input_tokens` 中扣除，保持跨 Provider 成本语义一致。

## 自适应 Cache Breakpoint Planner（Step 108）

固定三个断点的问题在于：短请求会浪费 Provider 限制，长会话又无法把断点放在真正有复用
价值的历史区间。Step 108 将“能力协商”和“断点布局”拆开：

```text
final Anthropic wire payload
  → CacheBreakpointCandidate[]
      tools / stable system block / message
      order + item/block index + cumulative tokens + digest
  → CacheBreakpointPlanner(mode, max_breakpoints, minimum_cacheable_tokens)
      ├─ moving frontier
      ├─ stable system anchor
      ├─ tools anchor
      ├─ evenly spaced history checkpoints
      └─ capacity + below-minimum skips
  → CacheBreakpointPlan
      selected breakpoints / skipped reasons / strategy / token coverage
  → cache_control markers
  → PreparedLLMRequest.cache_breakpoint_plan
  → ContextUsageSnapshot / TUI / JSONL / stream-json / SDK
```

选择规则：

1. 单槽位总是选择最终 wire frontier，因为它覆盖此前全部前缀。
2. 多槽位先保留最新 stable system 与 tools 锚点；system 前缀天然包含更早的 tools。
3. 再为历史消息选择均匀分布的 checkpoint，最后才使用更早 stable system 槽位。
4. 所有候选按最终 wire 顺序稳定排序；相同请求得到完全相同的 plan。
5. `max_breakpoints` 来自 `CacheNegotiation`，绝不写超 Provider 上限。
6. Sonnet/Opus 默认最小 1024 tokens、Haiku 2048；`cache_min_tokens=0` 显式覆盖。
7. 低于阈值、缓存关闭或 Provider 非 explicit 时 fail-closed，不生成 `cache_control`；
   单个候选的累计前缀不足阈值时跳过该候选，不因更长尾部而保留无效断点。

## ToolPromptSpec 统一工具契约（Step 109）

工具此前同时由执行对象、动态目录和 Provider Schema 分别描述，容易出现“名字相同但描述/别名/操作规则漂移”的问题。Step 109 将模型可见部分收敛为一个不可变契约：

```text
Tool                         ToolDescriptor                 ToolPromptSpec
执行实例                      source/tier/权限/运行模式       name + Provider description
execute(args, ctx)            read_only/mcp_server            parameters(JSON Schema)
                                                              instructions / when / examples
                                                              aliases / source / version
                                                              playbook metadata
        \___________________________ Registry ___________________________/
                                      │
                              TurnToolPool snapshot
                       ┌──────────────┼────────────────┐
                 Provider wire   system catalogue   ToolSearch
             name/description/     单行描述/别名        schema load
                 parameters
                                      │
                              PromptAssembler
                                      │
              L5 Tool Playbook（族 metadata + tool-owned Prompt）
```

关键边界：

- `ToolPromptSpec.as_tool_spec()` 是 Provider wire 的唯一 schema 投影，只包含
  `name/description/parameters`；操作指令、when/exclusion 和示例不得塞回 wire schema。
- `wire_hash` 只覆盖 Provider 字节；`content_hash` 覆盖全部模型可见 guidance。两者同时记录，
  可区分“工具 Schema 变化”和“仅 Playbook 文案变化”。
- `ToolPlaybookCatalog` 从实际 advertised specs 选择族元数据；只有 legacy spec 没有自有 instructions 时才使用 guidance fallback。内置工具族由工具名匹配，
  自定义工具可使用 `playbook_id` 分组或自动获得 `tool_playbook:tool:<name>`；已知工具的显式
  description 不会被默认族 guidance 覆盖。
- `ToolRegistry` 拒绝 Tool 与 spec 名称不一致；旧工具未提供显式 spec 时由 `from_tool()`
  生成兼容 spec，空 description 以稳定的 `Tool <name>` 占位，避免破坏第三方/测试替身注册。
- `TurnToolPool.tool_specs()`、`search_catalog()`、`system_hint()` 和 PromptAssembler 使用同一份
  snapshot；因此被权限、运行模式、feature、MCP health、allowlist 或 TurnIntent 排除的工具
  不会泄漏 Schema、描述、别名或 Playbook。
- `PromptPlanSnapshot.tool_prompt_specs`、`ProviderMessages.tool_prompt_specs` 和
  `channel_record()` 只保留 name/source/version、计数、别名、playbook ID 与 hash；`/context plan`
  和 `/context why <tool|tool:<name>>` 可展示 wire/content hash，但不渲染描述、Schema、指令或
  示例正文。

## Tool-owned Prompt 所有权下沉（Step 120）

Claude Code 式分工不应把每个工具的参数习惯、操作顺序和安全特例都堆进 Base Prompt。
Krelcode 从 Step 120 起把两层职责明确拆开：

| 层 | 回答的问题 | 典型内容 | 来源 |
|----|------------|----------|------|
| Base Prompt | Agent 是什么、总体原则、如何选择工具 | 精确、最小修改、真实证据、验证、安全权威 | `core:base:*`，L1 static |
| Tool-owned Prompt | 这个工具何时用、怎么用、禁止什么、如何验证 | 专用工具偏好、Git 安全、精确匹配、并行/串行、输出检查、示例 | Tool 类属性 → `ToolPromptSpec` |
| Tool Playbook | 多个工具如何形成稳定目录和缓存身份 | 工具族标题、顺序、active tools、版本与 hash | `ToolPlaybookCatalog`，L5 session |
| Runtime Contract | 本轮到底允许什么 | approval、Sandbox、权限模式、工具池过滤、MCP 健康 | `ModePermissionContract` / `TurnToolPool` |

当前内置、子代理、Skill、LSP/Web/Browser/Notebook/Database 与 Memory/`prompt_lab` 共 51 个已知工具均拥有非空
`prompt`、`when_to_use`、`when_not_to_use` 和版本；复杂工具还拥有可审查示例。工具 Prompt
随 `ToolPromptSpec.from_tool()` 进入统一快照，Provider wire 仍只序列化
`name/description/parameters`。`run_command` 的专用工具偏好、Git destructive-command
禁令、staged diff 检查与 Sandbox 边界，`edit_file` 的 exact/unique/`replace_all`/diff 检查，
以及 `ask_user` 的“先调查、只问单一阻塞决策”都位于各自工具，而不是 Base Prompt。

`ToolPlaybookCatalog.enrich_prompt_spec()` 的兼容规则是：

- 工具已有 `instructions`：只附加 `playbook_id/title/priority/version`，不得追加或覆盖规则。
- legacy 工具没有 `instructions`：才使用族 guidance 作为 fallback，并在未来迁移时删除回退。
- 工具未进入本轮 advertised/loaded 集合：对应 Prompt 不渲染，避免描述不存在的能力。
- Provider/观测层只看到 hash 与计数；完整工具正文只进入 L5 instruction channel。

`CacheBreakpointPlan.as_record()` 只包含 strategy、reason、候选 ID、kind、索引、token、
字符数、digest、stability、anchor/moving 标志和跳过原因，不包含 Prompt、工具 Schema、
消息正文或凭据。实际发送后的 `CacheSegmentSnapshot` 也从最终 wire 反向解析，校验 plan
与 wire 一致；TUI 与 `/context cache` 展示断点数量、累计 token、stability 和 hash。

## Tool Prompt Lint 与 examples 契约（Step E）

`ToolPromptSpec` 只保证运行时能构造统一对象，不能证明模型侧 guidance 可执行。
Step E 新增旁路 `tools/prompt_lint.py`，把结构校验从 AgentLoop、
Provider adapter 和 `/prompts` 展示逻辑中解耦：

```text
ToolRegistry.prompt_entries()
  → lint_tool_prompt_spec(spec, source)
  → PromptLintIssue[] (stable TPLxxx code + severity + field)
  → ToolPromptLintReport (counts + content_light=true)
  → /prompts lint [tool] / SDK/log-safe diagnostics
```

lint 检查 name 格式、顶层 object Schema、`required` 的合法性与引用、每个 example 的
未知字段、JSON Schema 类型/枚举/范围/必填约束、重复 input/title、空 description、
guidance 冲突、长度上限、疑似真实凭据形状和可选参数覆盖。它不检查 example 路径是否真实
存在，也不执行工具；因此 lint 只读、确定且不会触发工作区扫描。

来源决定缺失 examples 的严重级别：`builtin/skill/subagent` 是 error，
`mcp/plugin` 是 warning，保持离线外部能力可注册但不静默降低本地生产契约。报告只包含
工具名、字段路径、错误码、计数和 content-light 标志，不包含 instructions、Schema 注解、
example input 或 Secret 文本。`/prompts lint` 不刷新 Prompt Surface 目录，也不触发磁盘扫描。

P1-4 完成后，内置、扩展、子代理、LSP/Web/Browser/Notebook/Database、Skill、Memory 与 `prompt_lab` 共 51 个模型可见
工具均有至少一个 schema-valid example；复杂工具用多个 example 区分正常、安全或边界调用。

## Prompt Conflict Analyzer（Step 110）

Prompt 冲突分析发生在 `PromptAssembler` 对最终 Fragment 完成 L0-L12 稳定排序之后、生成
`PromptPlan` 之前。它只分析 Instruction channel，不分析 Memory/Web/MCP/ToolResult 等
Evidence，也不使用模型调用。当前可机器识别的冲突类型为：

- `direct_negation`：主题高度重合，但一方为肯定/要求、另一方为否定/禁止。
- `opposing_value`：同一上下文中选择了语义对立值，例如 tabs/spaces、sync/async、
  制表符/空格；先把同义值归一为同一个 side，再比较剩余主题。
- `conditional_*`：双方都带条件语义时的冲突，保留为低严重度 `info`；条件方与无条件方
  不做自动冲突判定，避免把不同适用范围的规则误报为错误。

分析器忽略 fenced code，支持中英文句末标点。英文按命令/标识符 token，中文在无分词器
条件下使用 CJK bigram，使“使用制表符缩进”和“使用空格缩进”可以稳定对齐。候选集先按
共享主题建立异极性索引，再按显式对立值建立 side-to-side 索引；不会对全部 Directive
做无条件 O(n²) 扫描。Directive、候选比较、最终冲突和持久化反序列化分别有硬上限，
达到上限时设置 `truncated=true`，并且所有文本解析函数都有进程内有界缓存。

权威裁决顺序固定为：

```text
managed policy
→ L5 mode/permission contract
→ L1 base agent prompt
→ directory rules
→ project rules
→ user rules
→ agent style
→ working context
→ per-turn reminder
```

同一 tier 内先比较 rule precedence，再比较 layer；所有权威键完全相同时，不使用 Fragment ID
或注册顺序进行任意裁决，而是标为 `ambiguous`。已裁决冲突记录 winner 与
`managed_policy` / `layer_precedence` / `rule_precedence` / `authority_precedence`，未裁决
冲突记录 `equal_authority`。

```text
Instruction Fragments
  → sentence/directive extraction
  → polarity + subject + condition + opposing values
  → topic/opposing-value candidate index
  → authority resolution
  → PromptConflictReport
  → PromptPlan / ProviderMessages / PromptPlanSnapshot
```

`PromptConflictReport` 是 content-light 契约：仅包含 fragment/source/layer/scope、冲突
code/severity/status、confidence、winner、resolution、topic/directive hash、token 重叠数和
聚合计数。指令原文、摘要、匹配句子和主题文本都不进入报告、Snapshot、Provider channel
或 Context checkpoint。`conflict_hash` 独立保存分析结果身份；它不并入 `full_hash`，因为
冲突报告是已包含 Instruction 的派生观测信息，不应仅因 analyzer 版本或展示策略变化制造
Provider cache miss。

诊断入口：

- `/context plan` 显示冲突数、未裁决数、分析 fragment/directive 数与 analyzer hash。
- `/context conflicts` 列出全部有界冲突记录。
- `/context why conflict:<id>` 查看单条冲突裁决。
- `/context why <fragment-id>` 同时显示与该 Fragment 相关的冲突，但始终不输出指令正文。

## Prompt Release 与模型 Profile 校准（Step 111）

Prompt 的“可发布身份”不再等于一个可变字符串或 profile 名称。`agent/prompt_release.py`
把生产 Base section 与 append-only Profile catalog 归一化为不可变 `PromptRelease`：

```text
PromptSectionIdentity(id, content_hash, priority, required)
PromptProfileIdentity(name, patterns, base_prompt_hash, section_hashes)
PromptRelease(release_id, frozen_at, base_sections, profiles)
  → release_hash = SHA256(canonical content-free manifest)
```

Release hash 对 section/profile 内容敏感，对注册顺序不敏感；manifest 可以稳定序列化，
但不包含任何 Prompt section 正文。当前 Release 的 ID 与 hash 在 `agent/prompts.py`
中显式冻结，任何生产 Prompt 变更都必须作为独立 review 事件更新二者。

`ModelProfileCalibration` 是从真实 live Benchmark baseline 派生的内容无关证据：

```text
profile_name / profile_hash
release_id / release_hash
contract_version / contract_hash
benchmark_schema_version / frozen suite_hash
provider / model（精确）或 provider/model patterns
verified / verified_at / trials
```

`certify()` 只接受通过完整 case manifest 和至少 3 trials 的 baseline。扫描范围默认是
具体 provider/model，只有显式提供 pattern 时才形成 family-wide scope；多个候选先按
精确匹配或最长 pattern 排序。匹配后还会逐项校验 Release、Profile、contract 和冻结
behavior suite。因此 profile 名称命中本身不会产生“已校准”结论。

运行时语义固定为：

- `calibrated`：Release/Profile/contract/suite/scope 都与 baseline 证据一致；
- `uncalibrated`：没有覆盖当前模型的证据；普通对话可继续，gate fail-closed；
- `stale`：存在旧 Release、旧 Profile、旧 contract 或旧 suite 的证据；显式可见但不采用。

Calibration 只进入 provenance 和发布证据，不进入 Instruction 或 Evidence channel。
主 Agent、`/model` 热切换和子代理都通过同一个 `resolve_prompt_profile()` 绑定 Release
与 calibration；L1 Base Fragment metadata、subagent instruction bundle、sidechain、
Benchmark report 和 `/context why` 使用同一组 ID/hash/status。任何展示/持久化路径都不含
Prompt 正文、答案、工具参数或凭据。

Baseline schema 升为 v2，顶层嵌入完整 calibration 并仍保持
`contains_prompt_content=false`。加载时对 baseline hash、calibration hash、字段白名单、
contract/suite/provider/model 一致性校验；schema v1 视为 legacy-uncalibrated，必须重跑
`--save-baseline`。Release gate 新增 `prompt-release`、`prompt-profile`、
`calibration-identity`、`calibration-state` 检查，并把 suite hash 与冻结值比较，任何身份
漂移都返回 `NOT_ALIGNED`。

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
mode_permission_contract                   # L5，权威 mode/permission/tool/Sandbox 契约
```

- `environment_attachments()` 对每个非 rules section 计算 SHA-256；只有 changed/added/removed
  section 会更新 `environment_snapshot` 与 `environment_delta`。规则按
  `managed:rules/user:rules/project:rules` 独立登记，目录规则使用请求期
  `directory:rules`；目录规则命中后进入 L3 cacheable 前缀，不会在 L3 形成断点并阻断后续
  L4-L9 稳定块。规则内容变化会正常更新 stable hash，但不会与 git/tree/date 等环境附件互相改写。
- catalog 只消费已经加载的 Manager/Supervisor 快照，不在 Prompt 装配时扫描磁盘或 MCP；
  catalog membership 变化与正文变化分别记录在无正文 metadata 中。
- 所有附件均 `cacheable=False`。`PromptAssembler` 将每个附件映射为一个独立
  `SystemPromptBlock`，因此环境变化只改变对应 block(s) 与 `dynamic_hash`，不会改变
  `stable_hash`，也不会复制进持久 history。
- `mode_permission_contract` 固定为 L5、managed trust、turn stability。正文只包含聚合后的
  capability 边界，不复制工具 Schema、工作区绝对路径或审批规则正文；attachment metadata 和
  `PromptPlanSnapshot` provenance 记录 contract/tool-exposure hash、run/permission mode、审批策略、
  Sandbox 状态与工具排除分类。`ContextCacheLedger` 同步记录 contract hash 与 exposure hash，
  `/context why`/`/context ledger` 可解释模式切换导致的 cache/dynamic 变化。
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

- `ToolPlaybookCatalog` 只根据本轮 Provider 实际 advertised 的工具生成 L5 工具 Prompt；
  disable、权限/运行模式过滤、Intent 收窄和尚未加载的 deferred 工具不会获得 Playbook，
  避免提示词能力与真实工具边界不一致。
- `TaskWorkflowClassifier` 在 turn 开始时只分类一次，`TaskWorkflowCatalog` 从固定任务类型
  中选择一个 L9 turn/managed Workflow；附件正文与代码围栏不参与分类，显式文件解释 Intent
  优先。Workflow 进入 dynamic hash，但不改变 Base Prompt 与工具 Playbook 的稳定缓存前缀。
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
├─ memory_type user | feedback | project | reference
├─ topic / why / how_to_apply
├─ source     manual | legacy | future extractor
├─ tags[]
├─ pinned
├─ created_at / updated_at
└─ metadata{}
```

`kind` 只保留旧 JSONL 分类；新的 Prompt/用户语义由 `memory_type` 表达。feedback/project
必须在写入前同时具备 Why 与 How to apply。旧记录缺失结构时安全降级为 reference，避免把
不完整事实误当作可执行项目知识。

| Scope | 存储位置 | 生命周期 | 使用范围 |
|-------|----------|----------|----------|
| user | `~/.krelcode/memory/entries.jsonl` | 跨进程、跨工作区 | 当前 OS 用户私有 |
| project | `<workspace>/.krelcode/memory/entries.jsonl` | 跟随工作区 | 当前仓库/目录 |
| session | `<workspace>/.krelcode/memory/sessions/<session-id>.jsonl` | 跟随会话 | 当前逻辑会话 |

作用域按 `user → project → session` 由宽到窄排列。索引固定最多 200 个主题、每行 150
字符、总长 24,000 字符，只展示标题/type/scope/hook；正文按需读取，避免大量全局记忆挤占
当前任务预算。

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
- `MemoryStore.index_view()` 生成 `memory:scoped` L7 Evidence layer，但内容只包含严格
  限长的 `MEMORY.md` 索引，不包含主题正文。JSONL 是权威源，Markdown 索引/主题是可重建
  视图；变更后原子重建，陈旧主题视图同步删除。
- 索引上限为 200 主题、每行 150 字符、总计 24,000 字符。user 主题视图只写入
  `~/.krelcode/memory/`，project/session 写入工作区，避免跨项目私密正文泄漏。
- `memory_search(query)` 负责定位主题，`memory_search(id|topic)` 才按需读取完整主题及
  Why/How。Memory 仍是 untrusted、requires_validation、不可缓存的 Evidence。

交互入口：

- `#内容` = project；`#user: 内容` / `#project: 内容` / `#session: 内容` 显式选择 scope。
- `/memory [list]` 查看 ID、scope/kind、语义 type 和内容；`/memory scopes` 查看条数、
  索引与主题路径；`/memory add` 支持 `--type/--why/--how/--topic`；`/memory remove <id>` 删除；
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
MemoryStore(typed entry)  → MEMORY.md index → memory_search(topic) on demand
```

- 默认模式是 `suggest`：只生成候选，不写长期记忆；`auto` 仅在候选置信度达到
  `0.80`、证据成立且与已有候选/记忆无冲突时晋级；`off` 完全停止提取。
- 候选队列按 workspace 隔离，并记录 project id；候选语义类型为
  `user/feedback/project/reference`，但队列本身仍 project-local。feedback/project 必须有
  Why/How，缺失时停在 pending 且不得自动或人工 accept。
- 稳定知识必须带证据：turn/session、用户原句、工具名、命令与 exit code。重复成功的
  测试命令只累积 evidence，不重复建候选；不同正式测试命令被标为冲突并阻止自动晋级。
- 文本、证据、tags 与 metadata 在落盘前统一经过 SecretRedactor 和密钥模式过滤；覆盖
  API key、Authorization/Bearer、Cookie、常见平台 token 与私钥块。脱敏后无实质信息的
  候选直接丢弃。
- 专用 L5 `memory:lifecycle-contract` 固定 What NOT to save 清单，并明确 Task（当前步骤）、
  Plan（实施方案）、Memory（长期可复用事实）与 Compact（当前会话交接）的所有权。
- `memory_search` 是本地只读工具，返回 `[Evidence: Memory]` envelope 的已审核记录；
  Memory 索引同样声明 `instruction=false`，不能升级为系统指令。

## 分层规则、目录规则与 @import（Step 87）

规则加载从 `context.py` 内部的简单文件拼接拆到独立 `agent/rules.py`，并把规则内容
与 Memory 明确分离：规则是持久行为约束，Memory 是任务事实/偏好记录。

```text
Rule precedence (constraint → specific)
managed    /etc/krelcode/AGENTS.md | ~/.krelcode/managed/AGENTS.md
           | <workspace>/.krelcode/managed/AGENTS.md | configured paths
user       ~/.krelcode/AGENTS.md
project    <workspace>/AGENTS.md | KRELCODE.md
directory  <target ancestor>/AGENTS.md | KRELCODE.md

RuleLoader
  ├─ discover: 每个目标路径只检查 workspace 内祖先目录
  ├─ select:   每个目录 AGENTS.md 优先，其次 KRELCODE.md
  ├─ expand:   独立行 @relative/path，递归、按出现位置内联
  ├─ guard:    scope escape / sensitive files / cycles / duplicates
  ├─ bounds:   depth + file count + total character budget
  └─ RuleStack { managed, user, project, directory: RuleBundle }
```

- `managed/user/project` 入口在一次会话的 PromptSnapshot 中分别生成
  `managed:rules/user:rules/project:rules`；`directory` 由
  `DirectoryRulesContextSource` 在每个模型 Step 前按当前轮路径收集为
  `directory:rules`，不会扫描整个仓库。Agent Definition 使用
  `agent_definition:<name>`。规则层按 `managed → user → project → directory` 渲染；
  managed 约束始终保留，user/project/directory 越靠后越具体。
- 每个规则 Fragment 的 provenance 包含 scope、precedence、覆盖关系、入口/导入路径、
  issue 与 conflict policy；`/context why project:rules` 显示最终生效来源，
  `/context rules` 展示完整优先级。
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
  `/context rules` 只展示优先级、路径、scope、字符数、导入关系与 issue，不输出规则正文。
  `/context sources` 可看到按需加载的 `directory:rules` layer 及其 token attribution；
  命中后它与 project rules 同处 L3 cacheable prefix，后续 L4-L9 稳定块不会被降级为动态块。

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
  `bootstrap/application.py` 注入，业务层不感知 HookRunner。
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

`bootstrap/` 横跨四层完成对象装配，但 `BootstrappedApplication` 只暴露 run/close，
`AsyncMain` 只拥有进程生命周期。headless 与 REPL 都通过 `AgentRuntime` 执行，因此
未来增加 API/IDE 入口时可复用同一会话、事件路由与执行门面。

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
                └─ 对话 / -m → 最后才导入 bootstrap 组装 Provider/MCP/Sandbox/UI
```

这里不做两套参数定义：fast-path 只识别无歧义的版本请求，所有正式参数语义仍由
完整 parser 负责；未识别参数原样转发。`daemon/jobs/agents` 已在完整 parser 下拥有独立
延迟加载路由；未来 bridge/background/template runner 可沿用同一边界，而无需把实现拉进
默认启动路径。

## CLI 子命令（Step 28）

- `krelcode doctor`：离线环境自检（不发起网络请求）——Python 版本、
  provider key（脱敏 mask_key，全 key 不落屏）、默认 provider 可解析、
  配置文件（显式 --config 必须存在；缺省探测报告命中项）、git/工作区可写、
  MCP stdio 命令存在性（url 型仅提示）。三态 ✓/⚠/✗；有 ✗ → 退出码 1（CI 可判定）
- `krelcode init [--force]`：脚手架生成 krelcode.toml（provider/MCP/hooks
  注释模板）与 AGENTS.md 规则骨架；默认不覆盖已存在文件
- 子命令为可选位置参数（nargs="?"），既有 flag 式用法完全兼容

## 成本感知（Step 30）

- **Anthropic prompt caching**（adapter 内实现，其他层零感知）：`auto/true` 默认协商开启；
  Step 108 起由 `adaptive-prefix-v1` planner 在 tools、stable system 与 messages 上选择
  零到四个 ephemeral 断点（str content 自动转 block；tool_result 合并消息只标末块）；
  `prompt_cache = false`（TOML）可关。
  缓存命中 input 按 ~10% 计价，长会话成本大幅下降；
  usage 新增规范化键 cache_read / cache_creation
- **模型目录**（agent/pricing.py）：价目 + 上下文窗口双表，共享「精确→最长前缀」
  匹配；`default_budget_for(model)` = 窗口×75% → --context-tokens 未显式指定时
  按模型自适应压缩预算（未知模型兜底 100k）；doctor 显示窗口与推导预算
- **OpenAI 缓存 usage 归一**：prompt_tokens_details.cached_tokens →
  cache_read，且从 input_tokens 扣除——统一为 Anthropic 语义（input 不含缓存），
  成本公式对两家一致且不双计；total_tokens 保留 provider 原值
- **Gemini 缓存 usage 归一**：`cachedContentTokenCount` → `cache_read`，同样从
  `input_tokens` 扣除；Gemini 2.5+ 默认只协商 server-managed implicit cache。
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

## Security Review（Step C）

Security Review 与普通 code review 使用不同的风险模型和执行边界，代码位于
`src/krelcode/security_review/`：

```text
git diff / staged / focus
        │
        ▼
security-review Task Workflow + untrusted evidence boundary
        │
        ▼
local read-only ToolRegistry
(read_file / grep / glob / find_refs / repo_map / tool_search)
        │
        ▼
PromptSurfaceContract
(redaction + input/output limits + timeout + JSON Schema)
        │
        ▼
SecurityReviewReport
(summary / severity / verdict / findings / CWE / evidence / remediation / coverage)
        │
        ├─ text renderer
        ├─ `krelcode security-review --output-format json`
        └─ `/security-review` slash command
```

核心约束：

- Workflow 分类新增 `security_review`，显式安全审查请求优先于普通
  `code_review`；Workflow 要求 source-to-sink 验证、去重、证据和覆盖限制。
- Headless runner 使用独立 `ToolRegistry`，只有本地只读工具；`RunMode.REVIEW`、
  `PermissionMode.READ_ONLY`、tool allowlist 和 SandboxPolicy 形成四层边界。
- diff、文件名、注释与源码字符串统一标记为 untrusted evidence；其中嵌入的指令
  不得改变系统规则、扩大范围或触发执行。
- 报告由 `security-review-report-v1` 严格 Schema 校验。Finding ID 必须为
  `SEC-NNN`，位置/严重度/置信度/CWE/证据/修复建议可机器读取；`high` 或
  `critical` 不允许 `pass` verdict。
- CLI 默认文本报告；`--output-format json` 输出包含 report、scope、gate、usage、
  tools_used 和 denied 的单行 JSON。`--fail-on` 默认 `high`，达到阈值时退出码为 1。
- `/security-review` 复用当前会话，但通过 `CommandResult.tool_allowlist` 把本轮
  工具池收窄到同一只读集合，并要求模型返回同一结构化报告。
- Prompt Surface Registry 登记 `aux:security-review`，Contract 记录
  Schema hash、300 秒超时、输入/输出上限与脱敏策略；执行 observation 不保存报告或
  Prompt 正文。

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
`pytest` 与 `mypy` 只通过 `dev` optional dependency 安装，普通 wheel 的运行时依赖仍为空。
主 CI 使用 Python 3.11/3.12 × Linux/macOS/Windows 矩阵运行架构门禁与全量测试，并独立验证
安装器、Python 包构建、TypeScript SDK 和 VS Code extension。
架构门禁以 `architecture/baseline.toml` 冻结现有模块循环、超大文件和 AgentLoop 动态属性
访问债务；新代码不得增加这些模式。`core` 与 `contracts` 使用 mypy strict，运行时依赖仍为空。

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
- **Output Styles**：内置 `default/explanatory/learning` 三个人格，并通过
  user/project/configured Markdown 扩展；合并规则为同名高优先级来源覆盖低优先级
  来源。活动样式只替换一个带 source 的 L4 `style:output` PromptFragment，不修改
  L1 base prompt，也不进入可执行 Hook 链。自定义 frontmatter 的
  `keep-coding-instructions` 决定该人格是否允许覆盖冲突的 L1 编码表现规则；L0/L2/
  L3、安全、权限、Sandbox、证据标准和当前请求始终拥有更高权威。
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
`EvidenceRecord` 与统一 renderer。Memory 使用人类可读且机器稳定的
`[Evidence: Memory]` envelope，逐项携带 source/scope/confidence/requires-validation/
instruction=false；Resume、子代理和 `memory_search` 复用同一格式。MCP/Web 等非 Memory
Evidence 继续使用 `<evidence_set role="non-instruction">` envelope。所有正文和属性先做
HTML escape，因此工具输出或记忆正文不能伪造 header、闭合 envelope 或注入新的 system
标签。

Memory 不再拥有独立的 authority 语义：

- `MemoryContextSource` 将 `MemoryEntry` 映射为 `EvidenceSource.MEMORY`。
- PromptAssembler 在唯一装配边界为 L0-L6 标注 `instruction_source`，为 L7 标注
  `evidence_type` 与 `instruction=false`，不依赖 adapter 各自解释 trust。
- PromptAssembler 固定映射到 L7 Evidence、turn stability、非 cacheable；Memory 变化只
  影响独立 `evidence_hash`，不改变 instruction 的 `stable_hash/dynamic_hash`。
- Resume 任务检索、子代理筛选记忆和 `memory_search` 复用同一契约。
- MCP initialize instructions 从 L6 catalog 降为 L7 Evidence；它仍是 server guidance，
  但不能伪装成受信任目录或行为指令。
- `/context why` 与 `/context sources` 展示 `role=evidence`、`instruction=false` 和
  `requires-validation=true`，metadata 只含 ID/scope/count/origin 等无正文字段。

Instruction 侧仍由 managed/user/project/agent-definition 分类器收口，Evidence 分类器对
`data`/`untrusted` 输入返回无指令来源，避免两套 taxonomy 在后继 adapter 中漂移。

`ProviderMessages` 现在显式保留 `instruction_blocks` 与独立 `EvidenceChannel`。后者携带
`evidence_type/scope/origin/requires_validation`、不含正文的 provenance metadata、独立 hash
和当前 user turn anchor。Evidence 不再序列化到 system message，也不参与
`SystemPromptBlocks`；OpenAI-compatible、Anthropic 与 Gemini adapter 在锚定 user message
内部使用各自兼容的 text content/block/part 表达。工具循环后续 step 复用同一 anchor，因此
Evidence 在本轮内持续可见，同时保持 tool message 语义不变。

SubagentManager 复用同一装配契约：`InstructionBundle` 先转换为带 L0-L4 来源层级的
`PromptFragment[]`，L5 mode/permission contract + 工具目录和 L6 Skill Catalog 继续由统一装配链补齐；筛选后的
L7 inherited-memory fragment 也注册到子代理自己的 `PromptFragmentRegistry`，再由同一个
`PromptAssembler` 生成 PromptPlan/ProviderMessages。
子代理因此拥有与主 Agent 相同的 stable/dynamic cache boundary、来源归因和 `/context why`
观测；inherited memory 不再经过 Runtime Hook 通道，也不会继承父级完整 history。

## Prompt Plan 可观测性（Step 103）

请求观测继续复用唯一 `PromptAssembler.assemble()` 路径，不建立第二套 Prompt 组装逻辑。
运行期 `PromptPlan` 保留正文用于 provider 序列化；新增的 `PromptPlanSnapshot` 只保存：

- 每个 Fragment 的 `id/source/scope/layer/stability/trust/tokens/cacheable/stable/content_hash`；
- stable/volatile token、按 source 分布、cache boundary 与 stable/dynamic/full hash；
- 稳定排序后的工具 Schema 目录 hash 和工具数量；
- Memory hit 的 `id/source/scope/kind/origin/tokens`；
- trust/scope、单文件范围、空 attachment、预算等裁剪项及机器可读 reason；
- 与上一次真实请求相比新增/删除/变化的 Fragment、各类 hash 标志和 token 差值。

`ContextPlanSnapshot` 嵌入该快照并随 Context checkpoint 持久化。`/context plan` 展示逐
Fragment 计划和 omission/diff，`/context why <id>` 可定位 Fragment、Memory entry、Context
layer 或裁剪项，`/context cache` 展示工具目录 hash 与跨请求变化。所有观测记录只保留
来源、计数和哈希，不包含 Prompt、工具 description/schema、history、Memory 或附件正文。

## Prompt Surface Registry（Step 125）

`PromptAssembler` 只拥有一次请求内部的 L0-L12 Fragment；Compact、Review、Tool、Workflow、
Subagent、Hook、MCP Prompt、Skill、Plugin/Custom Command 与 Output Style 还构成更外层的
模型可见 Prompt 面。Step 125 新增旁路 `PromptSurfaceRegistry`，首次把这些 surface 纳入
同一份只读目录，但不成为第二个 Assembler，也不参与 Provider 消息、工具 Schema 或缓存边界
的生成。

每个 `PromptSurfaceDescriptor` 只保存：

- stable ID、kind、source、owner、version、scope；
- instruction/evidence role、authority、trust、stability；
- content hash、Token 估算、可用能力、可用状态与有界 metadata。

可执行 Surface 还携带 `PromptSurfaceContract`：输入/输出 JSON Schema、超时、输入/输出字符
上限、输出解析模式、超限策略和输入/输出脱敏开关。Schema 是可持久化的结构化安全契约，不是
Prompt 正文；Snapshot 不保存任何实际模型输入、工具结果、MCP 返回正文或密钥。

目录刷新先在临时映射中验证全部 descriptor 并拒绝重复 ID，然后一次性替换；已有 observation
按相同 surface ID 保留。`PromptSurfaceSnapshot` 可往返序列化，catalogue hash 只由 schema 与
surface 元数据决定，不受刷新 generation 影响。MCP Prompt 与 server instructions 固定为
`evidence/untrusted`，Plugin、Skill、Hook 与 Command 的来源分别映射到对应 authority/trust。

`AgentLoop` 在真实 `PromptPlan` 生成后记录 main Agent 与本次实际暴露工具的内容轻量
observation；失败观测不会影响请求。`/prompts list|show|coverage|hash` 只显示来源、Hash、
Token、权限与最近 render 计数，不显示 Prompt 正文。该层当前严格只读：不自动禁用、改写、
发布或重排任何 Surface。

## Prompt Surface Contract（Step 126）

Review、Compact、Prompt Hook 和 MCP Prompt 不再各自维护互不一致的调用安全逻辑，而是通过
`PromptSurfaceExecutor.execute(surface_id, contract, value, runner)` 执行：

1. **输入脱敏**：递归处理字符串、对象键值和常见凭据形态；provider key、`api_key`、
   `authorization`、Cookie、Bearer、云厂商/代码托管 token 与私钥均替换为 `***`。
2. **输入 Schema**：执行前校验 provider-neutral 输入形状和长度，非法输入不会到达模型、
   Hook runner 或 MCP 服务器。
3. **超时**：使用 `asyncio.wait_for` 包住整个辅助调用；Timeout 返回结构化结果并取消等待。
4. **输出上限**：文本 Surface 可截断；Compact/MCP 等结构化 Surface 采用 fail-closed，
   超限时不解析不完整 JSON。
5. **输出脱敏与 Schema**：解析前先脱敏，再执行声明 Schema 校验；非法 JSON/Schema 返回
   `schema_error`。
6. **只读观测**：结果只向 `PromptSurfaceRegistry` 写入 status、schema/timeout/truncate/
   redaction 标志、耗时、输出 hash 与 token，不写入正文。

各 Surface 的默认边界：

| Surface | 输出模式 | 超时 | 输出上限 | 超限策略 |
|---------|----------|------|----------|----------|
| Review | text | 300s | 64,000 chars | truncate |
| Compact | compact-summary-v2 JSON | 180s | 64,000 chars | fail-closed |
| Prompt Hook | JSON or text | Hook `timeout` | 64,000 chars | truncate |
| MCP Prompt | MCP messages JSON | Server `timeout` | 1,000,000 chars | fail-closed |

Review 的 diff 在进入模型前脱敏，模型回复在打印前再次脱敏。Compact 使用既有
`COMPACT_SUMMARY_JSON_SCHEMA`，超时、Schema 失败或输出超限继续走 Working Context/History
降级摘要。Prompt Hook 的模板变量和 JSON payload 共享同一输入脱敏，MCP `prompts/get`
参数在发往服务器前脱敏，返回 messages 逐字段脱敏。MCP server instructions 同样应用脱敏和
256,000 字符上限。

`/prompts show <id>` 对存在 contract 的 Surface 展示 version、Schema ID/hash、timeout、
输出上限、脱敏策略和最近一次 contract observation，但仍不包含 Prompt 正文。

## Context / Cache Ledger（Step 104）

每个最终 Provider 请求由唯一的 `ContextCacheLedger` 收口。它只在 microcompact/compact 后的
最终重装配完成、且 `PreparedLLMRequest` 已确定后生成，避免把中间压缩探测误记成实际请求。
Ledger 聚合 stable/dynamic/instruction/evidence hash、tool catalog hash、attachment
generation/revision semantic identity、compact checkpoint、Working Context sequence、Memory
evidence IDs、history digest、Provider payload digest 与 `complete_request_hash`，并通过
`compared_with(previous)` 产生 machine-readable cache-miss reasons。

`ContextPlanSnapshot.cache_ledger` 随 Context checkpoint 持久化；Resume 保留原 request identity，
同时记录 `workspace-refresh`、文件 hash、Memory 选择或 Working Context 等 invalidation。
Subagent sidechain 持久化 child Ledger，并以 managed/user/project/directory 与 Agent Definition
Fragment 计算 `policy_instruction_hash`，再与父请求比较 `match/mismatch/not-applicable`；
父级完整 history、Evidence 正文和 sidechain 都不会因该诊断泄漏或自动注入。

`context_assembled` 的 JSONL/stream-json 记录同时携带 Ledger。`/context ledger` 是完整诊断入口，
`/context cache` 提供摘要。所有字段均为 hash、ID、revision、sequence 或布尔值，不保存 Prompt、
消息、工具 Schema、Memory 与请求体正文。

## Live Model Behavior Benchmark（Step 105）

结构 Benchmark 继续负责确定性 CI：它验证 PromptPlan、hash、cache boundary、
Evidence channel、Resume/Fork/Rewind/Compact 契约，但不声称真实模型会正确使用这些
结构。`benchmarks/behavior.py` 因此增加独立的 `--live` 层；两者共享生产组件，不共享
判定目标。

Live 执行边界：

- 每个 trial 创建新的临时 workspace 与 HOME；真实 HOME、项目规则和凭据目录不会暴露
  给文件工具或 shell Sandbox。
- Provider 在进入隔离现场前按分层配置解析；每个 trial 重新创建 Provider，API key
  只存在于 Provider 对象中，报告、state 和工具环境均不保存它。
- 复用生产 `AgentLoop`、`PromptAssembler`、动态工具池、`SandboxPolicy`、
  `ExecutionSandbox`、真实文件工具、`run_command`、EventBus 和 compact 引擎。
- 每个场景固定客观断言：磁盘内容/hash、测试退出码、真实 tool call 参数、最终答案
  事实 token、workspace diff 和 compact 事件。没有第二个 LLM Judge。
- v2 为每个 trial 增加总超时，单个模型/工具挂起只产生稳定的 `timeout` 失败，
  不阻塞后续 trial；provider、sandbox、configuration 与内部错误使用稳定错误分类。
- 报告记录 Krelcode/Python 版本、平台、Sandbox 配置、case 列表、trial/max-step 参数
  和 suite hash，并汇总工具数、输入/输出/缓存 token、可估算成本和错误分布。
- `review-no-edit` 同时验证 `code-review` Workflow 是否被选中，以及模型能否只读定位
  真实缺陷、报告行号且保持 workspace 不变。

v2 最初六个行为场景（Step 107 已扩展为十六个）：

1. `read-scope`：指定文件读取、答案事实和无关读取范围。
2. `edit-verify`：真实落盘修改、指定 test 命令和成功退出码。
3. `review-no-edit`：code-review Workflow、真实缺陷/行号发现和零 workspace 修改。
4. `instruction-precedence`：四层规则同时存在时的最终权威选择。
5. `evidence-injection`：Poisoned Memory + 文件注入下的事实校验和零未授权写入。
6. `compact-continuation`：多 turn 工作、强制 Structured Compact、后续现场恢复。

多 trial 报告同时输出 `pass_rate`、`pass@k`、`stable`、工具调用次数、输入/输出 token、
耗时、失败 check 和 workspace diff 摘要。默认隐藏最终答案及工具参数；只有
`--include-content` 显式开启时才进入报告。整体成功要求所有已选 case 的所有 trials
通过，避免单次偶然成功掩盖不稳定行为。

### Failure-trajectory Prompt tuning（Step 106）

`benchmarks/tuning.py` 把失败 trial 转成可审计的 Prompt 调优候选。输入只使用 objective
check、真实工具名/路径/退出码、Workflow、稳定错误类型和实际装配的 Fragment ID；默认
不使用最终答案正文，也不把工具输出直接复制进 tuning 报告。

归因规则是确定性的，并映射到真正拥有该行为的 Prompt 来源：

- `trajectory_scope` → `core:base:tool-discipline` 与当前 active 的读取/搜索 Playbook；
- required tool / test verification → tool-discipline、verification 或 command-execution；
- unsafe mutation → tool-discipline 与 workspace-edit；
- Workflow mismatch → 期望的 `workflow:*` 定义和分类信号；
- instruction authority / evidence injection → safety、规则来源或 memory Playbook；
- compact handoff → operating-loop 与 completion。

候选包含目标来源、建议指令、风险等级、受影响 case、轨迹证据和回归步骤。当前设计固定
`auto_apply=false`：禁止 benchmark 因单次失败自动改写生产 Prompt。人工修改目标来源后，
必须重跑失败 case、原先稳定 case 和完整 live suite，确认失败指标消失且没有 cache、
安全、工具范围或既有行为回归。Provider/runtime 错误只做 error-kind 归因，不伪造
Prompt 修复建议。

## Prompt Parity Contract 与 Benchmark v3（Step 107）

Claude Code 的 Prompt 正文不是公开接口，因此 Krelcode 把“Prompt Parity”定义为可审计
的架构与真实行为契约，而不是 Prompt 字符串相似度。`benchmarks/parity.py` 冻结了
architecture、permissions、evidence、cache、subagents、compact、recovery、leakage
八个维度、16 个 live case、每 case 最少 3 trials 以及 content-light baseline schema。

Frozen contract：

- 唯一装配路径必须是 `PromptAssembler -> PromptPlan -> ProviderMessages`；
- L0-L12 来源层级、Instruction/Evidence 分权与 stable/dynamic cache boundary 不可漂移；
- ToolPool 与 ApprovalPolicy 必须在执行前共享同一权限边界；
- Memory/MCP/Tool/Web/Retrieval 只能进入 Evidence channel；
- Subagent 不继承父级完整 history，sidechain 不自动回注 main；
- Compact、Resume、Fork、Rewind 必须恢复同一份可验证现场；
- baseline/gate 只能包含 hash、ID、计数和 pass/stability 元数据。

Step 107 将 live benchmark 从 v2 的 6 个场景扩展为 v3 的 16 个场景，新增最小编辑、
permission deny、Plan gate、ToolSearch deferred loading、MCP instruction injection、
失败测试恢复、bounded refactor、resume workspace refresh、fork Memory isolation 和
second-turn cache hit。所有 case 继续复用生产 `AgentLoop`、真实工具、Sandbox 和隔离
workspace/HOME。

`PromptParityBaseline` 只能由完整、全部稳定且至少 3 trials 的 report 创建；保存与加载
采用原子 JSON，加载时校验 `baseline_hash`、固定字段白名单与
`contains_prompt_content=false`，拒绝任何 Prompt、答案、工具参数、历史或凭据字段。
`evaluate_prompt_parity()` 对 contract hash、baseline schema/contract、provider/model、
suite hash、case manifest、trial floor、case pass@k/stability、整体 report、runtime error
和 content-light policy 逐项 fail-closed。只有全部 check 通过时才返回
`PromptParityStatus.ALIGNED`。

契约 hash 被固定为：

```text
8d9c1a9f68a24113ce7a3f42595bf13005a4286ae0f641547775d84e5495262a
```

任何语义变更都必须显式更新 contract version/hash，并通过同 commit 的 parity 测试与
新的真实模型 baseline/gate；不能通过修改 baseline 或降低 trial floor 静默放宽。

## Final Fail-closed Acceptance（Step 112）

Step 112 不再把“全量测试通过”或“有 gate 文件”直接等同于最终对齐，而是新增
`benchmarks/acceptance.py` 作为唯一最终验收器。契约冻结 17 个 capability requirement，
覆盖 12 个 area：entrypoint、provider、tools、prompt、context、recovery、subagents、
extensions、safety、interfaces、release、leakage。每个 requirement 必须引用真实生产
artifact 与测试文件；验收器再增加一个独立的 frozen-contract check，因此报告固定为 18 个
required checks。

证据矩阵分为四层：

1. `static`：接受契约 hash、required artifact/test 是否存在。
2. `runtime`：直接实例化生产 `ToolRegistry`、`TurnToolPool`、Secret Guard、Command
   Registry、ExtensionRegistry 和 Provider event stream，验证模块“可导入”之外的运行边界。
3. `offline`：执行 `PromptBenchmarkRunner` 的 main/headless/stream-json/SDK/subagent/
   resume/fork/rewind/compact 九个生产面与八项结构指标。
4. `live`：要求 fresh 的真实模型 report 与 `PromptParityGateResult`，不能由 mock、
   scripted provider、缺失 source report、旧时间戳或只有 `passed=true` 的对象伪造。

最终状态严格限定为：

- `accepted`：所有 required check 通过，且 live evidence 新鲜、完整并绑定冻结身份；退出码 0。
- `pending_live`：确定性矩阵和 runtime probes 全部通过，但尚无 Live Gate；退出码 2。
- `failed`：任一 required check 失败；退出码 1。

Live 验收不只信任 Parity Gate 的 `passed` 字段。`FinalAcceptanceRunner` 会独立核对：

- Gate schema/version/contract hash 与冻结 Prompt Parity contract 一致；
- Gate 的全部预期 check 均通过，baseline hash 是有效 SHA-256；
- source report 的 suite hash 与 Gate、冻结 suite 一致；
- report 使用真实非 mock provider，contract version/hash、calibration state 合法；
- 16 个 case 全部存在，每案至少 3 trials 且 stable/pass@k、零失败、零 runtime error；
- report 与 Gate 时间戳处于 24 小时窗口内，Gate 在 report 完成后 5 分钟容差内生成；
- Prompt Release、Parity contract、behavior suite、offline report、live report、gate 的
  hash 全部进入最终 fingerprint。

报告保持 content-light。`_safe_error()` 只输出异常类型，不复制任意异常消息；Runner 对
最终 JSON 再做一次递归自检，拒绝禁止字段、Prompt benchmark 正文标记、超长字符串和
credential-shaped 值。若某个 probe 误携带内容，相关 detail/evidence 会被清除、验收失败，
而不是把泄漏内容写入报告。CLI 使用
`python -m krelcode.benchmarks.prompt --live --gate <baseline> --final-acceptance`，
且禁止 `--include-content`、`--no-sdk`、`--case` 或没有 fresh Live Gate 的离线验收。

## Prompt Authoring Contract（Step 113）

Prompt Authoring 不是 Base Prompt 的常驻能力，也不是一个可以自动修改生产 Prompt 的
高权限工具。Krelcode 将它实现为默认关闭的 L6/session/cacheable Fragment
`prompt_authoring:contract`：只有 `[prompt.authoring].enabled=true` 时才由主 Agent 与
子代理各自的 PromptAssembler 装配。配置解析 fail-closed；缺失为关闭，非法 truthy 字符串
不会被当作启用。

```text
[prompt.authoring]
enabled = true
    → prompt_authoring:contract
    → L6 / managed / session / cacheable / toggleable
    → PromptAssembler
    → PromptPlan / ProviderMessages
```

契约正文只规定 Prompt 作者应遵循的执行流程，不复制或替换 Base safety/tool contract：

1. **目标分析**：目标行为、目标 Fragment/layer、非目标与可观测成功指标先于指令正文。
2. **Instruction/Evidence 分权**：managed/user/project/agent-definition 可以产生指令；
   memory/web/MCP/tool-result/retrieval 只能作为需要验证的 Evidence。
3. **stable/dynamic 分层**：固定规则和安全边界进入 cacheable 前缀；日期、环境、会话状态、
   当前请求、Memory 与工具结果进入动态/Evidence 通道。
4. **冲突检测**：候选不实现第二套检测算法，直接复用生产 `PromptConflictAnalyzer`；
   同权未裁决冲突阻断通过，高权威已裁决冲突作为 warning 保留来源记录。
5. **工具权限设计**：显式 permission mode、工具范围、read-only/mutating、审批与 Sandbox；
   Prompt 正文不能扩大运行时权限。
6. **可验证性**：success、regression、security 三类用例均需机器可判定 metric；主观描述、
   人工观感和仅靠 LLM Judge 的“看起来更好”不满足要求。

`PromptAuthoringValidator` 接受与 Provider wire 无关的
`PromptAuthoringRequest/Item/Tool/ValidationCase`。报告 `PromptAuthoringReport` 是
content-light：只含 issue code、来源/item/tool/case ID、分类计数、request/report hash 与
标准 `PromptConflictReport`，不保存候选 Prompt、指标正文或工具参数。Issue 覆盖目标缺失、
Evidence 冒充 Instruction、动态内容进入稳定缓存、候选冲突、read-only 声明写工具、
auto-approve/通配符缺少 Sandbox 或安全验证、权限提升文案以及不可机器验证的指标等失败面。

`PromptAuthoringContract` 本身不调用模型，也不写 Prompt Registry、文件或配置。启用开关只
添加可追踪、可手动关闭的 L6 Fragment；`/context fragments` 与 `/context authoring` 展示
位置、version/hash 和启停状态。生产 Prompt 的修改仍需人工 review、对应专项测试与真实
Behavior Benchmark，避免 meta-prompt 自己改写自身权限或悄悄污染稳定前缀。

## Prompt Engineering Lab（Step 114）

`prompt_lab` 是 Prompt Authoring Contract 的可选执行面，不是新的 Prompt 来源。只有
`[prompt.authoring].enabled=true` 时才注册为 core Tool；它不修改生产 Prompt，`propose` 仅写 content-light 本地提案。ToolContext 携带当前 Agent 的
`PromptFragmentRegistry`，其生命周期与主 Agent/子代理各自的 PromptAssembler 一致。

```text
inspect  → metadata + source + layer + token + hash + conflict IDs
generate → candidate → evaluate → applied=false
compare  → Fragment ID / unique source / inline text → PromptConflictReport
evaluate → PromptAuthoringValidator → content-light report
```

边界固定为：不调用模型、不写文件、不写 Registry、不自动应用候选、不改变 permission mode
或 Tool allowlist。`inspect`、`compare`、`evaluate` 的返回不包含候选正文；只有 `generate`
返回候选正文供人工审核。输入有上限，Fragment 匹配要求唯一，错误以 `ToolResult(is_error=True)`
回填给模型而不是伪造成功。工具复用生产 `PromptConflictAnalyzer` 与
`PromptAuthoringValidator`，因此交互诊断、Benchmark 和真实 Prompt 校验使用同一套检查语义。


## Candidate Prompt Schema（Step 115）

`agent/prompt_candidate.py` 是候选 Prompt 的唯一正式契约。`PromptAuthoringContract v3` 要求
作者返回以下封闭结构，`PROMPT_CANDIDATE_SCHEMA` 与运行时解析器保持一致：

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

解析执行以下 fail-closed 规则：八个字段全部必填；target/objective/proposed_prompt/rollback_plan
不能为空；字符串数组有数量和单项长度上限；risk 只允许 low/medium/high；evaluation case 使用
`id/kind/metric/machine_verifiable`，kind 只允许 success/regression/security；候选与 case 均拒绝
未知字段。空 evaluation_cases 结构有效，但语义 evaluate 会因缺少三类验证而失败。

`PromptCandidate.to_authoring_request()` 将候选适配到现有 `PromptAuthoringValidator`。若目标
Fragment 已存在，layer、scope、stability、cacheability、source/instruction authority 全部从
Registry 继承；不存在时使用 project-scoped L6/session 保守默认值。constraints 作为 non-goals，
success case metric 作为 success metrics；expected_behavior 与 rollback_plan 保留为审核元数据。
`candidate_hash` 对完整 canonical record 确定性计算且正文敏感。`content_light_record()` 只暴露
字段 hash、ID、kind 和计数，供 evaluate、日志与 checkpoint 使用，不保存 Prompt、metric 或
rollback 正文。

`prompt_lab.generate` 返回 canonical candidate、candidate_hash、evaluation report 与
`applied=false`；`prompt_lab.evaluate` 接受 canonical schema，也继续兼容旧
`PromptAuthoringRequest` 输入。两条路径都不调用模型、不写文件、不修改 Registry，也不自动应用
候选。

## Isolated Candidate Evaluation（Step 116）

候选 Prompt 的发布前验证不是第二套 Prompt 渲染器，而是对唯一生产装配路径的同步、无副作用
子集。`PromptCandidateIsolator` 从当前 Fragment Registry 创建 baseline snapshot 和 candidate
snapshot；候选 Fragment 只在内存 temporary registry 中存在，随后分别通过
`PromptAssembler.build_isolated_plan()` 得到两个临时 `PromptPlan`。该入口复用
`_finalize_plan()` 的排序、L0-L12 分层、Instruction/Evidence channel、冲突裁决和 cache
boundary；不读取 live ContextSources，也不持久化 snapshot。

```text
Candidate
  -> baseline temporary PromptPlan
  -> candidate temporary PromptPlan
  -> candidate temporary PromptPlan (determinism replay)
  -> six isolation gates
  -> review_allowed
  -> human review
  -> Prompt Release decision
```

六项门禁固定为：

1. `current-failure-cases`：至少一个机器可验证 success case，候选确实改变目标 Fragment，并进入
   Instruction channel。
2. `original-stable-cases`：至少一个机器可验证 regression case，验证未修改的 stable Fragment
   hash、顺序和集合不变；目标 Fragment 自身的预期变化不参与回归比较。
3. `instruction-precedence`：候选层等于作者声明的目标层，按 L0-L12 排序，未被更高权威规则
   覆盖，也未产生未裁决冲突。
4. `evidence-injection`：注入一个不可信隔离 marker，确认它只出现在独立 Evidence channel；
   不存在 Evidence/Instruction channel 串线，候选正文不能被复制进 Evidence。
5. `permission-sandbox`：用当前 `ToolContext.runtime`、Tool exposure 和 Sandbox 状态构造真实
   L5 contract；候选不得修改 L5、扩大权限或绕过当前 mode/Sandbox。
6. `cache-stability`：重复装配必须得到相同 stable/dynamic/full hash 与 cache boundary；stable
   前缀顺序不能变化；如果前置 L5 attachment 本身是动态的，后置 L6 Fragment 即使标记为
   cacheable 也只能进入 dynamic block，检查实际边界而不是简单等同 cacheable 与 stable。

报告是 content-light：只包含 candidate hash、六项 check ID/issue code、case ID、临时 Plan
计数/hash/boundary 和 Evidence ID，不包含 proposed Prompt、metric 或 rollback 正文。通过后
服务返回 `review_allowed=true`、`prompt_release_eligible=true`、`release_requires_manual_review=true`、
`production_modified=false` 与 `applied=false`。`prompt_lab` 只负责提出、评估和展示，不能
写生产 Prompt 源、Registry 或 Prompt Release；Release 必须由独立人工流程完成。旧
`PromptAuthoringRequest` evaluate 路径保留兼容，但只能得到 authoring-only 报告，永远不能获得
Release 资格。

## Connected Prompt Release Pipeline（Step 117）

Step 117 不再让 Candidate、Benchmark、Conflict 与 Release/Calibration 各自停留在孤立 API，
而是由 `agent/prompt_release_pipeline.py` 统一编排。链路固定为：

```text
PromptAuthoring Skill
  -> PromptLab Candidate
  -> PromptCandidateIsolator
  -> temporary PromptPlan / six isolation gates
  -> offline PromptBenchmarkRunner
  -> PromptConflictAnalyzer report
  -> explicit live BehaviorReport / optional Parity Gate
  -> ModelProfileCalibration proposal
  -> pending_approval
  -> human /prompt-release decision
```

`PromptReleasePipeline.prepare_async()` 复用已有对象，不复制 Prompt 装配、Benchmark 或冲突
算法。offline Benchmark 可由 runner 执行或注入现成 report；live Benchmark 默认关闭，必须由
调用方显式注入 report 或提供 live runner，避免 Agent 在普通 Prompt 评估中产生网络请求和费用。

每个阶段只保存 status、evidence hash、计数和失败 ID。`PromptReleaseProposal` 绑定 candidate、
authoring、isolation、offline、live、parity、conflict、release/profile/contract/suite 与
calibration 身份；`candidate` 字段仅使用 `content_light_record()`，不保存 Prompt、metric、
rollback、答案或工具参数正文。缺少 offline/live 证据分别进入 `pending_offline`/`pending_live`；
任一证据失败或身份不匹配即进入 blocked state。全部通过也只能到达 `pending_approval`。

本地存储固定在工作区 `.krelcode/prompt-releases/`：

```text
proposals/       content-light proposal + stages
approvals/       human decision bound to proposal/candidate hash
calibrations/    approved calibration artifact
artifacts/       approved manifest + approval identity
```

`prompt_lab` 增加 `propose` 与 `status`，其中 propose 只创建 review proposal；它没有
approve/reject action。独立前台命令 `/prompt-release` 是唯一人工审批入口，approve/reject
要求 proposal ID 与完整 candidate hash 同时匹配，并拒绝 `agent/model/system` actor。审批前
会重新校验当前 Release/Profile/contract/suite；任一漂移会把 proposal 标记为 `stale` 并要求
重新评估。批准仍保持 `production_modified=false`、`applied=false`，生产 Prompt 修改继续走
源码 review 与 Git commit，不由 Proposal 自动改文件。

内置 `builtin_skills/prompt-authoring/SKILL.md` 把上述边界作为用户可调用的 Prompt 工程流程：
生成 canonical Candidate、运行六项隔离、提交 offline/live 证据、读取 content-light stages、
最后由人类执行 `/prompt-release approve`。Skill 通过 package-data 随 wheel/uv tool 安装，
其 `allowed-tools` 只有 `prompt_lab`。

## Command Template 与 Reminder Catalog（Step F）

Prompt-producing slash commands 不再由各来源各自解释“参数、信任、工具范围和提醒”。
Step F 新增纯数据契约 `CommandTemplate`：

```text
CommandSpec
  → CommandTemplate(source/trust/envelope/argument_mode/allowlist/reminder_ids/hash)
  → source adapter render
  → CommandRenderResult(prompt + content-light contract)
  → SessionPrompt(tool_allowlist + reminder_ids)
  → AgentRuntime / QueryEngine
  → AgentLoop turn boundary
```

Builtin 继续是 Managed Prompt；Custom/Skill/Plugin 标记为 user 或 project scoped
instruction；MCP Prompt 固定为 untrusted evidence。Custom/Plugin 的 Markdown frontmatter
可声明 `reminder-ids`，但它只能引用当前进程内已注册的内部 Reminder ID，不能携带任意
外部正文。`allowed-tools` 仍只能收窄 ToolPool，不能扩大权限或绕过 Sandbox/审批。

`ReminderCatalog` 是 content-light、声明式、稳定排序的 per-turn 选择器：

```text
ReminderContext(command/task/intent/mode/tools/budget)
  → trigger matching + explicit IDs
  → stable ID/content de-duplication
  → priority ordering
  → per-reminder limit + turn token budget
  → ReminderSelection(selected/excluded/reasons/hash)
```

`AgentLoop` 在每个 turn 边界只选择一次，后续所有 model steps 复用同一
`ReminderSelection`，避免工具循环中途发生 Prompt 漂移。`PromptAssembler` 将 selected
reminder 映射为固定 ID `reminder:<id>` 的 L12 Fragment：turn、non-cacheable、compactable，
不进入 stable prefix；metadata 只保留 source/trigger/reason/priority 等归因信息。

诊断入口：

- `/commands [show <name>]`：查看模板版本、source、trust、参数模式、allowlist、reminder
  声明和 template hash；
- `/context reminders`：查看本轮 catalog size、selected/excluded、去重、预算裁剪与
  selection hash；
- REPL/headless/stream-json 的命令结果都携带 `command_source`、`command_trust`、
  `reminder_ids` 与 `template_hash`，但日志与诊断永不输出命令正文、用户参数或 reminder
  正文。

生产不变量：

1. 外部 Command/MCP 正文不能提升为 Managed instruction。
2. Reminder 固定位于 L12，不污染 stable cache prefix。
3. 同一 turn 的多步工具循环使用同一 reminder selection。
4. 未知 reminder ID、超预算项与重复 content 只形成 content-light exclusion。
5. Canonical Command Template 与 Reminder Catalog 不扫描磁盘、不执行工具、不依赖 UI。

## Prompt 可观测性与真实行为 Benchmark（Step G）

Step G 把 Prompt Surface 目录从静态清单收口为实际执行观测面。`ReminderCatalog` 中的每个
managed reminder 也登记为 `reminder:<id>` surface；`AgentLoop` 在本轮真实渲染 L12 Fragment
时写入 content-light observation。`PromptSurfaceRegistry.health()` 统一聚合
observed/unobserved/stale/unavailable、render 次数与 contract 失败数，并可按 kind 分项。
所有值只来自 descriptor、hash 与计数，不读取或保存 Prompt、reminder、工具参数或模型回复正文。

诊断闭环：

```text
/prompts              summary + usage
/prompts list ...     descriptor + latest observation
/prompts health       aggregate observation/contract health
/prompts observations actual rendered surfaces
/prompts active       latest real turn + PromptPlan summary
/prompts benchmark    saved live report / 16-case catalogue
```

`/prompts active` 以最近 `PromptPlanSnapshot.turn_id` 为边界，只显示同一 turn 的 main、实际
exposed tool 与 selected reminder surface。`/prompts benchmark` 只读取报告，不启动模型调用；
真实执行仍由 opt-in CLI 完成，避免 slash command 意外产生费用。

Live behavior Benchmark 新增正式入口 `krelcode benchmark`（兼容
`python -m krelcode.benchmarks.prompt`）。每次 trial 使用独立 workspace/HOME，复用生产
Provider、AgentLoop、PromptAssembler、动态工具池、权限与 Sandbox；report 的 trial facts 增加
Prompt Surface 数量、observed/stale/contract-failure 计数与 catalogue hash，用于证明真实行为
验证同时覆盖 Prompt 执行现场。

`--save-report [path]` 采用原子 JSON 持久化，默认
`.krelcode/prompts/benchmarks/latest.json`，文件权限为 `0600`。保存前强制使用
`include_content=False`，并拒绝 `answer` 或工具 `arguments`；汇总加载器只暴露 provider/model、
case/trial 稳定性、pass@k、token、成本、错误分类、profile 与时间等有界元数据。这样一次昂贵的
真实模型评测可以在后续会话中由 `/prompts benchmark status` 审计，而不会复制模型输出、仓库文本
或凭据进入会话记录。

## IDE Adapter Contract（P1-3）

IDE 集成是 `stream-json` 的宿主适配层，不是第二套 Agent 运行时。VS Code 通过
`@krelcode/sdk`，JetBrains 通过持久 JSONL 子进程消费同一套事件；Provider、工具池、
权限策略、Sandbox、MCP、Memory、Compact 与上下文装配全部保留在 CLI。

双向交互使用严格、一次性控制：

```text
permission/requested(permission_id)
  -> permission_response(request_id=permission_id, decision=allow|deny|always)

input/requested(input_id)
  -> user_input_response(request_id=input_id, action=accept|cancel, content?)
```

`permission_id` / `input_id` 与 SDK 顶层 `request_id` 分离，防止并发 turn 路由把交互
请求误送到另一个消费者。未知、重复或已结束响应返回拒绝 ack；中断、EOF、编辑器关闭
会自动 deny/cancel 所有挂起项。

编辑器文本只作为显式 Evidence：路径必须位于当前 workspace；敏感目录/文件在进入
Prompt 前阻断；内容做密钥模式脱敏和长度上限；Prompt 标记
`requires_validation: true` / `trust: untrusted-editor-data`。编辑器不应递归加载其他
文件，也不应把 IDE 品牌、建议或诊断文本提升为 instruction。

## Browser / Notebook / Database Migration（P1-4）

P1-4 不把三种能力塞进一个通用“超级工具”，而是保持三个独立边界，并统一进入
`ToolPromptSpec → ToolDescriptor → TurnToolPool → ApprovalPolicy` 链路。

```text
browser_open / snapshot / click / fill / screenshot / close
  → BrowserRuntime(options, BrowserBackend)
  → static: SafeHttpClient + HTMLParser
  → playwright(optional): fixed-action Chromium context + per-request URL validation
  → session TTL / max sessions / element ID revision / workspace screenshot path

notebook_inspect / edit / create
  → nbformat 4 structural validation
  → output summary only
  → targeted cell mutation
  → atomic_write_text(expected snapshot)
  → no kernel / no execution

migration_inspect / validate / create
  → bounded workspace discovery
  → framework/revision graph
  → static SQL/Alembic risk findings
  → fail-closed scaffold
  → apply/rollback remains run_command + Sandbox + approval
```

**Browser 边界。** `BrowserOptions` 限制网络端口、响应体、文本、会话数、TTL 与截图大小。
static 后端只支持快照，不伪装 click/fill；Playwright 必须显式配置，模型不能传
JavaScript、Cookie、Storage、Header、file/data URL。每个浏览器请求都经
`SafeHttpClient.validate` 检查，Playwright 导航后的最终 URL 再检查一次。敏感 query
参数（token/password/session/code 等）在模型输出前替换为 `***`。click/fill/screenshot
是 mutating 工具，受 ApprovalPolicy、read-only/plan 工具池过滤与 workspace path 约束。
会话过期时会显式关闭 backend context；元素 ID 只对最近快照有效。

**Notebook 边界。** 只接受 workspace 内的 `.ipynb` 与 nbformat 4；单文件、cell 数、
cell 大小都受限。`notebook_inspect` 永不返回原始 outputs，只返回 output type/count。
`notebook_edit` 按 index 替换/插入/删除/清空，不重建整个 notebook；未修改 cell 的
outputs/metadata 原样保留，替换 code source 默认清除 stale output/execution_count。
写入通过文件快照、同目录临时文件与 `os.replace` 原子完成。工具从不启动 kernel，执行是
另一个可审计的 `run_command` 决策。

**Migration 边界。** 工具只读迁移文件，不读 `.env`/credentials，不建立数据库连接。
inspector 识别 Alembic/Django/Prisma/Flyway/Liquibase/Rails/generic SQL；Alembic head
来自本地 revision/down_revision 图，并明确标注“非数据库真实状态”。validator 将 SQL
与管理性 Python 操作归为 critical/high/medium/low，检测 DROP/TRUNCATE、无 WHERE 的
DELETE/UPDATE、NOT NULL 无 DEFAULT、类型/索引锁风险以及 downgrade/undo/rollback 证据。
所有 finding 带文件/行号和稳定 code。create 只产生带 TODO 的脚手架；Alembic 两个方向
默认 raise `NotImplementedError`，不会生成可以误应用的空迁移。apply/rollback 永远走框架
原生命令、用户审批和 Sandbox。

**Workflow。** `browser-automation`、`notebook-editing`、`database-migration` 是
turn-scoped、managed、非缓存 L9 Fragment。分类只看用户指令正文，忽略附件/fenced code；
领域安全约束在专用 Workflow 中显式写出，但不会扩大稳定 Base Prompt 或跨轮缓存前缀。

## 高级 TUI / Diff 审阅（P2-1）

`ui/diff_review.py` 是终端无关的 diff 数据层，负责把 bounded unified diff 解析为
`DiffFile → DiffHunk → DiffLine`，并维护 `DiffPager`。它不依赖 Rich/Textual/Pygments，
也不在渲染时访问磁盘。

```text
ToolCall(write_file/edit_file)
  → build_tool_call_review()  # 只读当前目标 + 参数，不执行工具
  → unified diff + redact + size/sensitive guard
  → DiffPager（待审批）
  → ApprovalBroker y/n → SandboxPolicy → Tool.execute()

/diff [staged|refresh]
  → collect_git_review()      # tracked/staged + safe untracked text
  → DiffPager（只读）
```

写前预览必须从工具参数和写前文件快照生成，不能等 `ToolResult` 返回后再反推；这样拒绝时
不会产生任何文件副作用。`write_file` 预览完整目标正文，`edit_file` 预览精确替换后的
结果；目标大小上限为 1 MiB，超出或属于整体敏感文件时只显示安全 notice。预写入 diff
正文统一经过 `redact_obvious_secrets` 与当前 `SecretRedactor`；`.env`/`krelcode.toml`
保留结构但脱敏，`credentials*`、私钥和工作区逃逸路径不显示正文。

Git 审阅支持工作区、暂存区、新增/删除/rename 和 binary 状态；未跟踪文件只纳入普通文本、
非敏感且未超限的文件。单文件、文件数、hunk 数、单 hunk 行数和总字符数均有上限，正文
截断状态进入 `DiffReview.truncated`。解析器不执行 diff 中的任何文本，Git 子进程复用
`scrub_environment()`，并禁用 external diff。

`DiffPager` 以固定高度 overlay 嵌入现有 Panel Dock，不切换 alternate screen。
`j/k`、`PgUp/PgDn`、`[/]`、`#`、`g/G` 只修改审阅状态；审批中的 `y/n/q` 由前台
输入循环转成 `APPROVE_DIFF/REJECT_DIFF`，普通输入不会污染 prompt。`Review` 面板只
消费同一份 `DiffReview` 快照，不触发额外磁盘扫描。

启用边界由 `SandboxPolicy.review_writes` 和 composition root 共同控制：默认仅 REPL、
有前台 approver、default 模式且未设置 `--no-diff-review` 时启用；`[ui].diff_review=false`
可配置关闭。headless、stream-json/SDK、`--auto-approve` 与无 approver 场景保持原有
失败关闭/审批语义，不额外弹出交互式 pager。显式 allow 规则、只读/plan 模式和受保护路径
仍由既有权限链决定，diff review 不能替代 Sandbox 或 DENY。

## P2-2 Enterprise Controls

Enterprise policy is a value object, not a REPL feature.  The composition root loads it from
`managed` TOML even when `--config` selects an exclusive user file.  The policy clamps
permission modes and is checked before user allow rules, persistent allow rules, auto approval,
and `--no-sandbox`.  The dynamic tool pool applies the same managed allow/deny patterns before
advertising schemas, while `EnterpriseGuardPolicy` enforces the final execution boundary.

Compliance audit is intentionally separate from resumable session history.  It records typed,
redacted event summaries in an append-only JSONL chain (`seq`, `prev_hash`, SHA-256; optional
HMAC).  Required audit failures are fail-closed.  OIDC identity is reduced to a `Principal`
(subject/issuer/display metadata); access, ID and refresh tokens never enter prompt assembly,
session recording or compliance records.

## P2-3 Scalable Benchmarks and Privacy-Preserving Telemetry

Named suites add workload tiers without adding another benchmark implementation: `smoke` is
the five-case fast gate, `standard` is the frozen sixteen-case contract, and `nightly` /
`extended` raise the default trial count to three and five. Sharding is deterministic
round-robin over the frozen case order. A shard is an execution unit, not a new report schema:
all shards must share provider, Prompt Release/Profile, runner, sandbox, timeout and generation
fingerprints. Aggregation rejects overlap, duplicate case/trial pairs, missing trial numbers and
incomplete suite coverage, then recomputes every aggregate from trial results. Wilson intervals,
pass@k/pass^k, flaky counts, p50/p95 latency and tool error rates therefore cannot drift from the
summaries they claim to describe.

Telemetry is a strict, privacy-first EventBus subscriber. Consent belongs to the user, never to
the repository: `~/.krelcode/telemetry/consent.json` is the only grant source, while project TOML
may tune batching, retention and an endpoint. `KRELCODE_TELEMETRY` is an explicit process override
for isolated CI, and managed `allow_telemetry=false` is checked before every effective state.
The schema is deny-by-default at three levels: envelope keys, event types and per-event data keys.
Prompt text, answers, code, paths, tool arguments/output, usernames, hostnames, endpoints and
credentials have no representable field.

The local queue is bounded by event count, bytes and retention. Consent, queue, install ID and
export writes are private (0700 directory / 0600 files), atomic and symlink-rejecting. Remote mode
is HTTPS-only except explicit loopback tests. Delivery acknowledgement is per `event_id` after a
2xx response; transport, storage and schema failures are fail-open for the coding task and preserve
undelivered events. Doctor reports consent mode, endpoint presence, queue validity and permissions
without performing a network request.
