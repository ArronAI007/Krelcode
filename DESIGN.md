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
│   └── errors.py     # KrelcodeError / LLMError / ConfigError
├── llm/         # 统一 Provider 机制（模型适配层）
│   ├── base.py       # 统一契约：LLMProvider 协议、LLMEvent、规范化 finish/usage、collect()
│   ├── transport.py  # HTTP 传输抽象(urllib→asyncio桥) + SSE 解析器
│   ├── registry.py   # Provider 工厂：名字+配置 → 实例；resolve_settings 优先级解析
│   └── providers/    # 每个 adapter 独占一种 wire format
│       ├── openai.py     # OpenAI 兼容：SSE 流式、tool_call 增量聚合
│       ├── anthropic.py  # Anthropic：content blocks、tool_use/result、system 抽离
│       └── mock.py       # 脚本化响应，记录调用（测试/离线体验）
├── agent/       # 主循环：只 import core 与各端口，不认识任何具体工具/模型
│   ├── loop.py       # AgentLoop
│   └── prompts.py    # 系统提示词组装（后续注入 AGENTS.md 等）
├── tools/       # 工具层（实现 Tool 端口）
│   ├── base.py       # Tool 协议 + ToolResult + ToolContext
│   ├── registry.py   # ToolRegistry -> OpenAI tools schema
│   └── builtin/      # 每个工具一个文件，互相独立
├── safety/      # 策略层（实现 ApprovalPolicy 端口）
│   └── policy.py     # Decision / AllowAllPolicy（Step 6 加沙箱与交互审批）
├── session/     # (Step 8) JSONL 持久化，事件总线订阅者
├── ui/          # 交互层：事件总线订阅者，不被任何层引用
│   └── repl.py
├── app.py       # 组装根(composition root)：唯一知道所有具体类的地方
└── __main__.py
```

依赖方向（严格单向）：
`ui → agent → core`；`llm / tools / safety` 是适配器，只依赖 `core`，
由 `app.py` 注入 `agent`。`session`、`ui` 通过订阅事件总线旁路消费，内核对其零感知。

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
    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult: ...

# safety/policy.py —— 审批端口
class ApprovalPolicy(Protocol):
    async def check(self, tool_call: ToolCall, ctx: ToolContext) -> Decision: ...
# Decision(allowed: bool, reason: str)
# 实现：AllowAllPolicy（--no-sandbox）/ SandboxPolicy（默认）：
#   · 只读工具(get_time/read_file/grep/glob)放行；write/edit 由路径约束兜底放行
#   · run_command → safety.commands.classify_command()：DENY/ASK/ALLOW 三级
#     - 按控制符切段(punctuation_chars)逐段分级，整条取最严格；重定向升级为 ASK
#     - 无条件 DENY：sudo/dd/mkfs/shutdown、rm 灾难目标、curl|sh、fork 炸弹
#     - ASK：执行代码/装依赖/git 写操作/删文件/网络/未知命令
#   · ASK 处理链：持久化 allow 规则(按程序名) → --auto-approve → 交互 approver(y/n/a)
#     → 都没有则拒绝（fail-closed）；拒绝原因经 PermissionDenied 事件 + is_error 回填 LLM

# core/events.py —— 事件总线
class EventBus:
    def subscribe(self, handler) -> Callable[[], None]  # 返回退订函数
    async def emit(self, event: AgentEvent) -> None      # handler 可同步可异步
```

## 事件清单（UI/会话/审计的消费面）

RunStarted · StepStarted · LLMStreamDelta · LLMResponseReceived ·
ToolCallStarted · ToolCallCompleted · PermissionDenied · RunFinished · ErrorOccurred

## 主循环语义

```
run(user_input):
    history += user 消息; 失败计数清零
    for step in 1..max_steps:
        assistant = 消费 llm.stream(history, registry.tool_specs())
        history += assistant
        if 无 tool_calls: return 文本        # 结束
        results = asyncio.gather(           # 同批 tool_calls 并行执行
            审批(policy.check) → 查找 → execute → 异常转 is_error
            for tc in tool_calls)
        按原序 history += role=tool 消息     # 并行完成，保序回填
        if 某签名连续失败 ≥ repeat_failure_limit: return "判定为循环" 中止
    return "达到最大步数" 提示
```

卡死检测签名 = 工具名 + sort_keys 序列化的参数；成功即清零该签名计数。

启动时上下文注入（agent/context.py，失败安全）：
目录树快照（剪枝隐藏/依赖目录，限深 3、限 150 项）+ git 分支与 porcelain 状态
+ AGENTS.md/KRELCODE.md 项目规则（限 8K 字符），包成 <workspace_context> 注入 system prompt。

错误恢复原则：工具抛异常/被拒绝/未知工具，都转成 `ToolResult(is_error=True)`
回填给 LLM，让它自我修正，而不是中断循环。

