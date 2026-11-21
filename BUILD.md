# Krelcode 构建回顾（BUILD.md）

> 本文回答三个问题：这个项目**长什么样**（统计与导览）、**为什么长成这样**
> （关键决策时间线）、**怎么验收**（离线套件 + 真模型脚本）。
> 使用文档见 [README.md](README.md)，全量配置见 [CONFIG.md](CONFIG.md)，契约细节见 [DESIGN.md](DESIGN.md)，
> 逐步骤路线见 [PLAN.md](PLAN.md)。

## 1. 项目统计（Step 121）

| 维度 | 数值 |
|------|------|
| 源码 | 73,009 行（Python，**零第三方运行时依赖**） |
| 测试 | 35,998 行 / 148 个 `test_*.py` 模块 / 1,557 个通过用例 + 2 个本地端口集成 skip |
| 提交 | 167 个增量提交，每个功能阶段均包含测试与语义化 commit |
| 工具 | 默认内置 17 + 只读 `memory_search`（装配 MemoryStore 时）+ `prompt_lab`（Prompt Authoring 开启时；生产只读，proposal 仅写 `.krelcode/`）+ 子代理 9 + Skill 5 + LSP 4 + Web 2 + MCP/Plugin 动态注册；39 个已知工具均有自有 Prompt 契约 |
| Provider | OpenAI 兼容 / Anthropic / Gemini / Mock（统一事件流） |
| CLI 入口 | REPL 对话 · -m 单次（text/json/stream-json）· stream-json 持久会话 · Python SDK · doctor · init · review |
| 文档 | README（上手）· CONFIG（全量参考）· DESIGN（契约）· ARCHITECTURE（对标）· PLAN（路线）· BUILD（本文） |

源码按层分布：

```
agent 22161 · session 11916 · benchmarks 7456 · 顶层(app/config/doctor/scaffold/hooks/review/sdk) 4415
ui 3965 · llm 3797 · tools 4424 · safety 2861 · mcp 2679 · subagent 3085
lsp 1352 · plugins 1105 · core 1023 · skills 1039 · web 923 · jobs 582 · entrypoints 226
```

core 只有 1023 行——内核保持极小、能力在边缘，正是当初对标 pi/dsh 想要的形状。
测试代码量约为源码的 50%，覆盖协议、安全边界与端到端行为。

## 2. 关键架构决策时间线

按 git 历史梳理「转折点」，每条都是先有问题、后有决策：

