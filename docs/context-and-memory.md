# Context、Memory 与 Compact

本文说明 Krelcode 的请求上下文、分层记忆、Structured Compact、终端可观测性，以及
REPL / headless / stream-json / Python SDK 的共同语义。配置项的权威速查仍以
[`CONFIG.md`](../CONFIG.md) 为准。

## 1. 设计边界

Krelcode 把“本轮模型实际看到的上下文”拆成四个可独立检查的部分：

1. **Workspace Context**：规则、Git 状态、目录树等启动时快照；可用
   `/context refresh` 重新采集。
2. **Working Context**：当前目标、计划、读写的文件、命令、错误和决策，只属于当前
   会话任务现场，不进入长期记忆。
3. **Evidence**：Memory、Web、MCP、Tool Result 和 Retrieved Document 等观察材料。
   Memory 经作用域管理、脱敏和审核后仍只作为不可信 Evidence 注入，不升级成
   Instruction。
4. **History / Tool Results**：对话历史与工具结果；超预算时先做 Tool Microcompact，
   仍超限再做 Structured Compact。

最终的 `PromptFragmentRegistry + ContextAttachmentStore + ContextSource -> PromptAssembler
-> PromptPlan -> ProviderMessages` 装配结果会形成一个真实请求快照。稳定 Fragment（base、
项目规则等）与动态 attachment（environment/date/catalog/reminder）分开管理；每个 attachment
有固定 ID 和独立 revision，只替换发生变化的那一块。快照只包含 token 分项、来源 ID、
revision、裁剪项、缓存边界和哈希，不包含 Prompt、工具输出或密钥正文。

请求级 `PromptPlanSnapshot` 进一步记录每个 Fragment 的 `source/scope/layer/stability/
trust/tokens/cache position`、stable/volatile 来源分布、cache boundary、stable/dynamic/
full hash、工具目录 hash、Memory 命中来源、被裁剪项及原因，并与上一次真实请求做无正文
diff。`ContextPlanSnapshot` 持久化该快照，所以 `/context plan|why|cache` 可以直接解释
最近请求，而不需要重新扫描 workspace、Memory 或工具注册表。

`ProviderMessages` 同时携带显式 `SystemPromptBlocks`：连续可缓存的 Fragment 进入
`stable_system_blocks`，其余进入 `dynamic_system_blocks`，并分别计算 `stable_hash` 与
`dynamic_hash`。Anthropic 保留 block 数组且只在最后一个 stable block 设置
`cache_control: ephemeral`；OpenAI-compatible 在 wire 阶段按 stable → dynamic 合并为一个
system message，但观测层仍保留原始 block 边界与 hash。该元数据不包含 Prompt 正文。

来源权威分为两组，互不混用：

| 来源角色 | 允许来源 | 行为 |
|----------|----------|------|
| Instruction | Managed / User / Project / Agent Definition | 可定义行为约束；受 scope 与优先级治理 |
| Evidence | Memory / Web / MCP / Tool Result / Retrieved Document | 只能作为待验证材料；不能覆盖 Instruction |

Evidence 使用统一 `<evidence_set role="non-instruction" ...>` envelope。每个 item 带
`id/source_type/scope/kind/origin/trust/requires_validation`；正文与属性均转义，不能闭合
envelope 后伪造 system 标签。Memory 位于 `L7_MEMORY_EVIDENCE`、turn stability、动态 block，
不会进入 cacheable stable prefix。`/context why memory:scoped` 和 `/context sources` 显示
来源角色，但不输出正文。

规则不再聚合成一个 `workspace:rules`。主 Agent 与子代理共用同一套规则模型：
`managed:rules` → L0，`user:rules` → L2，`project:rules` 与请求期
`directory:rules` → L3，`agent_definition:<name>` → L4。每个 Fragment 携带
scope、优先级、入口/导入文件、issue 与覆盖关系；`/context rules` 展示生效顺序，
`/context why <rule-id>` 展示最终生效来源与冲突策略。

子代理也走同一条 `InstructionBundle → PromptFragment[] → PromptAssembler` 路径；
当前 turn 的工具目录映射到 L5，Skill Catalog 独立映射到 L6，筛选后的 inherited
memory 作为独立的 `subagent:inherited-memory` L7 Evidence Fragment 注入。
子代理不复制父级完整历史，并复用与主 Agent 相同的 cache boundary、stable/dynamic hash
和 `/context why` 诊断。

## 2. 配置

