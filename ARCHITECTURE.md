# Coding Agent 架构参考（Krelcode 设计蓝图）

本文总结主流 coding agent（Claude Code、OpenAI Codex CLI、Cursor、Aider、OpenHands、Gemini CLI）
的通用架构与技术栈，作为 Krelcode 逐步实现的设计蓝图。

## 1. 总体架构

所有主流产品都收敛到同一模式：**LLM 主循环 + 工具执行 + 上下文管理**，外加安全层与交互层。

```
┌─────────────────────────────────────────────────┐
│ 交互层   REPL / TUI / IDE / 流式渲染 / 审批弹窗    │
├─────────────────────────────────────────────────┤
│ Agent 核心                                       │
│   · 主循环: LLM → tool_calls → 执行 → 回填 → 再推理 │
│   · 系统提示词 + 项目上下文注入 (AGENTS.md/git状态)  │
│   · 上下文管理: token估算 / 压缩摘要 / 大输出落盘    │
├─────────────────────────────────────────────────┤
│ 工具层                                           │
│   文件: read / write / edit / glob / grep        │
│   执行: run_command (超时/截断/后台进程)           │
│   扩展: MCP / 本地插件 / Skills / web_search     │
├─────────────────────────────────────────────────┤
│ 安全与权限层                                      │
│   沙箱: Seatbelt / Landlock / Docker             │
│   审批: 白名单 / 危险命令确认 / 只读区              │
├─────────────────────────────────────────────────┤
│ 模型层   LLM API 客户端: tool-calling / 流式 / 重试 │
└─────────────────────────────────────────────────┘
```

## 2. Agent 主循环（心脏）

```python
while steps < max_steps:
    response = llm.complete(messages, tools)
    if response.tool_calls:
        results = executor.run(response.tool_calls)   # 可并行
        messages += [assistant_msg, *tool_result_msgs]
    else:
        break          # 纯文本回复 = 任务完成或向用户提问
```

关键点：
- 工具报错时把错误文本作为 tool result 还给 LLM，让它自我修正（错误恢复）
- 最大迭代步数防止失控
- 工具结果过长时截断/落盘，只留摘要进上下文

## 3. 工具层设计要点

| 工具 | 设计要点 |
|------|----------|
| read_file | 输出带行号；大文件截断 |
| write_file | 全量覆盖；新建目录 |
| edit_file | 精确字符串替换 (old→new)，要求唯一匹配；替换失败给出清晰错误 |
| run_command | 超时；stdout/stderr 合并截断；返回 exit code |
| grep / glob | 直接调 ripgrep / fnmatch，比让 LLM 拼 shell 命令更快更稳 |
| MCP | 标准协议接入第三方工具生态 |

## 4. 上下文管理

- token 估算（chars/4 粗估或 tiktoken），接近预算时压缩旧对话为摘要
- 项目上下文自动注入：user/project 分层 AGENTS.md、按需目录规则与安全 @import、git 分支/diff 状态、目录结构
- 分层记忆：user 跨工作区、project 跟随仓库、session 跟随会话；结构化记录经统一边界脱敏并作为动态 Fragment 注入
- 请求级装配：PromptSnapshot、工具目录、Turn Intent、Hooks 经 ContextSource 收集，ContextAssembler 统一排序、隔离异常、计算 cache 边界并生成 provider-bound system message
- Aider 的 repo-map：用 tree-sitter 生成代码符号地图，低成本让模型了解全仓库

## 5. 安全与权限

- 沙箱：macOS Seatbelt (sandbox-exec)、Linux Landlock/seccomp、Docker/microVM
- 权限模式：只读 / workspace-write（可写工作区+tmp，默认禁网）/ 全权限
- 审批流：越权操作弹确认，可持久化为规则（如命令前缀白名单）

## 6. 主流产品技术栈对照

| 产品 | 语言 | 交互 | 沙箱 | 特点 |
|------|------|------|------|------|
| OpenAI Codex CLI | Rust (早期 TS) | TUI | Seatbelt/Landlock | AGENTS.md 约定 |
| Claude Code | TypeScript | CLI | OS沙箱+审批 | 工具精简，MCP 生态 |
| Cursor | TS+Rust | IDE | 云/本地混合 | 代码索引 (embedding) |
| Aider | Python | CLI | 无(靠git) | repo-map, git 深度集成 |
| OpenHands | Python | Web | Docker | 事件流架构 |
| Gemini CLI | TypeScript | CLI | Seatbelt/Docker | 开源 |

结论：TS 或 Python 都够用（瓶颈在 LLM 调用）；工具协议向 OpenAI tool-calling 格式收敛；
沙箱与审批是"玩具"和"生产力工具"的分水岭。

## 7. Krelcode 的分步映射