| 版本 | 转折点 | 决策与理由 |
|------|--------|-----------|
| v0.9 | **wire format 泄漏**（用户指出 Step 1 未完成统一 Provider） | core 消息去 OpenAI 化；转换下沉适配器；用 Anthropic 异构格式证明抽象成立。这是全项目最重要的一次返工 |
| v0.9 | 并发模型 | asyncio 全面异步（用户拍板）；阻塞 IO 一律 to_thread/线程桥 |
| v0.10 | 工具协议 | 统一 ToolSpec（name/description/parameters），各 adapter 自己包装（function.parameters / input_schema / functionDeclarations） |
| v0.12 | MCP 传输 | MCPTransport 端口拆分 stdio/SSE；Future 不能在外部线程 set_result → 统一 call_soon_threadsafe 回投 |
| v0.15 | 跨平台 | os.killpg/SIGKILL 是 POSIX-only → _terminate_process_tree 平台分派 |
| v0.17 | 双重回滚语义 | checkpoint 管文件级回滚（/undo），auto-commit 管历史留痕，互补不互替 |
| v0.18 | 防自我提权 | .krelcode/allow_rules.json 列入写保护——LLM 不能给自己改白名单 |
| v0.19 | hooks 攻击面 | 引入配置驱动 hooks 前，先把 krelcode.toml 与 .krelcode/ 全目录列入写保护（堵注入） |
| v0.20 | 重试语义 | 已产出 TextDelta 后绝不重试（emitted 标志）——重放会造成重复输出 |
| v0.23 | 打断修复 | sanitize_history 下沉 core：主代理 /stop 与子代理 interrupt 共用同一套「孤儿 tool/assistant(tc) 级联弹出」 |
| v0.24 | Gemini 无 tool-call id | 合成 gem-N id + 转换历史时重建 id→name 映射（functionResponse 按 name 配对） |
| v0.26 | REPL 架构 | 从「读一行跑一轮」重构为常驻 reader + 状态机，才可能有运行中 steering |
| v0.30 | usage 语义分裂 | OpenAI prompt_tokens 含缓存、Anthropic input_tokens 不含 → 统一为 Anthropic 语义（input 扣除 cache_read），成本公式两家通用且防双计 |
| v0.31 | 热切换原子性 | /model 先建新 provider 并验证 key，成功才替换——切换失败不破坏会话 |
| v0.34 | 协议时序 | MCP notifications/initialized 必须 await 实发——fire-and-forget 的调度延迟会让它晚于后续请求（spy 调试抓到） |
| v0.36 | 多模态持久化 | 图片按 source_path 存 JSONL（每轮快照×base64 会撑爆文件），恢复时重载 |
| v0.40 | 并行写冲突 | 隔离子代理走 git worktree 分支而非目录拷贝——复用 git 的合并语义，成果以分支交付（merge/cherry-pick），不做危险的自动合并 |
| v0.33 | 扩展思考回传 | Anthropic thinking 块（含 signature/redacted）挂 Message.provider_meta 不透明状态位随历史回传——core 只携带不解读，wire 知识仍锁在 adapter 内 |
| v0.67 | 生态边界 | Skill 是指令包、MCP 是远程协议、Plugin 是本地可信 Tool 适配器——三者共享 Tool 协议但加载/信任模型分开，不做插件微内核 |
| v0.67 | 会话恢复语义 | `/resume` 与 `/fork` 都建立 copy-on-write 式新分支，源会话不改写；rename/delete/metadata 由 SessionLifecycle 独占，保证 recorder/runtime id 同步 |
| v0.68 | 运行时权限边界 | 权限模式从启动参数升级为独立运行时管理器；统一 allow/ask/deny 规则，旧白名单保持兼容；显式 ask、deny 和受限模式都有不可被快捷键/auto-approve 绕过的安全下限 |
| v0.68 | Plan 执行确认 | 计划生成与执行授权分离：Plan 模式只读调查，批准后恢复进入 Plan 前模式并自动启动执行轮，避免“看到计划即视为授权” |
| v0.69 | 对话级状态恢复 | rewind 不是简单截 history：稳定 turn/session 身份把 history 边界、文件 checkpoint、plan 审批/权限合成一个事务；预检失败不改会话，文件写入失败尝试回滚 |
| v0.69 | 历史分叉语义 | fork 只复制选定边界的 history + plan，不静默改工作区；“恢复文件”和“从旧对话另开分支”明确拆成 `/rewind` 与 `/rewind fork` 两个动作 |
| v0.70 | 后台 Job 生命周期 | Job 刻意不绑定 turn abort，避免 dev server/watcher 被一轮结束误杀；由 runtime close 或显式 kill 回收。模型经 `job_output(after_sequence)` 游标轮询，UI/协议经事件实时观察，完整输出落有界脱敏日志而非塞回模型历史 |
| v0.71 | 交互式子代理 | Feed、pause/resume、kill、Definition、sidechain 拆成独立组件；AgentLoop 只新增通用 step gate。Pause 不取消 Task，Kill 才取消；Definition 只能收缩工具集；sidechain 可审计但不自动注入父上下文或复活旧 Task |
| v0.72 | Prompt Fragment Registry | system prompt 的唯一组装点从 `build_system_prompt` 迁移到来源化 Registry；cacheable 前缀、stable hash 与 provider cache offset 成为数据结构，而不是依赖拼接顺序。compact 同时拆成“只预览”和“唯一正式修改入口”，避免 UI 复制压缩逻辑 |
| v0.73 | 结构化 Panel Dock | Tasks/Activity/Context/Memory 直接消费 reducer 与请求快照；输入框 `!shell` 与 `#memory` 成为窄入口，不混入模型 history |
| v0.74 | 请求级 Context 观测 | token 分项、预算、cache 边界与 hash 从真实 provider-bound 请求派生；精简快照贯通 UI/JSONL/stream-json/SDK，且不包含 Prompt 正文 |
| v0.75 | Context assembly 边界 | 临时 system context 不再由 AgentLoop 手拼，统一经 ContextSource/ContextAssembler；分类/priority/注册顺序确定结果，单个可选来源失败只留下异常类型诊断，不让整轮请求失效 |
| v0.76 | Memory 分层作用域 | 记忆从单文件项目笔记升级为 user/project/session 三作用域结构化记录；文件位置决定 scope，旧 JSONL 原位兼容，session 生命周期跟随 setup/fork/resume，注入预算优先更具体作用域 |
| v0.77 | 规则分层与按需加载 | user/project 规则进入稳定前缀，目录规则只按当前轮目标路径加载，避免 monorepo 全树扫描；命中后的 `directory:rules` 进入 L3 cacheable prefix，不会因其 L3 位置阻断后续 L4-L9 稳定块。@import 从提示词拼接中独立为有来源、有预算、可诊断的安全解析器，拒绝逃逸/敏感文件/循环，并明确规则与 Memory 的职责边界 |
| v0.78 | 真实请求装配与预算 | Snapshot 不再只看统一 Message：Provider 先构造可复用的 PreparedRequest，再从最终 wire body 提取字节/token/hash/缓存段；压缩同时使用完整输入与真实输出预留，超限只重新装配一次。旧 Provider 通过 estimated 回退保持兼容 |
| v0.79 | Working Context 与 Tool Microcompact | 当前任务事实从长期 Memory 分离为 bounded/redacted session store；真实预算链路先做零 LLM 工具分级压缩，保护当前轮/错误/写编辑/计划，仍超限才摘要。工具结构化元数据可随会话恢复，微压缩事件贯通 UI/JSONL/stream-json |
| v0.80 | Structured Compact v2 | compact 从自由摘要升级为严格 schema 的 goal/completed/decisions/files/tests/open issues/next steps/critical constraints；失败时 Working Context 降级、旧摘要合并、turn 边界与高低水位控制，pre-compact history、精确 Working Context 与文件 Hash 持久化后由 Rewind 恢复和失效校验。可观测元数据贯通 UI/JSONL/stream-json，避免历史和工具结果反复摘要 |
| v0.81 | Auto Memory | 长期记忆写入改为候选流水线：显式“记住”、用户纠正、成功测试复用、架构重复确认和任务完成事实先进入 project-local 队列；统一脱敏、精确去重、证据累积、冲突检测与阈值晋级。默认 suggest，`/memory review` 人工审核，auto 只晋级高置信无冲突 knowledge；新增只读 `memory_search` 与 pin 检索优先 |
| v0.82 | Context v3 默认与安全收口 | 不再把 v3 只当内部命名：engine/trust/origin 成为跨 assembly、plan、usage、命令和 stream-json 的可检查契约。Memory 从 system Prompt Registry 分离为 untrusted knowledge layer；Working/Runtime context 使用 data-not-instructions envelope 并转义标签；单文件解释过滤无关 git/tree，重复 goal 不注入。legacy/v2 保留一个版本回退，安全回归覆盖注入、路径逃逸、损坏输入、取消、长会话与隔离 |
| v0.83 | 扩展生态 P0 | Plugin v1 的单一 Tool 适配器升级为统一扩展平面：ExtensionRegistry 追踪 Plugin/Tool/Skill/Agent/Command/Hook/MCP 来源；Plugin v2 单包声明六类组件并保持 v1 兼容；install/update/remove/enable/disable 带原子替换、路径/symlink/体积校验和 scope 隔离状态；配置改为 user → project → local → managed 合并，managed fail-closed，`--config` 保持独占 |
| v0.84 | 生产可用性 P1 | MCP 四类列表分页、`list_changed` 串行原子刷新、Resource Templates 与 OAuth（refresh/client credentials/metadata discovery/动态注册/PKCE/401 retry/`0600` cache）；新增 `CommandSource.CUSTOM` 和 Plugin/Custom 统一优先级；Hook 升级为 command/http/prompt 三执行器并强化 URL/Header 脱敏；Subagent Definition 支持独立 model/provider、permission-mode 和 MCP 集合。单组件失败继续隔离，所有新元数据不保存 Prompt 正文、Hook Header 或 MCP headers |
| v0.85 | PromptAssembler 唯一入口 | Fragment 扩展 layer/stability/trust，建立 L0-L12 固定层级与 `PromptAssembler.assemble() → PromptPlan → ProviderMessages`；AgentLoop 不再用 `ContextAssembly.messages_for()` 生产请求，动态时间、工具结果、当前请求与 reminder 被隔离在稳定前缀之外，ContextAssembly 降为观测/恢复兼容视图 |
| v0.86 | 显式 system cache boundary | `ProviderMessages` 新增 provider-neutral `SystemPromptBlocks`，逐 Fragment 保留 stable/dynamic block、双 hash 与 `cache_control` 边界；Anthropic 不再提前 join system，只在最后 stable block 标记 ephemeral，OpenAI-compatible 在 wire 层固定顺序合并但观测元数据不丢失。边界、block 数与双 hash 贯通 PreparedRequest、ContextUsageSnapshot、ContextPlanSnapshot、`/context cache` 和 TUI |
| v0.87 | 动态工具池与 ToolSearch | `ToolTier` 将 turn 工具目录拆为 core/discoverable/deferred：core 带完整 Schema，discoverable 只暴露名称与单行描述，MCP/Plugin 默认 deferred；`tool_search` 只在已完成权限/运行模式/feature/MCP/禁用过滤的候选集内搜索并加载 Schema，新工具下一模型 Step 才可执行。Registry、Provider tools、动态目录、搜索和 `/tools` 使用名称稳定排序，避免注册顺序改变 Prompt Cache 前缀 |
| v0.88 | 上下文增量附件 | `ContextAttachmentStore` 将 environment snapshot/delta、date、Skill/Agent/MCP catalog delta、token budget 与 L5 mode/permission contract 拆成固定 ID、独立 revision 的动态 block；环境按 section hash 做增量，catalog 只读已加载快照，MCP instructions 标记 untrusted。相同内容不更新 revision；PromptAssembler 仍保持唯一装配入口，环境变化只改变对应 dynamic block 与 `dynamic_hash`，稳定前缀和持久 history 不变。`/context attachments` 提供无正文 revision/generation/token 观测 |
| v0.89 | Memory 降级为 Evidence | 新增统一 `InstructionSource/EvidenceSource` 分权契约：L0-L6 在 PromptAssembler 标注 instruction_source，L7 标注 evidence_type/instruction=false；Memory 改用 `[Evidence: Memory]` envelope（source/scope/confidence/requires-validation），Resume、子代理、memory_search 复用该格式，MCP/Web 保留转义后的 `<evidence_set>`。Memory 固定进入 L7 Evidence、turn stability、untrusted 且不可缓存；`/context why|sources` 暴露来源权威和 validation 要求但不记录正文 |
| v0.90+ | Base Prompt 生产级拆分与模型 profile | `core:base*` 拆为身份、执行循环、工具纪律、编码质量、安全权威、验证、沟通与完成度八个独立 L1 Fragment；`[prompt].profile=auto` 按最长模型模式 → provider family → default 解析 Claude/DeepSeek/OpenAI/Gemini profile。Profile 仅可追加 optional section，安全 required section 不可覆盖；主 Agent、`/model` 热切换和按实际子模型运行的后台子代理复用同一解析与 PromptAssembler 路径，`/context why` 暴露 profile/match/hash 但不记录正文 |
| v0.91 | L5 Mode/Permission Contract | `ModePermissionContract` 由实际 RuntimeSnapshot、过滤后的 ToolPool、ApprovalPolicy 与 ExecutionSandbox 单一生成；主 Agent、子代理、REPL/print/JSON/SDK/review 共用 L5 attachment。运行时 AUTO_APPROVE 与显式 ASK 例外进入 approval 摘要；子代理的 permission/tool/MCP scope 只能相对父级收窄。模式切换会重建工具池并刷新 contract hash，但不改变 stable prefix；contract/exposure hash 贯通 PromptPlan、ContextCacheLedger、`/context why` 与 `/context ledger` |
| v0.92 | 工具级 Prompt Playbook | 新增独立 `ToolPlaybookCatalog`：由 Provider 实际 advertised tools 选择读取、搜索、编辑、命令验证、计划、ToolSearch、后台 Job、Subagent、LSP、Web 与 Memory 的 L5 操作规则。Playbook 只渲染本轮可用工具，按工具族稳定排序、session cacheable、不可 compact/toggle，并携带 active tools/version/content hash 进入 PromptPlan、ProviderMessages、token accounting 与 `/context why`；禁用、过滤或未加载工具的指令不会泄漏 |
| v0.93 | 任务类型 Workflow Fragment | 新增确定性 `TaskWorkflowClassifier + TaskWorkflowCatalog`：每轮从 general、bug-fix、feature、refactor、explanation、review、test-verification、repository-exploration 中选择一个 L9 Workflow。分类忽略附件正文与 fenced code，显式文件解释 Intent 优先；Workflow 作为 turn/managed instruction 注入，不改变稳定缓存前缀、不可 compact，并通过 PromptPlan provenance 暴露 task type/confidence/signals/version/content hash |
| v0.94 | Live Model Behavior Benchmark v2 | `--live` 使用真实 Provider、生产 `AgentLoop`、隔离 HOME/workspace、真实工具与 Sandbox 执行六个客观行为场景；新增 `review-no-edit` Workflow 验收。每个 trial 有独立超时与 timeout/provider/sandbox/configuration/internal 错误分类，报告加入 stability/pass@k、工具/token/cache/成本、失败分布、Krelcode/Python/平台/Sandbox 配置和 suite hash，默认不保存答案与工具参数正文 |
| v0.95 | Failure-trajectory Prompt tuning | 新增确定性失败轨迹归因器：从 failed checks、真实工具轨迹、Workflow、runtime error 和实际 active Fragment 映射到 core/tool_playbook/workflow/rule 来源，生成带风险、证据、受影响 case 与回归步骤的 Prompt 调优候选；`--tune/--tune-output` 输出内容轻量报告，固定 `auto_apply=false`，provider/runtime 故障不会伪造 Prompt 修复建议 |
| v0.96 | Provider 缓存能力协商 | `ProviderCapabilities.cache` 声明 none/implicit/explicit、显式断点上限和缓存 usage 字段；统一的 `CacheCapabilities.negotiate()` 将 TOML `prompt_cache = auto/on/off/implicit/explicit` 与真实模型能力解析为 `CacheNegotiation`，再由 adapter 生成 wire。Anthropic 仅走 explicit，OpenAI/DeepSeek 与 Gemini 2.5+ 走 server-managed implicit；不支持的 explicit 请求会显式降级。requested/effective/reason/fallback/支持模式与最大断点贯通 PreparedRequest、ContextUsageSnapshot、`/context`、TUI、JSONL/stream-json/SDK；Gemini `cachedContentTokenCount` 同步归一为 `cache_read` |
| v0.97 | Prompt Parity Contract 与 Benchmark v3 | 冻结 8 个 parity 维度、16 个真实行为 case、每 case 至少 3 trials 的发布门禁；新增 content-light `PromptParityBaseline`、原子保存/加载、hash/字段白名单校验与 fail-closed `--gate`。Live suite 新增最小编辑、权限拒绝、Plan gate、ToolSearch 延迟加载、MCP 注入、失败测试恢复、bounded refactor、Resume 现场刷新、Fork Memory 隔离与第二轮 cache hit 场景 |
| v0.98 | 自适应 Cache Breakpoint Planner | Anthropic 不再固定写死三个断点。新增 content-light `CacheBreakpointCandidate/Plan/Planner`，在最终 wire 的 tools → stable system → messages 顺序上选择稳定锚点、移动 frontier 与均匀历史 checkpoint，严格服从 Provider 最大断点数；Sonnet/Opus 默认 1024、Haiku 2048 tokens 阈值，`cache_min_tokens` 可覆盖，低于阈值不写 `cache_control`。plan 的选中/跳过原因、token、stability 与 digest 贯通 PreparedRequest、ContextUsageSnapshot、`/context cache`、TUI、JSONL/stream-json/SDK |
| v0.99 | ToolPromptSpec 统一工具契约 | 新增 `ToolPromptSpec`，把 Provider description/Schema、工具指令、when/exclusion、示例、别名、来源/版本和 Playbook 元数据收敛为单一来源。`Tool` 保持执行职责，`ToolDescriptor` 保持安全/路由职责；Registry、TurnToolPool、ToolSearch、PromptAssembler 和 L5 Playbook 共用同一 spec。Provider wire 只接收 name/description/parameters，`wire_hash/content_hash` 贯通 PromptPlan/ProviderMessages/`/context`，所有观测字段不含工具正文。旧工具空 description 使用稳定占位，兼容历史注册 |
| v1.00 | Prompt Conflict Analyzer | 最终 Prompt 排序后做无模型、content-light 的跨层冲突分析：识别直接否定、显式对立值和条件冲突，固定 Managed → Mode Contract → Base → Directory → Project → User → Style → Working → Reminder 权威顺序；同权冲突不按 ID 裁决。中英文分句/CJK bigram、代码围栏过滤、主题候选索引与有界缓存把基础 Prompt 分析降到约 2ms。报告贯通 PromptPlan、Provider channel、Snapshot 与 `/context conflicts|why`，只保存来源、分类、winner、置信度和 hash，不保存指令正文，也不改变 Provider `full_hash` |
| v1.01 | Prompt Release 与 Profile 校准 | 新增不可变 `PromptRelease` 与内容无关 release hash；`ModelProfileCalibration` 绑定真实 live baseline 的 release/profile/contract/suite/provider/model，支持精确与最长 pattern scope。运行时 `calibrated/uncalibrated/stale` 可见，calibration 不进入指令通道；主 Agent、`/model`、子代理、`/context why` 与 Benchmark report 共用身份链。Baseline schema v2 嵌入 calibration，gate 对 Release/Profile/contract/frozen-suite/state 漂移 fail-closed |
| v1.02 | 最终完全对齐验收 | 新增 `benchmarks/acceptance.py`：冻结 17 个 requirement/12 个 capability area，以 static/runtime/offline/live 四层证据执行 18 个 required checks。最终 `accepted` 要求 9 个离线生产面、运行时探针、冻结 Prompt Release/Parity contract/suite 和 fresh 的真实 provider report/gate 同时通过；Live 校验独立核对 16-case × 至少 3-trial、stability/pass@k、runtime error、24h 时效和 gate-report 时序，不信任裸 `passed=true`。缺 Live Gate 只能 `pending_live`（退出 2），任何 required 失败为 `failed`（退出 1）；最终 JSON 再执行 content-light 自检与异常/凭据内容清除，统一入口为 `--live --gate <baseline> --final-acceptance` |
| v1.03 | Prompt Authoring Contract | 新增默认关闭的 L6 `prompt_authoring:contract`，通过 `[prompt.authoring].enabled` 显式启用并共享给主/子 Agent。契约自身覆盖目标分析、Instruction/Evidence 分权、stable/dynamic、冲突检测、工具权限与可验证性；`PromptAuthoringValidator` 复用生产 Conflict Analyzer，检查无效来源角色、动态缓存污染、read-only 写工具、通配符/自动批准边界、权限提升文案和 success/regression/security 机器指标，报告只含 ID/计数/hash。`/context authoring` 提供内容轻量状态入口，不自动应用 Prompt 或扩大运行时权限 |
| v1.04 | Prompt Engineering Lab | 在 `[prompt.authoring].enabled` 下注册只读 core 工具 `prompt_lab(action=inspect|generate|compare|evaluate)`，复用 Fragment Registry、PromptConflictAnalyzer 和 PromptAuthoringValidator；inspect/compare/evaluate 内容轻量，generate 只返回 `applied=false` 候选。主/子 Agent 的 ToolContext 分别绑定自己的 Registry，工具不写文件、不修改 Registry、不扩大权限，Plan/read-only 模式可用 |
| v1.05 | Candidate Prompt Schema | PromptAuthoringContract 升为 v2，新增封闭 `PromptCandidate`/`PROMPT_CANDIDATE_SCHEMA`：八个字段必填，risk 仅 low/medium/high，evaluation cases 固定 success/regression/security，未知字段与超界输入 fail-closed。`prompt_lab.generate` 输出 canonical candidate，evaluate 接受新 schema 并兼容旧 authoring 输入；已存在目标自动继承 layer/scope/stability/cacheability/authority，新目标使用 project/L6/session。候选只生成确定性 hash 与 content-light metadata，不写文件、不修改 Registry、不自动应用 |
| v1.06 | 隔离候选评估 | PromptAuthoringContract 升为 v3；canonical candidate 先通过 baseline/candidate temporary `PromptPlan`，执行当前失败、稳定回归、Instruction 优先级、Evidence 注入、permission/Sandbox、cache stability 六项固定门禁。通过只允许人工 review，Prompt Release 仍需独立人工决策；评估复用真实 ToolContext/Sandbox L5 合同，报告 content-light，且始终 `production_modified=false`、`applied=false`。旧 authoring evaluate 明确不能绕过 Release 资格 |
| v1.07 | Connected Prompt Release Pipeline | 新增 `agent/prompt_release_pipeline.py`，串联 PromptAuthoring Skill、Candidate、temporary PromptPlan 六项隔离、offline/live Benchmark、Parity、Conflict Analyzer、Release/Calibration proposal 与人工审批。`prompt_lab` 增加 `propose/status` 且无 approve action；新增 `/prompt-release list|status|approve|reject`。提案、审批、calibration 与 release artifact 全部 content-light、原子写入 `.krelcode/prompt-releases/`，绑定 candidate/proposal/release/profile/contract/suite/calibration hash；缺证据停在 pending，身份漂移标记 stale，批准后仍 `production_modified=false/applied=false`。内置 `prompt-authoring` Skill 规定完整工程流程与 Agent 自批准禁令 |
| v1.08 | 子代理委派 Meta Prompt | 新增 canonical `subagent/prompt.py`，统一父 Agent 的 `spawn_agent.task` 写作契约：心智模型注入、事实/假设分离、禁止猜测、正例/反例、范围边界、交付物与验证；接入 ToolPromptSpec 与 subagents playbook。`fork_context=true` 追加明确现场搬运指导，并说明 fork 只记录血缘、不复制父完整 history。Meta Prompt 不注入子代理 task 正文，不改变任务 Schema、审批、工具范围或 Sandbox |
| v1.09 | 对抗式 Verification Agent | 新增 canonical `agent/verification.py` 与内置 `verifier` Definition：先建模验证 Agent 会偷懒、自我说服、把绿色命令/编译成功/作者总结当正确性，再用 `RECOGNIZE_YOUR_OWN_RATIONALIZATIONS` 强制补证，并以固定 JSON `OUTPUT FORMAT` 返回 verdict/claims/checks/behavior/rollback。检查矩阵覆盖 backend/API、frontend、iOS/Android、数据库 down/rollback、data/ML、infrastructure、dependency upgrade 与 refactor；refactor 要求原测试保持不变。Verifier 不含 write/edit 工具，用户同名 Definition 可覆盖内置版本 |
| v1.10 | 工具级 Prompt 所有权下沉 | Base Prompt 移除具体工具名、参数、顺序与安全特例，只保留身份、总体原则和工具选择纪律。默认内置、子代理、Skill、LSP、Web、Memory 与 `prompt_lab` 共 39 个已知工具各自拥有 `prompt`、when/exclusion、版本和必要示例；`run_command`、`edit_file`、`ask_user` 等复杂工具把专用偏好、Git 安全、精确匹配、串行写、diff 检查和决策门槛放在自身 Prompt。`ToolPlaybookCatalog` 只附加族元数据，legacy 空 instructions 才回退族 guidance，避免覆盖或重复规则；Provider wire 仍只含 name/description/parameters，过滤/未加载工具不泄漏 Prompt |
| v1.11 | Claude Code 六内置 Agent 兼容 | 新增 `Explore`、`Plan`、`general-purpose`、`claude`、`statusline-setup`、`claude-code-guide`，保留额外 `verifier`。缺省委派默认 `general-purpose`；Explore/Plan 只读、无 MCP、跳过 user/project/directory 规则但保留 Managed；general-purpose/claude 继承全部子代理能力；statusline/guide 使用 sonnet/haiku 逻辑档位。`[agent_models]` 显式覆盖优先，Anthropic 自动映射档位，DeepSeek/OpenAI-compatible 无覆盖时继承父模型；新增 `/statusline`。用户/项目同名 Definition 可覆盖内置 Agent，所有路径继续受审批、Sandbox 和防递归边界约束 |