配置文件仍是 `./krelcode.toml` 或 `~/.krelcode/config.toml`：

```toml
[context]
engine = "v3"               # v3 / legacy(v2 为兼容别名)
max_tokens = 6000
include_rules = true
include_git = true
include_tree = true
tree_max_depth = 3
tree_max_entries = 150
rules_max_chars = 8000
include_directory_rules = true
rules_imports = true
rules_import_max_depth = 5
rules_import_max_files = 32

[memory]
mode = "suggest"            # off / suggest / auto
suggest_threshold = 0.55
auto_threshold = 0.80
```

- `engine=v3`：默认装配路径；非 system 来源都带 `trust/origin`，Memory、Working Context
  与 Hook runtime context 只作为 `data-not-instructions` 数据注入。`legacy`（别名 `v2`）仅保留
  一个发布周期用于故障回退，未知值会安全回落到 v3。
- `mode=off`：不提取 Auto Memory 候选。
- `mode=suggest`：候选写入本地审核队列，不自动进入长期记忆；这是默认值。
- `mode=auto`：置信度达到 `auto_threshold` 且不存在冲突时才写入 `project/knowledge`。
- `suggest_threshold` 控制候选是否值得进入审核队列；`auto_threshold` 只对 `auto`
  模式生效，不能绕过冲突检查和脱敏。

启动参数 `--no-context` 可跳过 workspace 初始快照；`--context-tokens` 和
`--no-compress` 控制请求预算与自动 compact。显式 CLI 值优先于 TOML。

## 3. `/context` 命令

```text
/context
/context plan
/context why <fragment-id|source|memory-id|layer-id|token-source|omitted-id>
/context sources
/context attachments
/context cache
/context refresh
```

### `/context`

展示最近一次真实 provider 请求：

- 输入 token、预算、使用率、模型窗口与输出预留；
- `system/workspace/memory/history/runtime/tools/overhead` 分项；
- stable/volatile token 与 cache boundary；
- stable 与 dynamic system block 数、`cache_control` 边界；
- stable/dynamic/full hash，以及相对上一请求是否变化；
- 贡献 token 最多的来源；
- 实际注入的记忆条数、作用域和 token；
- 被预算或范围过滤掉的来源；
- 当请求需要压缩时，compact 的高/低水位。

如果没有真实请求快照，命令只报告历史估算值，不会为了展示而触发磁盘扫描。

### `/context plan`

展示最近一次 `PromptAssembler` / `ContextAssembly` 的机器可检查计划：

- 每个 Fragment 的 ID、source、scope、layer、stability、trust、token 和 stable/volatile 位置；
- stable/volatile 的 Fragment 数与 token，以及按 source 的分布；
- cache boundary、stable/dynamic/full hash、工具目录 hash 与工具数；
- 被 trust/scope 过滤、单文件范围过滤、空 attachment 或预算裁剪的项目及原因；
- Memory 命中条目的 ID、source、scope、kind、origin 和 token；
- 与上一真实请求相比新增/删除/变化/裁剪的 Fragment、hash 变化标志和 token 差值。

Fragment、layer、source 和 category 列表都有数量上限；超大 Context 会显示省略数量，并可用
`/context why <id>` 定向检查。所有输出均不含 Fragment、工具 Schema、history 或 Memory 正文。

### `/context why <id>`

接受以下目标：

- Prompt Fragment ID 或 source 名；
- Memory entry ID；
- Context layer ID，例如 `source:prompt`；
- source 名，例如 `prompt`、`tool_directory`；
- token source，例如 `history`；
- omitted item，例如 `workspace:tree`。

输出会解释 scope、layer、stability、trust、stable/volatile 位置、content hash、cache/compact
属性、token attribution、Memory 来源、是否出现在请求计划中以及裁剪原因；不会输出对应内容正文。

### `/context sources` 与 `/context cache`

`sources` 查看 layer 顺序、来源 token、cache 属性、source issue 和 dropped item。
`cache` 查看 stable/dynamic block 数、stable/dynamic/full hash、工具目录 hash、与上一请求
相比的 hash/flag/token 差值、`cache_control` 字符边界、显式缓存段数量，以及各 segment 的
token/字符/哈希。Anthropic 的断点位于最后一个 stable block；OpenAI-compatible 只合并 wire
system message，不会丢失这组边界元数据。`/context cache` 从最近 `PromptPlan` 列出包含
attachment 在内的 stable/volatile Fragment；`/context attachments` 则列出动态附件的 kind、
revision、generation、layer 与 token，均不输出正文。