## 统一 Provider 机制（pi-ai 同款思想）

core 的 Message/ToolCall 是 **wire-free 规范格式**；任何 provider 专属的 JSON
结构都不允许出现在 core/agent/tools 层。每个 adapter 独立负责四件事：

1. **消息互转**：规范历史 ⇄ provider wire format
   - OpenAI: role 消息 + tool_calls/tool_call_id
   - Anthropic: system 顶层抽离、content blocks、连续 tool_result 合并进单条 user 消息
2. **流式翻译**：provider SSE 事件 → 统一 `TextDelta` / `StreamFinished`
   - OpenAI: choices[].delta.content / delta.tool_calls（按 index 聚合分片 arguments）
   - Anthropic: content_block_delta(text_delta / input_json_delta.partial_json)
3. **语义规范化**：finish_reason（end_turn→stop, tool_use→tool_calls, max_tokens→length）；
   usage（prompt_tokens→input_tokens 等，缺失不补造）；
   **思考流**（reasoning_content/reasoning、Anthropic thinking 块）→ 统一
   ThinkingDelta 事件——只用于 UI 展示，永不进入 message.content/history
4. **错误规范化**：非 2xx → `LLMHTTPError(status, body, provider)`；网络错误 → `LLMError`

配套机制：
- **ToolSpec 统一工具描述** `{"name","description","parameters"}`：registry 产出，
  adapter 各自包装（OpenAI function.parameters / Anthropic input_schema）
- **transport 抽象**：urllib 同步实现 + `asyncio.to_thread` 桥；测试注入 FakeTransport 全离线
- **工厂与配置**：`resolve_settings()` 按 显式参数 > TOML([providers.NAME]) > 环境变量 > 默认 解析；
  `create_provider(settings)` 按名建实例；`register()` 是第三方 provider 扩展点
- **非法工具参数约定**：arguments 非合法 JSON 时 adapter 填 `INVALID_ARGUMENTS_KEY` 保留原文，
  循环层转成 is_error 工具结果回填，让 LLM 自我修正

新增一个 provider = 新建一个 adapter 文件 + registry 里注册一行，其余层零改动。

## 上下文管理（Step 7）

- **token 估算**（agent/tokens.py，零依赖）：CJK ≈ 1 token/字，其他 ≈ 4 字符/token，
  精度足够做预算决策；消息含 tool_calls 参数序列化
- **预算压缩**（agent/compression.py ContextManager）：超 `--context-tokens`（默认 10 万）时，
  把「中段历史」渲染后交给 LLM 摘要成一条 `[上下文摘要]` 消息，
  结构变为 `[system] + [摘要] + [最近 keep_recent 条]`；
  切点安全：保留区绝不以孤儿 tool 消息开头（否则 provider 拒绝请求）；
  失败安全：摘要调用出错退化为占位说明，不崩主循环；
  太短的历史（<6 条）不压缩，防抖动
- **大输出落盘**（loop）：工具结果 > 12K 字符（可调）时全文写入
  `<workspace>/.krelcode/tool_outputs/`，历史只留 truncate_middle 版 + 文件引用；
  落盘失败退回纯截断
- 压缩发生会 emit `ContextCompressed(before_tokens, after_tokens)`，UI 可提示用户；REPL `/compact` 经 `maybe_compress(force=True)` 手动触发（不必等预算超限）
- 跨平台：run_command 超时终止在 POSIX 走进程组 killpg(SIGKILL)，Windows 退化为杀主进程（os.killpg/SIGKILL 不存在时不触碰）

## 交互与会话（Step 8）

- **StreamRenderer**（ui/render.py）：EventBus 订阅者，LLMStreamDelta 逐字流式打印
  （首个 delta 打 "agent> " 前缀）；工具活动/审批/压缩事件打断流式行后续接；
  RunFinished 兜底打印未经流式的系统消息（max_steps/卡死中止）。write/color 可注入，测试无需终端
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
    通知回 202，流内服务器请求自动应答 -32601，GET 监听流惰性尝试一次（405 容忍）
  配置 `command` → stdio；`url` → streamable-http（默认），`transport = "sse"` 回退旧规范
- **capabilities 门控**：initialize 握手的 capabilities 决定注册哪些工具——
  tools → 每远程工具一个 MCPTool；resources → list_resources/read_resource；
  prompts → list_prompts/get_prompt；全部 `<server>__<tool>` 命名进同一注册表
- **热重载**（MCPSupervisor）：sync() 配置 diff（dataclass 相等判断：
  未变保持连接、变更先停后启、移除即注销）；check_and_restart() 复活崩溃
  服务器并重注册工具；工具注销走 registry.unregister_prefix("<server>__")；
  REPL /mcp 查看状态、/mcp reload 重读配置触发同步