| v1.12 | Tool Prompt lint 与 examples 补齐 | 新增旁路 `tools/prompt_lint.py` 与 `/prompts lint [tool]`：校验 ToolPromptSpec name、object Schema、required 引用、instructions、when/exclusion、example 未知字段与 JSON Schema 约束、重复 input/title、空 description、guidance 冲突、长度、凭据形状和可选参数覆盖。内置/Skill/Subagent 缺 example 为 error，MCP/Plugin 为 warning；报告只含字段路径、稳定 `TPLxxx` code、severity/count 与 content-light 标志，不扫描磁盘或回显正文。39 个已知工具全部补齐至少一个 schema-valid example，Step 120 契约测试同步要求 examples；全量 1626 通过 + 2 个本地端口集成 skip |
| v1.13 | 标准命令模板与 Reminder Catalog | 新增 `CommandTemplate` 统一 Builtin/Custom/Skill/Plugin/MCP Prompt Command 的 source/trust、参数模式、envelope、工具 allowlist、reminder 声明与 stable hash；REPL、headless、stream-json 将 command contract 传入真实 turn。新增 `ReminderCatalog`，以显式 ID/trigger 选择 l12 turn reminder，支持稳定排序、ID/content 去重、单条/turn 预算与 content-light exclusion；AgentLoop 每 turn 只选择一次，PromptAssembler 不将其放入 stable cache prefix。新增 `/commands [show <name>]` 与 `/context reminders`；全量 1633 通过 + 2 个本地端口集成 skip |