### `/context refresh`

重新读取目录树、Git 状态和规则文件。规则变化才更新稳定 Fragment；Git/tree 变化只替换
`environment_snapshot/environment_delta` attachment，Skill/Agent/MCP catalog 也各自使用独立
attachment。它不会重写持久 history；下一真实模型请求会使用新快照。TUI 刷新本身不会触发该扫描。

## 4. TUI Context 面板

`Ctrl-O` 聚焦面板，切到 `Context`。该面板只读取 `SessionViewState` 与已缓存摘要：

- provider 请求的输入/预算、wire 保真度与模型窗口；
- system/workspace/runtime/history/tools/overhead token；
- stable/volatile token、stable/dynamic block 数、cache 模式和 stable hash 是否变化；
- Context source/fragment/tool schema 数量；
- Memory 总条数、本请求命中条数、注入 token；
- 被裁剪来源的预览；
- Tool Microcompact 次数与节省 token；
- Structured Compact generation、来源、节省 token、checkpoint 与水位。

高频 panel redraw 不得重新扫描 Memory 或工作区文件。Memory 面板使用初始化、Memory 写入
或 `/memory` 命令后的缓存快照；真实请求事件只更新轻量计数。

## 5. `/memory` 命令

```text
/memory
/memory list [user|project|session]
/memory scopes
/memory show <id>
/memory add <text> [--scope user|project|session]
/memory add <user|project|session> <text>       # 旧语法仍兼容
/memory search <query> [--scope <scope>]
/memory pin <id>
/memory unpin <id>
/memory forget <id>
/memory remove <id>                             # forget 的兼容别名
/memory mode [off|suggest|auto]
/memory review [pending|accepted|rejected|superseded|all]
/memory review accept|reject <id>
/memory migrate [user|project|session]
```

`add` 默认写入 `project`。ID 可填写完整值或唯一后缀；`show`、`pin`、`unpin`、
`forget` 和 `remove` 都支持唯一后缀。`search` 只检索已审核的 MemoryEntry，不搜索待审核
候选。

`scopes` 显示每层条数、存储路径、Auto Memory 模式、待审核数量和候选队列路径。

## 6. Memory 数据模型与作用域

每条记忆结构固定为：

```json
{
  "id": "mem-...",
  "text": "项目测试命令是 pytest -q",
  "scope": "project",
  "kind": "knowledge",
  "source": "manual",
  "tags": ["tests"],
  "created_at": "2026-11-16T12:00:00+08:00",
  "updated_at": "2026-11-16T12:00:00+08:00",
  "metadata": {},
  "pinned": false
}
```

`kind` 支持 `note`、`preference`、`fact`、`workflow`、`knowledge`。`knowledge` 只表示
经过模型提取或人工审核的可复用知识，不表示高信任指令。

Memory 在 provider 请求中固定映射为 `EvidenceSource.MEMORY`，原始记录 ID、scope、kind、
origin、pinned 状态保留为机器可读元数据。模型生成内容仍是 `untrusted` 且
`requires_validation=true`；配置或写入模式不能把 Memory 提升为 Instruction。

作用域从宽到窄：

| Scope | 路径 | 生命周期 |
|-------|------|----------|
| `user` | `~/.krelcode/memory/entries.jsonl` | 跨工作区、跟随本机用户 |
| `project` | `<workspace>/.krelcode/memory/entries.jsonl` | 当前项目 |
| `session` | `<workspace>/.krelcode/memory/sessions/<session-id>.jsonl` | 当前会话；fork 可复制独立副本 |

每层独立执行 2,000 字符/条、200 条和 tag/metadata 限制。检索优先 pinned，再按查询相关度
和更新时间排序；实际注入仍受条数与总字符预算限制。

### Auto Memory 候选

候选保存在 `<workspace>/.krelcode/memory/candidates.jsonl`，不跨项目共享。候选触发来源
包括：

- 用户明确说“记住”；
- 用户纠正；
- 同一测试命令成功复用；
- 稳定架构模式被重复确认；
- 任务完成后提取的高价值事实。

候选写入前会脱敏、按规范化文本摘要去重、与现有记忆做保守冲突检测并记录证据摘要。
`/memory review accept <id>` 将其写入 `project/knowledge`；`reject` 标记拒绝且不会自动
写入。`off` 不生成新候选，但已有候选仍可审核。

## 7. `/compact`

```text
/compact preview
/compact preview --auto
/compact
/compact <focus>
```

