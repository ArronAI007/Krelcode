# Krelcode

Krelcode 是一个面向真实软件工程的本地 Coding Agent。它可以在终端中理解代码库、执行开发任务、调用工具、运行测试、审阅变更，并通过 CLI、Web、SDK 与 IDE 共享同一个 Agent 会话。

Krelcode 强调可操作、可观测和可控：所有重要行为都能检查，高风险操作受权限与 Sandbox 约束，长期上下文可以在压缩、恢复和分支后继续使用。

## 重大更新

当前版本：`v0.81.0`

- **Web UI 与 CLI 同步运行**：交互式执行 `krelcode` 会自动启动本地 Web 控制台，不需要单独命令，CLI 退出时同步关闭。
- **Agent 可观测性**：提供实时事件流、工具轨迹、Context/Cache 检查、运行记录和消融实验开关。
- **上下文与长期记忆**：支持工作区规则、动态上下文、Structured Compact、后台预压缩、Auto Memory、AutoDream、恢复与检查点。
- **完整权限与安全边界**：支持权限模式、路径规则、写前 Diff 审阅、命令审批、敏感信息保护和运行时 Sandbox。
- **可扩展 Agent 生态**：支持 Plugins、Skills、Agent Definitions、Commands、Hooks 和 MCP。
- **持久化后台任务**：后台 Shell Job 和子代理可在 REPL 退出后继续运行，并支持重新连接、暂停、恢复和终止。
- **多端接入**：支持交互式终端、单次执行、JSON、Stream JSON、Python SDK、TypeScript SDK、VS Code 和 JetBrains。
- **多模型 Provider**：支持 OpenAI-compatible、DeepSeek、Anthropic 和 Gemini，并支持运行时模型切换。

## 核心能力

### 代码理解与修改

- 读取、搜索、编辑和创建代码文件。
- 搜索符号引用、生成仓库地图并检查语法。
- 执行测试、构建、格式化和诊断命令。
- 对大型工具输出进行折叠、摘要和按需展开。
- 对明确指定文件的任务限制读取范围，减少无关上下文和 token 消耗。

### 任务执行

- 自动规划多步骤开发任务。
- 并行执行相邻的只读工具调用，串行执行写入操作。
- 在运行中接收补充要求，或将下一条任务加入队列。
- 在达到步数、token 或失败阈值时安全停止。
- 支持 Plan 模式、只读模式、默认审批和自动批准模式。

### 上下文与记忆

- 加载全局、项目、目录级规则和本地项目规则。
- 按任务选择相关文件、Git 状态、仓库结构和工具说明。
- 在长会话中自动压缩上下文，并保留目标、文件、决策、测试和待办事项。
- 支持用户、项目、会话级长期记忆，以及待审核的自动记忆候选。
- 支持 Resume、Fork、Rewind 和 Compact 检查点。

### 安全与审计

- 文件操作限制在工作区内，并阻止符号链接和路径逃逸。
- `.env`、凭据文件、私钥和 Token 文件默认拒绝读取或统一脱敏。
- 写入前展示 Diff，敏感路径需要显式审批。
- 高风险命令默认拒绝，不能通过普通审批绕过。
- macOS 支持 Seatbelt，Linux 支持 Bubblewrap；不可用平台降级为静态策略并明确告警。
- 支持本地审计记录、企业策略和 OIDC 单点登录。

## 安装

推荐使用一键安装器。安装器会自动准备隔离的 Python 环境，无需手动创建或激活虚拟环境。

```bash
curl -fsSL https://raw.githubusercontent.com/ArronAI007/Krelcode/main/install.sh | bash
krelcode --version
```

在源码目录中安装：

```bash
./install.sh
```

升级：

```bash
./install.sh --upgrade
```

卸载：

```bash
curl -fsSL https://raw.githubusercontent.com/ArronAI007/Krelcode/main/install.sh | bash -s -- --uninstall
```

卸载程序不会删除用户配置目录 `~/.krelcode`。

### 环境要求

- Python 3.11 或更高版本。
- 支持 macOS、Linux 和 Windows。
- 使用命令、浏览器、LSP 或语法增强能力时，可能需要额外系统工具。
- 运行时默认不需要第三方 Python 包。

## 快速开始

进入项目目录并初始化配置：

```bash
cd your-project
krelcode init
```

`krelcode init` 会创建项目配置和规则模板，不会覆盖已有文件。

检查环境：

```bash
krelcode doctor
```

启动交互式会话：

```bash
krelcode
```

第一次使用时，建议先完成以下配置：