## 3. 文件导览（能力 → 位置）

| 想找什么 | 去哪里 |
|----------|--------|
| 消息/事件/错误类型 | `core/messages.py` `core/events.py` `core/errors.py` |
| Provider 抽象与事件流契约 | `llm/base.py`（TextDelta/ThinkingDelta/StreamFinished） |
| 显式 system block / cache boundary 契约 | `llm/base.py`（SystemPromptBlock/SystemPromptBlocks） |
| 自适应 explicit cache 断点规划 | `llm/cache_planner.py`（adaptive-prefix-v1） |
| SSE/传输桥 | `llm/transport.py`；MCP 侧 `mcp/sseparse.py` `mcp/http_connector.py` |
| 各 wire format 翻译 | `llm/providers/{openai,anthropic,gemini}.py` |
| 重试 | `llm/retry.py`（RetryingProvider 装饰器） |
| 主循环/steering/取消修复 | `agent/loop.py` |
| system prompt 来源/启停/cache 边界 | `agent/fragments.py` |
| 唯一 PromptPlan/ProviderMessages 装配 | `agent/prompt_assembler.py` |
| 动态上下文附件/revision/增量 | `agent/attachments.py` |
| 压缩边界/预览与两道防线 | `agent/compression.py`（LLM 摘要）· `agent/tool_microcompact.py`（分级工具微压缩） |
| 会话任务现场 | `agent/working_context.py`（goal/plan/files/commands/errors） |
| token 估算/价目/窗口 | `agent/tokens.py` `agent/pricing.py` |
| 请求级 Context 可观测性 | `core/context_usage.py` `agent/context_usage.py` |
| 候选 Prompt 隔离评估 | `agent/prompt_isolation.py`（temporary PromptPlan + 六项门禁） |
| 可插拔上下文来源/确定性装配 | `agent/context_source.py` `agent/context_assembler.py` |
| 扩展来源目录/配置分层 | `extensions.py` `config.py` |
| Plugin v2 六类组件/生命周期 | `plugins/{manifest,components,manager,loader}.py` |
| 工作区上下文注入 | `agent/context.py`（目录树/git/分层 AGENTS.md） |
| 工具端口、PromptSpec 与注册表 | `tools/base.py` `tools/prompt.py` `tools/registry.py`；具体工具 `tools/builtin/`，交互工具 `tools/extension/` |
| 动态工具池 / ToolSearch | `agent/tool_pool.py` `tools/builtin/tool_search.py` |
| 命令风险分级/审批/写保护 | `safety/commands.py` `safety/policy.py` `safety/rules.py` |
| 运行时权限/Plan 审批状态机 | `safety/runtime.py` |
| MCP 客户端/传输/OAuth/热重载 | `mcp/client.py` `mcp/{stdio,sse}_transport.py` `mcp/streamable.py` `mcp/oauth.py` `mcp/supervisor.py` |
| 子代理 | `subagent/manager.py`（生命周期/控制）`subagent/{definition,feed,control,sidechain}.py` `subagent/tools.py` |
| 后台 Shell 进程/实时输出/日志 | `jobs/shell.py`；模型工具 `tools/builtin/job_tools.py` |
| 会话录制/恢复/分支/导出/检查点/回退 | `session/{recorder,manager,lifecycle,restore,export,checkpoint,rewind}.py` |
| 插件发现/隔离加载/热重载 | `plugins/{manifest,loader,manager}.py` |
| Custom Markdown Command | `session/commands/custom.py` `session/commands/sources.py` |
| 流式渲染/审批/用户输入/@引用 | `ui/render.py` `ui/approval.py` `session/interactions.py` `ui/atfiles.py` `ui/repl.py` |
| 终端 Panel Dock / 键鼠导航 / Shell+Memory 快捷模式 | `ui/tui/{model,layout,controller,mouse,panels}.py` `session/memory.py` `agent/loop.py:execute_direct_tool` |
| Memory 模型/作用域/存储与命令 | `session/memory.py` `session/memory_candidates.py` `session/commands/handlers/memory_ops.py` `tools/builtin/memory_search.py` |
| hooks / 用量 / 自动提交 | `hooks.py` `agent/usage.py` `session/autocommit.py` |
| 诊断/脚手架 | `doctor.py` `scaffold.py` |
| 组装根（唯一认识所有具体类的地方） | `app.py` |
| Stream JSON 协议 / 持久运行器 / Python SDK | `session/stream_json.py` `session/stream_json_runner.py` `sdk.py` |
| LSP 客户端与管理器 | `lsp/client.py` `lsp/manager.py` `lsp/tools.py` |
| Output Styles / Status Line | `output_styles.py` `ui/status_line.py` |
| WebFetch / WebSearch | `web/http.py` `web/tools.py` |
| TypeScript SDK | `typescript-sdk/src/index.ts` |
| 全局安装/升级/卸载 | `install.sh` |
| 最终 fail-closed 验收 | `benchmarks/acceptance.py` |

