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
│   扩展: MCP / 插件 / web_search                  │
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
- 项目上下文自动注入：AGENTS.md、git 分支/diff 状态、目录结构
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
→ 5(循环加固) → 6(安全层) → 7(上下文管理) → 8(交互增强)。
