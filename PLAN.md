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

| 18 | 生产化加固 | token 预算（--token-budget：loop 消费 provider 上报 usage，每次 LLM 调用前检查，超限优雅中止；无 total 时退化为 input+output）· 敏感文件写保护（.git/、.env*、*.pem/*.key、id_rsa*、.npmrc/.netrc、**allow_rules.json 防 LLM 自我提权** → ASK，非交互 fail-closed）· headless JSON 输出（--output-format json：stdout 纯 JSON{ok,reply,usage,tools_used,denied,session}，启动提示走 stderr，CI/脚本可用） | 17 个新测试：预算中止时机（LLM 调用数精确断言）/无预算不受影响/usage 退化求和、保护路径 10 组参数化、策略级拒绝与批准、AllowAll 语义不变、JSON 模式 stdout 纯净性 + 字段断言 | ✅ |

| 19 | Hook 系统 + 安全补洞 | 安全补洞：krelcode.toml 与 .krelcode/ 全目录列入写保护（防 LLM 篡改配置注入钩子/改审批白名单/篡改检查点会话）· HookRunner：TOML [[hooks]] 配置 file_edited/tool_completed/tool_failed/run_finished 四类触发器，模板变量全部 shlex.quote（值来自 LLM 参数，防命令注入），钩子失败/超时只记日志不影响主循环 | 13 个新测试：解析容错、触发映射（失败写工具不触发 file_edited）、路径含空格+注入载荷被 quote（PWNED 不落地）、exit 3 不抛、sleep 30 被 0.5s 超时终止、AgentLoop e2e 钩子链 | ✅ |

| 20 | 韧性与自我修正辅助 | RetryingProvider（LLMProvider 透明装饰器：429/408/409/425/5xx/网络错误指数退避重试，401/400/ConfigError 不重试，**已产出 delta 绝不重试**防重复输出，sleep 可注入，--retries 默认 2）· edit_file 失败辅助：多处匹配报精确行号、未匹配给 difflib 相近行提示（帮 LLM 一轮自我修正） | 12 个新测试：可重试分类/退避序列 [1,2]/精确错误单次尝试/重试耗尽/中途产出不重试/0 关闭/属性透传/on_retry 回调/app 接线；行号与提示断言 | ✅ |

| 21 | 思考模型统一支持 | 统一 ThinkingDelta 事件（llm 层）：OpenAI 兼容 reasoning_content/reasoning（DeepSeek/Qwen/OpenRouter）与 Anthropic thinking 块（thinking_delta，signature_delta 忽略）都翻译到同一事件流；约定思考**永不进入** message.content/history；core LLMThinkingDelta 事件转发；渲染器 dim [thinking] 流（前缀一次、换行压平、任意其他事件收束） | 6 个新测试：两 provider 的 SSE 思考流解析、思考不混入 content、loop 转发与历史洁净、渲染器收束与压平 | ✅ |

| 22 | 交互层增强 | @文件引用（ui/atfiles：词首 @ 触发、email 不误伤、工作区约束、二进制/超限 fail-safe、注入预算）· 会话 Markdown 导出（session/export + /export [路径]，长内容截断）· REPL 可测试化（input_fn 注入点）——全部 /命令首次固化为自动化回归 | 24 个新测试：@展开 9 例（截断/上限/越界/二进制/email）、导出结构与截断、REPL 13 例全命令（help/tools/消息流/异常路径/@注入/history/reset/cost/compact/未启用提示/sessions+resume/export/undo 集成） | ✅ |

| 23 | 子代理增强 | 消息队列（运行中 send → 排队，同 Task 内排空，wait 语义=队列排空）· interrupt 打断（取消→**历史消毒** sanitize_history 防孤儿 tool_calls/连续 user→合并打断指示）· wait_many / wait_agent 多 id（逗号分隔，单 id 输出格式向后兼容）· status_lines 显示队列深度 | 9 个新测试 + 1 个语义更新：消毒级联弹出/干净历史不动、interrupt 后历史精确为 [sys,user(合并),assistant] 且 cancelled 事件发出、打断保留队列、并行 wait_many 时序、多 id 工具分节、单 id 向后兼容、排队后自动续跑（原「运行中拒绝」测试改为排队语义） | ✅ |

| 24 | Gemini provider | 第三种 wire format 验证统一抽象：contents/parts（text/functionCall/functionResponse）、systemInstruction 独立字段、functionCall **无 id** → 合成 gem-N + 转换历史时重建 id→name 映射、连续 tool 结果合并、finishReason 规范化（有调用即 tool_calls / MAX_TOKENS→length / 其余小写透传）、usageMetadata 三键规范化、x-goog-api-key 鉴权、:streamGenerateContent?alt=sse；GOOGLE_API_KEY 兜底；FakeTransport 升级为响应队列 | 14 个新测试：全形态历史转换、请求形态、流式文本+usage、合成 id 多调用、非法 args 标记、finish 映射×2、HTTP 错误、key 缺失/env 兜底、registry 接入、**AgentLoop 全链路 wire roundtrip**（functionCall→get_time→functionResponse 按 name 配对→文本） | ✅ |

| 25 | 上下文管理二期 | 工具结果驱逐（LLM 摘要前的免费第一道防线：只留最近 N 条工具输出，更早的换占位标记，配对结构不动、幂等、短结果跳过；驱逐后仍超预算才 LLM 摘要）· AGENTS.md 分层（全局 ~/.krelcode/AGENTS.md + 项目级，各自限字符，全局在前项目在后）· REPL /context（历史/工具消息数、估算 tokens、预算）与 /context refresh（重采目录树/git/规则并重建 system prompt） | 8 个新测试：驱逐保结构/保最近/幂等/短结果跳过/零 LLM 调用解决预算/驱逐后摘要兜底/标记含重读提示；分层规则全局+项目顺序/仅全局；/context 统计与刷新进 system | ✅ |

| 26 | 主循环 steering 与中止 | AgentLoop：steering_queue（每 step 开始前注入，尾部 user 则合并防连续 user，否则独立追加）+ 取消修复 _repair_after_cancel（sanitize_history 下沉 core 共用；尾部 user 补「[已被用户中止]」保角色交替）· REPL 重构为常驻 reader + 状态机：运行中直接打字=插话、/stop 取消、其他斜杠命令给提示、EOF 停任务退出、未送达插话可见丢弃 · UserSteered 事件（渲染器/recorder） | 11 个新测试 + 1 个语义更新：预填合并/运行中注入位置/空白忽略/LLM 流中取消/工具中取消弹悬挂/取消后续聊协议合法；REPL 级插话注入/停止/运行中命令提示/空闲 stop；子代理打断测试更新为新修复语义 | ✅ |

| 27 | 代码智能工具 | find_refs：ast 级 Python 符号引用查找（[def]/[ref]/[import] 分类，不受字符串/注释同名词干扰，语法损坏文件静默跳过，剪枝+限额）· check_syntax：批量语法检查（单文件/目录，错误带 路径:行:列，不执行代码）——两者零依赖，与文本级 grep 互补 | 10 个新测试：类/方法符号、字符串注释不误报、剪枝/隐藏/损坏文件、子目录与限额、错误定位、干净目录、单文件与非 Python、越界拒绝 | ✅ |

| 28 | CLI 子命令 doctor/init | `krelcode doctor`：Python 版本/provider key（脱敏显示）/默认 provider 解析/配置文件/git 与工作区状态/MCP 命令存在性，✓⚠✗ 三态报告，有 fail 退出码 1（CI 可用）· `krelcode init [--force]`：生成 krelcode.toml（provider/MCP/hooks 注释模板，tomllib 自洽性有测试守护）与 AGENTS.md 规则骨架，不覆盖已存在文件 | 10 个新测试：各检查项 ok/warn/fail 分支、key 脱敏不泄露、非仓库/仓库、不可写路径、MCP 五种配置形态、报告汇总与退出码、CLI init/跳过/force/doctor 集成；真机演示 doctor 输出 4✓/2⚠ | ✅ |

| 29 | 端到端行为验收套件 | tests/test_acceptance.py：用与 app.py 相同的组件装配全栈（default_tools+SandboxPolicy+Recorder+Checkpoint+Usage+AutoCommit+SubagentManager），脚本化 MockProvider 驱动 4 个典型任务，断言工作区最终状态+审计痕迹+行为不变量——①修 bug 全链路（失败回填→修复→PASS→JSONL 审计→用量 5×150→git 提交→undo 恢复）②子代理委派（spawn/wait/子报告进父历史/防递归/事件进父审计流）③重构改名（find_refs→replace_all→grep 零残留→check_syntax→undo 还原两文件）④自我修正+写保护（错误 old_string→相近行提示→修正成功；.env 拒绝→事件+审计） | 4 场景全过；这是无真模型条件下的"整机"行为回归，真模型验收为其补充 | ✅ |

| 30 | 成本感知 | Anthropic prompt caching（默认开：tools 末尾/system/最后一条消息 3 个 ephemeral 断点，str content 自动 block 化，tool_result 列表只标最后一块；prompt_cache=false 可关，TOML [providers.anthropic] 透传）· usage 规范化新增 cache_read/cache_creation 键 · 内置价目表 agent/pricing.py（18 个主流型号 USD/1M，精确→**最长前缀**匹配防 gpt-4o 误吞 mini，未知返回 None 不造假；cache_read 10%/cache_creation 125% 计价）· UsageTracker 带 model 累计成本，/cost 与 -m [usage] 显示估算 $，JSON 输出含 cost_usd | 13 个新测试 + 1 处更新：3 断点形态/tool_result 只标末块/关闭无标记/cache usage 规范化；价目精确与前缀匹配/未知 None/基础与缓存数学/Tracker 累计与摘要/未知模型不算钱；JSON cost_usd 键 | ✅ |

| 31 | 模型感知深化 | 上下文窗口目录 CONTEXT_WINDOWS（18 型号）+ default_budget_for（窗口×75%）→ --context-tokens 未显式指定时按模型自适应（未知模型兜底 100k）· doctor 显示窗口与推导预算 · OpenAI prompt_tokens_details.cached_tokens 规范化为统一 Anthropic 语义（input 扣除缓存部分，防成本双计；total 保留原值） | 6 个新测试：窗口匹配/预算比例/app 三级预算解析（兜底/显式/目录推导 96k）/doctor 显示；OpenAI 缓存 usage 规范化断言 + 成本不双计对比 | ✅ |

| 32 | REPL 运维增强 | /allow 审批规则管理（list/add/remove，AllowRules.add_program/remove_program 持久化到工作区，--no-sandbox 时提示不适用）· /model 运行时热切换（build_model_switcher：支持 "model"（沿用当前 provider）与 "provider/model" 跨切换；重建 provider 含重试包装、替换 loop.llm 与压缩器 llm、同步 usage.model、未显式指定预算时按新模型窗口自适应；缺 key/未知 provider 优雅失败不破坏原 provider；mock 基座要求显式 provider） | 11 个新测试：规则增删幂等/持久化重载/REPL 全流程/无沙箱提示；切换当前信息/mock 裸切拦截/切 openai 联动预算 96k+重试包装/同 provider 沿用/缺 key 优雅失败且原 provider 完好/未知 provider/显式预算不被覆盖/REPL 集成 | ✅ |

| 测试总数：445（离线 443 + 本地端口集成 2，沙箱内自动 skip） |

## 已确认的技术栈（详见 DESIGN.md）
Python 3.12 · **asyncio 全面异步** · 零第三方运行时依赖 · OpenAI tool-calling 内部规范 ·
pi 式分层包 + 轻量事件总线（不做插件微内核） · TOML 配置 + JSONL 会话