- `preview`：展示 head/compact/keep 边界、高/低水位、预计节省、工具微压缩、待摘要消息
  和来源节省量，不修改 history。
- `preview --auto`：只有当前请求已超预算时才给出一致预览。
- `/compact`：立即执行 Tool Microcompact + Structured Compact v2。
- `/compact <focus>`：将自然语言重点写入摘要 prompt，例如
  `/compact 保留 API 设计、失败测试和下一步`。

自动 compact 只在安全 turn 边界切割，并保护当前 turn、最近工具、错误、写入/编辑确认、
plan 与 ask_user 结果。compact 摘要使用机器可校验的八字段契约：

```json
{
  "goal": "",
  "completed": [],
  "decisions": [],
  "files": [],
  "tests": [],
  "open_issues": [],
  "next_steps": [],
  "critical_constraints": []
}
```

模型不得增加其他字段；source/generation/checkpoint 等审计信息只保存在内部，不会混入
下一轮摘要请求。连续 compact 会按稳定 key 合并旧摘要，摘要失败时从 Working Context
与保留历史生成降级摘要。compact checkpoint 同时保存精确 Working Context 和任务文件
SHA-256；`/rewind` 恢复后若文件已变化或缺失，会移除陈旧文件事实并记录 open issue。

## 8. 存储、迁移与备份

工作区相关文件：

```text
.krelcode/
├── memory/
│   ├── entries.jsonl
│   ├── candidates.jsonl
│   └── sessions/<session-id>.jsonl
├── context-checkpoints/
└── compact-checkpoints/
```

Memory 使用逐行 JSON，采用临时文件加原子替换；读取时以文件路径决定作用域。旧 project
记录可以缺少 `scope/kind/source/tags/updated_at/metadata/pinned` 字段，读取时按
`project/note/manual` 兼容。

### `/memory migrate [scope]`

默认迁移 project 文件，也可指定 `user` 或 `session`。迁移过程：

1. 逐行解析 JSON，跳过损坏或非对象行；
2. 补全当前 schema 并重新脱敏；
3. 按规范化文本摘要去重；
4. 合并重复项的 tag/metadata，并保留 pinned 状态；
5. 仅在规范化内容确实变化时创建
   `entries.jsonl.bak-YYYYMMDD-HHMMSS[-N]`；
6. 原子重写并输出 `scanned/kept/migrated/duplicates/skipped/path/backup`。

迁移是幂等的：第二次执行应报告“无需更新”，且不会生成新备份。恢复备份时先退出会写该
作用域的 Krelcode 进程，再用备份原子替换对应 `entries.jsonl`。

Compact checkpoint 保存 compact 前完整、脱敏的 history 与 Structured Compact 摘要，
供 `/rewind` 恢复；`context-checkpoint` 则保存 resume/fork/rewind 所需的任务现场引用。

## 9. 脱敏与安全

Context v3 把来源和信任级别分开记录：system prompt 与项目规则为 trusted；Memory、
Working Context 和 Hook runtime context 为 untrusted knowledge，只能进入
`role="data-not-instructions"` envelope。XML-like 内容在封装前转义，不能提前闭合标签或
伪装成更高优先级指令。`ContextPlan` 持久化 trust 汇总；`/context why` 还会显示
每条 layer 的 source/origin。观测事件只保留来源、ID、token 与 hash，不携带正文。

以下边界统一应用 `SecretRedactor` 和 Memory 自身的明显密钥过滤器：

- Memory 文本、tag、metadata、候选与 evidence 落盘前；
- 工具输出、会话 recorder、TUI、stream-json 和 SDK 读取前；
- compact 摘要与 checkpoint 落盘前；
- stream-json 事件序列化前。

常见 `api_key/token/password/cookie/authorization/private_key`、Bearer、JWT、云厂商 key
与私钥块都会替换为 `***`。事件只携带 ID、scope、token、hash、数量和 digest，不携带记忆
正文。模型生成的 Memory 一律作为不可信 contextual knowledge 注入，不能覆盖 system
prompt、权限、Sandbox 或工具规则。

## 10. Stream JSON 事件协议

新事件同时保留旧式 `type/subtype`，并增加稳定的 `event` 名称，便于 SDK 消费者精确路由：