1. 在配置文件中选择 Provider、模型和 API Key 环境变量。
2. 在 `AGENTS.md` 中记录项目约定。
3. 运行 `krelcode doctor` 检查 API、工作区、Git 和 Sandbox 状态。

## 配置模型

Krelcode 使用分层 TOML 配置，优先级从低到高为：

1. 用户配置 `~/.krelcode/config.toml`
2. 项目配置 `krelcode.toml`
3. 本地覆盖配置 `krelcode.local.toml`
4. 企业托管策略

推荐将 API Key 保存在环境变量中，只在配置文件里引用变量名。

### DeepSeek 示例

```toml
default_provider = "openai"

[providers.openai]
model = "deepseek-flash"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
```

```bash
export DEEPSEEK_API_KEY="your-api-key"
krelcode doctor
krelcode
```

### 其他 Provider

```toml
[providers.anthropic]
model = "claude-sonnet-4-5"
api_key_env = "ANTHROPIC_API_KEY"

[providers.gemini]
model = "gemini-2.5-flash"
api_key_env = "GEMINI_API_KEY"
```

OpenAI-compatible 服务可以使用任意兼容端点：

```toml
[providers.openai]
model = "your-model"
base_url = "https://your-provider.example/v1"
api_key_env = "YOUR_PROVIDER_API_KEY"
```

完整 Provider、权限、MCP、Memory、遥测和企业配置见 [CONFIG.md](CONFIG.md)。

## 使用方式

### 交互式终端

```bash
krelcode
```

交互式模式提供：

- 多行输入、历史记录、反向搜索和文件引用。
- Slash 命令补全与实时命令菜单。
- 流式回答；模型思考和执行过程各占一行，工具状态原地刷新，完整输出按需展开。
- Tasks、Activity、Context、Memory 摘要默认压缩进底部状态栏；`Ctrl-O` 查看详细面板。
- 权限模式快速切换、计划确认、Diff 审阅和任务中断。
- 默认保留终端原生拖拽复制和滚轮滚动。面板、Thinking 行和工具行点击可通过
  `krelcode --tui-mouse` 显式启用；启用后文本选择需按住 `Shift` 拖拽。
- 交互式终端默认使用 Alt Screen 后端：应用自己管理 transcript、
  滚轮/PageUp/PageDown 视口、拖拽选择、系统剪贴板与 Thinking/工具行点击。
  `--ui native` 可回退到终端原生 scrollback；退出 fullscreen 后恢复主屏幕并打印完整会话文档。
- Review 面板只在存在待审批 diff 或 `/diff` 审阅时自动出现，不再占用常态界面。

常用命令：

```text
/help          查看帮助
/tools         查看当前工具
/context       查看上下文与缓存状态
/memory        管理长期记忆
/compact       压缩当前上下文
/permissions   查看或修改权限规则
/plan          生成或管理执行计划
/diff          审阅代码变更
/rewind        回退到历史轮次
/resume        恢复会话
/exit          退出
```

### Web 可观测控制台

交互式运行 `krelcode` 时，Web UI 会自动随 CLI 启动，并在终端输出一次性访问链接。

Web UI 可以提供：

- Agent 实时时间线。
- 工具调用与结果轨迹。
- Context、Cache、Memory 和 Compact 状态。
- 运行记录与历史会话查看。
- 下一轮实验配置与能力开关。

Web UI 只监听本机地址，随 CLI 生命周期启动和关闭。

```bash
krelcode             # 交互式模式自动启动
krelcode --webui     # 强制启动
krelcode --no-webui  # 关闭
```

详细说明见 [docs/agent-observability.md](docs/agent-observability.md)。

### 单次执行

```bash
krelcode -m "解释当前项目的测试结构"
```

输出为机器可读 JSON：

```bash
krelcode -m "修复失败的测试" \
  --output-format json \
  --auto-approve \
  --no-session
```

### 代码评审

```bash
krelcode review
krelcode review --staged
krelcode review --focus "并发安全"
```

评审模式只使用只读工具，不会修改文件。

安全审查：

```bash
krelcode security-review
krelcode security-review --staged --focus "authentication"
krelcode security-review --output-format json --fail-on high
```

### SDK 与自动化

- Python SDK：复用本机 Krelcode CLI 与统一会话协议。
- TypeScript SDK：面向 Node.js 应用和编辑器集成。
- Stream JSON：适合长连接、自定义 UI 和 IDE。
- Headless JSON：适合 CI、脚本和流水线。

相关文档：

- [TypeScript SDK](typescript-sdk/README.md)
- [IDE Integrations](editors/README.md)