## 4. 测试方法论

每个依赖都被收敛成**可注入的窄接口**，测试因此全程离线：

| 注入点 | 替身 | 覆盖了什么 |
|--------|------|-----------|
| LLMProvider | MockProvider / 无状态 Fake | 主循环、压缩、子代理、steering |
| Transport（LLM HTTP） | FakeTransport（支持响应队列） | 三家 provider 的 SSE/错误/wire 互转 |
| SSEConnector / HTTPConnector | 内存 Fake | MCP SSE 与 Streamable 全协议路径 |
| MCP 服务器 | tests/mcp_stub_server.py（真实子进程） | stdio 握手/工具/资源/提示词/崩溃 |
| input | ScriptedInput / TimedInput | REPL 全命令 + steering 时序 |
| sleep / approver / log / write | 假函数/收集器 | 重试退避、审批链、渲染、hooks |

P2 新增测试使用 fake LSP stdio server、注入 resolver/transport、临时样式目录与
Node 内置测试运行器；LSP/Web 测试不要求本机安装语言服务器或发起真实网络请求。
SSE/Streamable 的真实 http.server 集成用 socket bind 探测自动 skip。

**行为级验收**：`tests/test_acceptance.py` 用与 app 相同的组件装配全栈，
脚本化驱动 4 个典型任务（修 bug+git+安全 / 子代理委派 / 重构改名 /
自我修正+写保护），断言工作区最终状态、审计痕迹与行为不变量。