| `event` | `type/subtype` | 关键字段 |
|---------|----------------|----------|
| `context_assembled` | `context/assembled` | `snapshot` |
| `context_dropped` | `context/dropped` | `items`, `reason` |
| `memory_loaded` | `memory/loaded` | `entry_ids`, `scope_counts`, `tokens`, `source` |
| `memory_candidate` | `memory/candidate` | `candidate_id`, `status`, `trigger`, `confidence`, `conflict_count`, `digest` |
| `memory_written` | `memory/written` | `memory_id`, `action`, `scope`, `kind`, `tokens`, `source` |
| `compact_checkpoint` | `compact/checkpoint` | `checkpoint_id`, `generation`, `before_tokens`, `after_tokens`, `summary_digest`, `strategy`, `trigger`, `source_savings` |

示例：

```json
{"type":"memory","subtype":"loaded","event":"memory_loaded","entry_ids":["mem-1"],"scope_counts":{"project":1},"tokens":18,"source":"scoped_memory","turn_id":"turn-2","sequence":7,"session_id":"..."}
```

`memory_written` 的 `action` 可为 `added/removed/pinned/unpinned/accepted/migrated`。
`memory_candidate` 的 `status` 可为 `pending/accepted/rejected/superseded`。
`compact_checkpoint.strategy` 通常为 `structured_summary`，失败降级信息通过
`ContextCompressed.summary_source` 表达。

JSONL 会话审计记录使用同名扁平类型：

```text
context_assembled context_dropped memory_loaded memory_candidate memory_written
compact_checkpoint context_compressed context_microcompacted
```

这些记录同样不包含正文或密钥。

## 11. REPL、headless、stream-json 与 SDK 一致性

- **REPL**：command registry 直接处理 slash command；自然语言进入完整 AgentLoop。
- **headless text/json**：初始 prompt 是 slash command 时由 `SessionController` 直接执行，
  不创建模型请求；命令要求继续 prompt 时才进入 AgentLoop。
- **stream-json**：user 消息为 slash command 时直接执行并发出 `command/result`，随后仍为
  该请求发出恰好一个 `result`；`run_prompt` 命令会继续普通查询。
- **Python SDK**：通过 stream-json 子进程复用同一命令与事件语义，不维护另一套 Memory、
  Context 或 Compact 实现。

直接命令示例：

```json
{"type":"command","subtype":"result","command":"/memory","handled":true,"output":["[memory] ..."],"run_prompt":false,"turn_id":"command-1","sequence":1,"session_id":"..."}
{"type":"result","subtype":"success","is_error":false,"result":"[memory] ...","turn_id":"command-1","sequence":2,"session_id":"..."}
```

## 12. Resume、Fork、Rewind 与子代理

- `resume` 恢复 history 和最近 `context_checkpoint`，校验文件 hash、刷新 Git/目录/rules，
  重建 Working Context，并重新检索相关 Memory；不沿用旧 Git 状态。
- `fork` 复制对应 checkpoint 引用，而不是共享可变 Memory/session 状态；后续写入互不影响。
- `rewind` 联合恢复 history、compact 前现场、文件 checkpoint、plan/审批/权限状态，并让
  `ContextPlan` 与恢复后的 history 保持一致。
- 子代理继承基础/用户/项目指令与父任务筛选出的 Memory，拥有自己的 Working Context；不继承
  父代理完整 history，sidechain 也不会自动回灌主上下文。

## 13. 排障

| 现象 | 检查 |
|------|------|
| Context 面板没有数据 | 先发起一次模型请求；没有真实请求时只显示历史估算 |
| cache hash 每次变化 | `/context cache` 查看 stable segment；检查规则、工具 schema 或 system prompt 是否被动态改写 |
| dynamic hash 每次变化但 stable hash 不变 | 属于正常动态尾部变化；检查 `/context cache` 的 block 数与边界，确认规则没有进入 dynamic block |
| Anthropic 没有缓存命中 | 确认至少存在 stable block；只有一个 stable block 时 `cache_control` 必须落在该 block，没有 stable block 时不会生成显式断点 |
| token 异常高 | `/context` 看分项，`/context sources` 看 layer，`/context why <id>` 看 attribution |
| Memory 未注入 | `/memory list` 确认作用域，`/memory search` 检查检索，`/context` 看命中数；注入还受条数/字符预算限制 |
| Auto Memory 不写入 | `auto` 模式仍要求达到阈值且无冲突；用 `/memory review` 检查 pending 原因 |
| compact 后信息丢失 | `/rewind <n>` 恢复 compact 前现场；检查 `/compact preview` 的 pending message 边界 |
| TUI 卡顿 | panel redraw 不应触发磁盘扫描；用 `/context` 手动刷新诊断视图 |
