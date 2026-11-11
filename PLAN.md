# Krelcode 路线图：逐步构建一个 Coding Agent

原则：**每一步都可独立运行、可测试**。架构契约见 [DESIGN.md](DESIGN.md)，
主流产品架构对照见 [ARCHITECTURE.md](ARCHITECTURE.md)。

| 阶段 | 架构层 | 交付物 | 验证方式 | 状态 |
|------|--------|--------|----------|------|
| 1 | 模型层+交互层 | LLM 抽象、Mock、REPL（平铺版） | pytest | ✅（并入 Step 2 重构） |
| 1.5 | 统一 Provider 机制 | wire-free core · transport+SSE · OpenAI(真流式+tool_call聚合) / Anthropic(异构格式) 双适配器 · 工厂/TOML/env 配置解析 · 统一 ToolSpec | FakeTransport 全离线测试两适配器互转与流式翻译；50 个测试 | ✅ |
| 2 | 内核+主循环 | core(messages/events) · LLMProvider/Tool/ApprovalPolicy/EventBus 四端口 · AgentLoop tool-call 循环 · 组装根 · get_time 演示工具 | 25 个测试（纯内存断言循环/错误恢复/拒绝/max_steps/事件序列）+ `--mock` 演示 | ✅ |
| 3 | 工具层-文件 | read_file(行号/分段/截断/二进制防呆) / write_file(建目录/覆盖) / edit_file(唯一匹配替换+diff) / 路径越界防护(symlink 也拦) | 20 个新测试：tmp 工作区真实读写改 + write→read→edit 端到端 + 错误回填自我修正 | ✅ |
| 4 | 工具层-Shell | run_command(asyncio subprocess、进程组超时击杀、输出头尾截断、cwd 限定) + grep(正则/剪枝/include) + glob(mtime 新→旧) | 21 个新测试 + 端到端「写函数→跑挂→修复→PASS」工作流 | ✅ |
| 5 | 主循环加固 | asyncio.gather 并行工具执行(结果保序回填) · 卡死检测(同签名连续失败中止) · 上下文注入(目录树快照/git 分支+状态/AGENTS.md) | 14 个新测试：并行时间窗重叠、乱序完成仍保序、失败计数重置/分签名计数、树剪枝+限深+限条目、真实 git 仓库快照；端到端「写函数→跑挂→修复→PASS」已在 Step 4 通过 | ✅ |
| 6 | 安全层 | 命令风险分级(shlex punctuation 切段/DENY·ASK·ALLOW/跨段 curl\|sh 规则/重定向升级) · SandboxPolicy(fail-closed) · allow 规则持久化(.krelcode/allow_rules.json) · 控制台审批 y/n/a + ApprovalRequested 事件 · --auto-approve/--no-sandbox | 58 个新测试：分级 30+ 案例、审批全流程、规则落盘重载、损坏规则 fail-closed、事件断言；终端演示 放行/批准/拒绝 三链路 | ✅ |
| 7 | 上下文管理 | CJK 感知 token 估算(零依赖启发式) · ContextManager 预算压缩(system+LLM摘要+keep_recent，切点不拆 tool_call/result 配对，摘要失败退化占位) · 大工具输出全文落盘留引用 · ContextCompressed 事件 · --context-tokens/--no-compress | 11 个新测试：估算、预算触发/不触发、太短不压缩、孤儿 tool 边界、失败安全、循环集成(事件+token下降)、50K 输出落盘/小输出不落盘 | ✅ |
| 8 | 交互增强 | StreamRenderer 流式渲染(LLMStreamDelta 逐字输出、工具活动打断续流) · edit_file diff 红绿高亮(tty/NO_COLOR 感知) · SessionRecorder JSONL 审计流+历史快照 · --resume/--no-session · /session 命令 | 16 个新测试：渲染器注入 write 断言逐字输出/兜底打印/打断续流、codec roundtrip、快照取最后、损坏行跳过、录制→恢复→续聊集成；终端演示 录制+恢复 全链路 | ✅ |

🎉 基础路线图 8 个阶段全部完成。

## 进阶阶段

| 阶段 | 内容 | 验证方式 | 状态 |
|------|------|----------|------|
| 9 | MCP 客户端：stdio JSON-RPC 2.0（initialize 握手/tools list/call、EOF 崩溃处理、超时）、MCPTool 适配器（远程 inputSchema 直通统一 ToolSpec、server__tool 命名）、TOML [mcp_servers.*] 配置驱动、单服务器失败不阻断 | stub MCP 服务器真实子进程集成测试 ×12：握手/发现/调用/isError/JSON-RPC 错误/kill 崩溃/坏命令/AgentLoop 集成/config 启动 | ✅ |
| 10 | repo_map 工具：Python ast 精确提取（类/函数/方法/签名/嵌套缩进）、JS/TS 正则提取、剪枝+20K 字符预算、行号可直接接 read_file(offset) | 符号/行号/签名/剪枝/预算截断/损坏文件跳过 ×8；对 Krelcode 仓库自身生成地图验证 | ✅ |
| 11 | 多会话管理：/sessions 列表（时间/轮数/消息数/预览）、/resume <n\|latest\|path> 会话内切换（与 --resume 共用 restore_session） | manager/inspect/format/目标解析/历史切换 ×7 + 终端演示 | ✅ |