见 PLAN.md：Step 1(模型层+REPL) → 2(主循环) → 3(文件工具) → 4(Shell工具)
→ 5(循环加固) → 6(安全层) → 7(上下文管理) → 8(交互增强)
→ MCP/Skills/Plugins 生态 → 多会话 metadata/fork/resume/rename/search/native exchange。

## 8. Claude Code REPL 六步对齐

| Claude Code 阶段 | Krelcode 对应实现 |
|------------------|-------------------|
| 1. REPL 组装请求并分流命令 | `InputProcessor` 统一规范化/命令分类/附件/Hook；`CommandRegistry` 分流 COMMAND/UI/CONTROL/PROMPT；`AgentRuntime.start_query` 产生 turn 级事件流 |
| 2. QueryEngine 注入 tools 并发起流式 API | `TurnToolPool` 按权限/运行模式/MCP 健康/Skill allowlist 生成本轮 schema；Provider adapter 统一 OpenAI/Anthropic/Gemini 流式协议 |
| 3. 流式解析 tool_use | Provider 发出 `ToolCallStarted/ArgumentsDelta/Ready`；`StreamingToolExecutor` 在模型流结束前登记已完整调用 |
| 4. 分发并执行工具 | 连续只读工具并发；mutating/未知工具作为 barrier 串行；执行顺序为工具池边界→JSON→Schema→审批→execute |
| 5. 封装 tool_result 回填 | `ToolResult`/富 blocks 按原 tool index 保序转成 provider wire 格式；先提交 assistant(tool_calls)，再追加全部 result |
| 6. REPL 消费事件并继续循环 | 有界 `QueryEvent` 背压、renderer drain/flush、ApprovalBroker 前台审批、Esc/Ctrl-C 中止、Enter steering、Alt+Enter 排队下一条 |

## 13. IDE 扩展层（P1-3）

```text
VS Code Extension ───────────────┐
                                 ├── Editor Bridge / SDK ── stream-json v1
JetBrains Tool Window/Actions ───┘                              │
                                                         Krelcode CLI
                                                   ┌───────────┴───────────┐
                                                   │ AgentRuntime + Tools   │
                                                   │ MCP / Sandbox / Memory │
                                                   │ Prompt / Compact       │
                                                   └───────────────────────┘
```

VS Code 与 JetBrains 只实现宿主职责：命令/Action、输入 UI、Output/Tool Window、当前
选区与诊断采集、权限确认。二者共享协议和行为契约，不共享 Provider 代码；JetBrains
不依赖 Node，而是直接管理 `krelcode --input-format stream-json` 子进程。这样 IDE
离线、Daemon、REPL、SDK 与 headless 仍走同一条权限和工具执行链。


## 14. 浏览器 / Notebook / 数据库迁移（P1-4）

```text
                     ┌── BrowserRuntime ── static SafeHttpClient
Browser tools ───────┤                    └─ Playwright (optional, explicit)
                     └─ session TTL / element IDs / approval-bound actions

Notebook tools ─────── nbformat 4 validate → targeted cell edit → atomic replace
                       (outputs are never raw; no implicit kernel)

Migration tools ────── bounded file discovery → framework graph → static findings
                       (no DB connection; apply/rollback = run_command + Sandbox)
```

这三种能力都采用 discoverable tool tier，避免扩大每次请求的 core Schema。工具只在对应
`[browser]/[notebook]/[database]` 启用时进入 Registry；`--no-browser`、`--no-notebook`
与 `--no-database` 可以独立关闭。Browser 的 click/fill/screenshot 仍是 mutating，
在 plan/read-only 模式下被 TurnToolPool 排除并在默认模式走审批；Notebook 写工具与
Migration create 同样不进入只读池。领域执行策略由 Task Workflow L9 提供，而不是污染
稳定 L1 Base Prompt。

## 15. 高级 TUI / Diff 审阅（P2-1）

交互层在原有 `PromptWriter + Panel Dock + stream renderer` 上增加一个模态 diff review
surface。它仍是 scrollback 友好的，不切换全屏 alternate screen：

```text
Tool approval / /diff
  └─ DiffReview → DiffPager overlay
       ├─ file/hunk/line 导航
       ├─ bounded + redacted unified diff
       └─ y/n/q → ApprovalBroker / SandboxPolicy
```

`Review` 是默认第五个 Panel Dock 面板，和 `Tasks / Activity / Context / Memory` 共用
稳定布局与缓存快照。写前审阅只读取目标文件和工具参数，不先执行写入；普通 `/diff` 只
采集 Git tracked/staged/安全 untracked 文本。diff 引擎和 Git 采集在 UI 层之外保持纯数据、
可测试、零第三方依赖，实际执行仍完全经过现有权限、审批、Hook 与 Sandbox 链。