## 子代理/后台任务（Step 13）

- **SubagentManager**（subagent/manager.py）：spawn(task) 创建独立 AgentLoop
  （自己的历史/事件总线/工具注册表；共享父级 provider、ToolContext、审批策略），
  asyncio.create_task 后台执行；wait(id, timeout) 用 `asyncio.shield` 包裹——
  等待超时绝不取消子任务；send_input 续聊（仅空闲时，上下文延续）；
  shutdown 统一 cancel
- **父代理工具**（subagent/tools.py）：spawn_agent / wait_agent / send_to_agent /
  list_agents，工具持 manager 引用（构造注入），ToolContext 保持纯工作区语义
- **防递归**：子代理工具集 = default_tools()（不含 spawn 系），委派深度硬限制 1 层
- **可观测**：SubagentSpawned/SubagentFinished 上父总线——渲染器打状态行、
  recorder 落 JSONL 审计；REPL /agents 列状态
- 子代理系统提示词要求：专注委派任务、不向用户提问、最终消息即给父代理的报告

## 检查点与回滚（Step 16）

- **CheckpointManager**（session/checkpoint.py）：纯事件订阅者——
  RunStarted 开组（label=用户输入预览）、ToolCallStarted 对 write_file/edit_file
  的目标文件在**首次修改前**做字节级快照（同组同文件只记第一次，保最原始版本）、
  RunFinished 关组（无文件改动的空组直接丢弃）
- 落盘 `<workspace>/.krelcode/checkpoints/NNNN-<ts>/`：meta.json（文件清单）
  + files/ 备份；构造时扫描目录重建组栈 → /undo 跨重启可用
- undo_last()：LIFO 弹出最近已关闭组，逐文件恢复（existed=True 还原备份、
  existed=False 删除新建），单文件失败不阻断其余
- 边界：run_command 的文件修改不可静态追踪，不在快照范围（文档注明）；
  路径解析与工具层同规则（越界路径静默跳过）
- REPL：`/undo`（回滚上一轮）、`/undo list`（查看检查点栈）、`--no-checkpoint` 关闭

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

## Hook 系统与安全补洞（Step 19）

- **写保护补洞**：`krelcode.toml`（agent 配置本体）与 `.krelcode/` 全目录
  （审批白名单/检查点/会话/工具输出）列入 SandboxPolicy 保护路径——
  堵住「LLM 改配置注入 hooks / 给自己提权 / 篡改回滚痕迹」的攻击面
- **HookRunner**（hooks.py）：用户在 TOML 里声明 `[[hooks]]`
  （on = file_edited / tool_completed / tool_failed / run_finished，
  command 支持 {path} {tool} {reply} {workspace} 等模板变量，timeout 可配）；
  EventBus 旁路订阅，典型用途：编辑后自动 formatter/linter、任务完成通知
- 安全模型：hooks 只能来自用户配置（配置即信任，直接执行不走审批）；
  模板变量值来自 LLM 工具参数，**一律 shlex.quote** 防命令注入；
  钩子失败/超时只记日志（executions 留审计），绝不影响主循环

## 韧性与自我修正辅助（Step 20）

- **RetryingProvider**（llm/retry.py）：LLMProvider 的透明装饰器，
  组装根按 `--retries`（默认 2）包装真实 provider（mock 不包装）：
  · 可重试：HTTP 408/409/425/429/5xx、网络/超时类 LLMError
  · 不重试：401/403/400、ConfigError（确定性错误重试无意义）
  · **emitted 标志**：已产出 TextDelta 后绝不重试（重放会重复输出）；
    常见瞬态故障（HTTP 层）都发生在首事件前，覆盖面足够
  · 指数退避 base*backoff^attempt（封顶 max_delay），sleep 可注入（测试零等待），
    on_retry 回调接 UI 日志
- **edit_file 失败辅助**：多处匹配 → 错误信息带每次出现的**精确行号**
  （LLM 可据此扩大上下文）；未匹配 → difflib.get_close_matches 给出
  最接近的行（带行号）提示缩进/拼写差异。目标：把「edit 失败 → 盲改重试」
  变成「edit 失败 → 一轮定向修正」

## 思考模型支持（Step 21）

- llm 层新增 `ThinkingDelta` 事件；DeepSeek/Qwen 的 `reasoning_content`、
  OpenRouter 系的 `reasoning`、Anthropic 的 `thinking_delta` 都在各自
  adapter 内翻译到统一事件（signature_delta 忽略）
- 约定：思考文本**不进** message.content、不进 history、不进会话快照——
  只经 `LLMThinkingDelta` 事件流向 UI（渲染器 dim `[thinking]` 前缀流式展示，
  内部换行压平防串行，任意其他事件到达即收束思考行）
- RetryingProvider/collect 等对 ThinkingDelta 天然透明

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