### IDE

- VS Code：Ask、Explain Selection、Review Current File、Stop、New Session。
- JetBrains：Tool Window、Explain、Review、Stop 与 Settings。

IDE 只发送用户明确提供的工作区上下文，不会递归读取其他文件。

## 扩展能力

### Plugins

插件可以组合 Tools、Skills、Agents、Commands、Hooks 和 MCP Servers，并提供安装、升级、启停、依赖解析、签名验证和可信 Registry。

### Skills

Skills 使用 `SKILL.md` 描述可复用的工作流、领域知识和工具约束，支持用户级与项目级发现、搜索、热重载和主动调用。

### Agent Definitions

可以为子代理定义独立 Prompt、工具集合、模型、权限模式和 MCP 范围。支持后台运行、实时 Feed、暂停、恢复、终止和 Sidechain 审计。

### Commands 与 Hooks

- 支持内置命令、自定义 Markdown 命令、插件命令、Skill 命令和 MCP Prompt。
- 支持 Command、HTTP 和 LLM Prompt 三类 Hook。
- Hook 可用于输入检查、Tool 前置策略、文件格式化、任务通知和生命周期审计。

### MCP

支持：

- `stdio`
- `SSE`
- `Streamable HTTP`
- Tools、Resources、Resource Templates 和 Prompts
- 分页、能力变化通知、热重载和 OAuth
- Elicitation 与受控反向请求

MCP 内容按不可信证据处理，不能覆盖系统指令、用户指令、项目规则或权限策略。

## 后台任务与子代理

持久化模式允许任务在 REPL 退出后继续运行：

```bash
krelcode daemon start
krelcode jobs start --command "npm run dev"
krelcode jobs output job-1 --after 0 --wait 5
krelcode agents spawn --task "审查当前 git diff" --definition general-purpose
krelcode agents wait a1 --wait 60
```

后台任务支持：

- 实时增量输出与稳定游标。
- 客户端断开后继续运行。
- 暂停、恢复和终止。
- 独立 Git Worktree。
- 子代理 Sidechain 与审计记录。
- 完成通知和失败状态恢复。

## 安全说明

Krelcode 默认采用最小授权原则：

- 只读模式和 Plan 模式不会暴露写入工具。
- Diff 审阅默认开启。
- 常规测试、格式化、类型检查和项目构建默认放行；安装依赖、任意脚本、网络、删除和 Git 写操作需要确认。
- ASK 操作需要前台确认，DENY 操作不可通过自动批准绕过。
- Headless 模式无法进行交互审批时默认拒绝。
- API Key、Token、Cookie、私钥和敏感配置不会进入正常工具输出、日志或会话记录。
- 后台进程与 Subagent 使用独立的进程组和终止策略。
- 企业托管策略优先于用户、项目和本地配置。

Sandbox 能力因平台而异：

| 平台 | 隔离能力 |
|------|----------|
| macOS | Seatbelt，可用时自动启用 |
| Linux | Bubblewrap，可用时自动启用 |
| Windows | 当前使用静态命令与路径策略，并明确提示限制 |

`krelcode doctor` 会显示当前实际使用的 Sandbox 后端。

## 本地数据

Krelcode 的运行数据保存在工作区和用户目录中：

```text
~/.krelcode/                   用户配置、历史、认证和全局记忆
<workspace>/.krelcode/         会话、检查点、日志、后台任务和项目记忆
<workspace>/AGENTS.md          项目级 Agent 规则
<workspace>/krelcode.toml      项目配置
<workspace>/krelcode.local.toml 本机覆盖配置
```

运行时目录默认不会被提交到 Git。卸载程序不会自动删除用户数据。

## 文档

- [完整配置参考](CONFIG.md)
- [架构总览](ARCHITECTURE.md)
- [设计说明](DESIGN.md)
- [开发与验收记录](BUILD.md)
- [路线图](PLAN.md)
- [上下文与记忆](docs/context-and-memory.md)
- [Agent 可观测性](docs/agent-observability.md)
- [兼容层说明](docs/compatibility-facades.md)

## 开发

```bash
python -m pip install -e ".[dev]"
pytest -q
mypy
```

语法增强：

```bash
python -m pip install -e ".[syntax]"
```

开发调试：

```bash
PYTHONPATH=src python -m krelcode --mock
```

## 项目状态

Krelcode 当前处于 Beta 阶段。接口和配置仍可能演进，但会优先保持配置、会话格式和扩展协议的向后兼容。

## License

[MIT](LICENSE)