## 5. 真模型验收（需要 key 与 accepted baseline）

离线套件验证「机制」，真模型验证「行为」。快速 smoke test 可运行：

```bash
export OPENAI_API_KEY=sk-...     # 或 ANTHROPIC_API_KEY / GEMINI_API_KEY
python scripts/real_acceptance.py                        # 7 阶段自动验收
python scripts/real_acceptance.py --provider anthropic --keep
```

最终发布门禁使用 fresh 的 16-case × 3-trial suite：

```bash
PYTHONPATH=src python -m krelcode.benchmarks.prompt --live \
  --gate benchmarks/baselines/PROVIDER-MODEL.json \
  --final-acceptance --json
```

状态与退出码：`accepted=0`、`pending_live=2`、`failed=1`。最终验收会同时运行 9 个离线
生产面、runtime probes、Prompt Release/Parity/Calibration 身份检查和 Live Gate；不能与
`--include-content`、`--no-sdk` 或单 case 混用。

建议再手动过一遍 REPL：流式渲染、`/model` 热切换、steering 插话、
`/stop`、`/undo`、`/compact`、`/cost`、MCP 服务器（如有）。

## 6. 已知边界与限制（诚实清单）

- checkpoint 不追踪 run_command 造成的文件修改（命令内容不可静态分析）
- Windows：进程终止退化为主进程 kill（无进程组）；未在真机验证
- 后台 Job 是进程内管理器：进程重启后不会接管旧 OS 进程；已写日志仍保留，但 list/output/kill 只管理当前进程创建的任务
- 子代理 sidechain 在进程重启后可审计，但不自动接管/复活旧 Task；Definition 的角色提示是用户可信配置，仍需遵守审批、Sandbox 和工具 allowlist
- grep/repo_map 为纯 Python 实现，超大仓库性能弱于 ripgrep/tree-sitter
- 价目表/上下文窗口是内置近似值，会过时（`agent/pricing.py` 一处可改）
- 思考流的展示文本不进对话历史（设计如此）；但 Anthropic 扩展思考的
  thinking 块（含签名/redacted）经 Message.provider_meta 完整回传（Step 34），
  其他 provider 无此要求