| 12 | MCP 进阶 | MCPTransport 端口拆分（StdioTransport/SSETransport：endpoint 事件、reader 线程→call_soon_threadsafe 回投、connector 可注入）· capabilities 门控 · resources/prompts 四类能力工具 · MCPSupervisor 热重载（配置 diff 增/删/改、崩溃自动重启+工具重注册、/mcp 状态与 reload）· ToolRegistry.unregister_prefix | 34 个新测试：FakeConnector 内存 SSE（握手/urljoin/断流/JSON-RPC 错误）、真实 http.server SSE 集成（沙箱自动 skip，提权 234 全过）、supervisor 增删改/崩溃复活后工具可调用、能力门控、resources/prompts 端到端 | ✅ |

| 13 | 子代理/后台任务 | SubagentManager（spawn/wait/send_input/shutdown，独立 AgentLoop+历史+总线，共享 provider/工作区/审批策略）· 4 个父代理工具 spawn_agent/wait_agent/send_to_agent/list_agents · 防递归（子代理工具集不含 spawn 系）· wait 超时用 shield 不杀任务 · SubagentSpawned/Finished 事件（渲染器+recorder+审计）· REPL /agents | 12 个新测试：生命周期、续聊上下文延续、3 子代理并行时序（<0.7s 跑 3×0.25s）、超时仍运行、shutdown 取消、防递归断言、事件、工具表面、父代理经 tool-call 委派端到端 | ✅ |

| 14 | Streamable HTTP 传输 | MCP 2025-03-26 规范：单端点 POST（响应 JSON/SSE 二态）、Mcp-Session-Id 会话头捕获与携带、404 过期 fail-closed 断连、通知 202、流内服务器请求自动回 -32601、GET 监听流惰性尝试（405 静默容忍）；HTTPConnector 端口可注入；共享 SSEEventParser；config `transport = "sse"` 回退旧规范，url 默认新规范；协议版本升至 2025-03-26 | 8 个新测试：Fake 连接器（会话捕获/Accept 头/SSE 响应模式/202 通知/GET 405/服务器请求自动应答/会话过期断连且不再打网络/transport 选择）+ 真实 http.server 集成（bind 门控）；提权 254 全过 | ✅ |

| 15 | 发布准备 | pyproject 完整元数据 + `[project.scripts]` 控制台入口（pip 安装后 `krelcode` 直接可用）· requires-python >=3.11（tomllib）· `--version` · run_command 跨平台进程终止（POSIX killpg / Windows proc.kill 兜底）· ContextManager force 压缩 + REPL `/compact` | 7 个新测试：元数据/版本一致性/入口可调用/--version/三分支终止逻辑/强制压缩；离线 pip install 进临时 venv 实跑 `krelcode --version` 与 REPL 验证 | ✅ |

| 16 | 检查点与回滚 | CheckpointManager（事件总线旁路订阅者，内核/工具层零改动）：RunStarted 开组 / write_file·edit_file 首次修改前快照 / RunFinished 关组（空组丢弃）；快照落盘 .krelcode/checkpoints/（meta.json+字节备份）；undo_last LIFO 恢复（改过的还原、新建的删除）；跨重启重建组栈；REPL /undo、/undo list；--no-checkpoint | 10 个新测试：修改恢复/新建删除/同组保最早版本/空组丢弃/越界路径忽略/LIFO 多轮/跨重启/损坏组跳过/事件驱动/AgentLoop 端到端（edit+write 一轮 undo 全恢复） | ✅ |

| 17 | 用量统计与 git 自动提交 | UsageTracker（LLMResponseReceived.usage 累计，本轮+会话双账本，/cost、-m 模式摘要）· AutoCommitter（Aider 风格：RunFinished 有变更即 add -A + commit "krelcode: <输入预览>"；.krelcode/ 自动进 .git/info/exclude 仅本地生效；非仓库/无变更/失败全静默）· --auto-commit | 8 个新测试：turn 清零/缺失 usage 键容错/MockProvider 集成；真 git 仓库 e2e（写入触发提交、log 含 label、工作区变干净、.krelcode 不进版本库、幂等 exclude、无变更不提交） | ✅ |

| 测试总数：279（离线 277 + 本地端口集成 2，沙箱内自动 skip） |

## 已确认的技术栈（详见 DESIGN.md）
Python 3.12 · **asyncio 全面异步** · 零第三方运行时依赖 · OpenAI tool-calling 内部规范 ·
pi 式分层包 + 轻量事件总线（不做插件微内核） · TOML 配置 + JSONL 会话
