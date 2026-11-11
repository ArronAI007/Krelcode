# Krelcode 构建回顾（BUILD.md）

> 本文回答三个问题：这个项目**长什么样**（统计与导览）、**为什么长成这样**
> （关键决策时间线）、**怎么验收**（离线套件 + 真模型脚本）。
> 使用文档见 [README.md](README.md)，契约细节见 [DESIGN.md](DESIGN.md)，
> 逐步骤路线见 [PLAN.md](PLAN.md)。

## 1. 项目统计（v0.31.0 / Step 32 时点）

| 维度 | 数值 |
|------|------|
| 源码 | 8,083 行（Python，**零第三方运行时依赖**） |
| 测试 | 7,307 行 / 46 个文件 / 445 个用例（离线 443 + 本地端口集成 2） |
| 提交 | 32 个步骤，每步一个语义化 commit，全部含测试 |
| 工具 | 内置 10 + 子代理 4 + MCP 远程动态注册 |
| Provider | OpenAI 兼容 / Anthropic / Gemini / Mock（统一事件流） |

源码按层分布：

```
llm 1351 · mcp 1331 · tools 1211 · app/config/doctor/scaffold/hooks 905
agent 860 · ui 656 · session 625 · subagent 435 · safety 427 · core 276
```

core 只有 276 行——内核极小、能力在边缘，正是当初对标 pi/dsh 想要的形状。

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

## 3. 文件导览（能力 → 位置）

| 想找什么 | 去哪里 |
|----------|--------|
| 消息/事件/错误类型 | `core/messages.py` `core/events.py` `core/errors.py` |
| Provider 抽象与事件流契约 | `llm/base.py`（TextDelta/ThinkingDelta/StreamFinished） |
| SSE/传输桥 | `llm/transport.py`；MCP 侧 `mcp/sseparse.py` `mcp/http_connector.py` |
| 各 wire format 翻译 | `llm/providers/{openai,anthropic,gemini}.py` |
| 重试 | `llm/retry.py`（RetryingProvider 装饰器） |
| 主循环/steering/取消修复 | `agent/loop.py` |
| 压缩两道防线 | `agent/compression.py`（驱逐 + LLM 摘要） |
| token 估算/价目/窗口 | `agent/tokens.py` `agent/pricing.py` |
| 工作区上下文注入 | `agent/context.py`（目录树/git/分层 AGENTS.md） |
| 工具端口与注册表 | `tools/base.py` `tools/registry.py`；具体工具 `tools/builtin/` |
| 命令风险分级/审批/写保护 | `safety/commands.py` `safety/policy.py` `safety/rules.py` |
| MCP 客户端/传输/热重载 | `mcp/client.py` `mcp/{stdio,sse}_transport.py` `mcp/streamable.py` `mcp/supervisor.py` |
| 子代理 | `subagent/manager.py`（队列/打断/wait_many）`subagent/tools.py` |
| 会话录制/恢复/导出/检查点 | `session/{recorder,manager,export,checkpoint}.py` |
| 流式渲染/审批交互/@引用 | `ui/render.py` `ui/approval.py` `ui/atfiles.py` `ui/repl.py` |
| hooks / 用量 / 自动提交 | `hooks.py` `agent/usage.py` `session/autocommit.py` |
| 诊断/脚手架 | `doctor.py` `scaffold.py` |
| 组装根（唯一认识所有具体类的地方） | `app.py` |

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

两个「真环境」测试（SSE/Streamable 的真实 http.server 集成）用 socket
bind 探测自动 skip——沙箱内跳过、提权环境全过（445/445）。

**行为级验收**：`tests/test_acceptance.py` 用与 app 相同的组件装配全栈，
脚本化驱动 4 个典型任务（修 bug+git+安全 / 子代理委派 / 重构改名 /
自我修正+写保护），断言工作区最终状态、审计痕迹与行为不变量。

## 5. 真模型验收（待执行）

离线套件验证「机制」，真模型验证「行为」。配好 key 后运行：

```bash
export OPENAI_API_KEY=sk-...     # 或 ANTHROPIC_API_KEY / GEMINI_API_KEY
python scripts/real_acceptance.py                        # 7 阶段自动验收
python scripts/real_acceptance.py --provider anthropic --keep
```

7 阶段：基础对话 → get_time 工具调用 → 文件写读 → 命令执行 →
上下文注入 → 会话录制恢复 → 用量成本。退出码 = 失败阶段数。

建议再手动过一遍 REPL：流式渲染、`/model` 热切换、steering 插话、
`/stop`、`/undo`、`/compact`、`/cost`、MCP 服务器（如有）。

## 6. 已知边界与限制（诚实清单）

- checkpoint 不追踪 run_command 造成的文件修改（命令内容不可静态分析）
- Windows：进程终止退化为主进程 kill（无进程组）；未在真机验证
- grep/repo_map 为纯 Python 实现，超大仓库性能弱于 ripgrep/tree-sitter
- 价目表/上下文窗口是内置近似值，会过时（`agent/pricing.py` 一处可改）
- thinking 流不进历史：跨轮不保留思考内容（Anthropic 扩展思考的
  thinking 块回传未实现，多轮工具调用场景对该能力有损）
- MCP 未实现 sampling/roots（服务器反向请求一律 -32601）
- 上下文压缩的摘要质量依赖所用模型；小模型摘要可能丢细节（有占位兜底）