- MCP 反向请求支持 sampling/roots/ping（Step 35）；elicitation 等更新的能力未实现
- MCP 交互式 OAuth 使用本机回调端口（默认 `8765`，可配置）；端口被占用时需改用可用端口，token endpoint/client 必须接受对应 redirect URI
- Structured Compact 的模型摘要仍依赖模型能力；失败或 schema 非法时使用 Working Context/history 降级，宁可保留结构化事实也不阻断主循环
- Fragment 手动启停是当前会话运行时状态，重启恢复默认；需要长期策略时应修改来源本身或后续增加持久化 overrides

## P2 验证命令

```bash
PYTHONPATH=src pytest -q tests/test_lsp.py tests/test_output_styles.py \
  tests/test_status_line.py tests/test_web_tools.py
PYTHONPATH=src node --experimental-strip-types --test typescript-sdk/tests/sdk.test.ts
```

TypeScript SDK 的 `npm run typecheck` 需要本机已有 `typescript` 与 `@types/node`；
没有网络依赖时仍可运行上面的 Node type-stripping 测试。

## Step 122 Memory 索引与按需主题验收

```bash
PYTHONPATH=src pytest -q tests/test_step122_memory_index.py \
  tests/test_memory_store.py tests/test_step91_auto_memory.py \
  tests/test_step102_memory_evidence.py tests/test_command_handlers.py
```

预期：

- `MEMORY.md` 只含受限索引，主题正文不在默认 Context 中；
- `memory_search(id=...|topic=...)` 才展开主题与 Why/How；
- feedback/project 缺少 Why/How 时写入失败，候选保持不可接受；
- user 主题文件只出现在 `~/.krelcode/memory/topics/`；
- 专用 L5 Prompt 同时存在 What NOT to save 与 Task/Plan/Memory/Compact 边界。
