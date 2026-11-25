# Krelcode Agent 可观测性与消融实验

## 1. 定位

Web UI 不是 CLI 内部的一个渲染分支，而是与 CLI/TUI、SDK、IDE 并列的前端：

```text
                   ┌── CLI / TUI       krelcode
                   ├── Web UI          CLI 生命周期内的同步前端
AgentRuntime ──────┼── Python/TS SDK   stream-json
  EventBus         ├── VS Code
  Session API      └── JetBrains
        │
        ▼
ObservabilitySink（旁路）
        │
        ├─ SecretRedactor
        ├─ bounded queue
        └─ append-only JSONL
                 │
                 ▼
          localhost Web Gateway
             HTTP + SSE
```

`AgentLoop`、Provider、Tool、MCP 不导入 Web 代码。Web 进程退出、SSE 慢连接、日志写入失败
或事件投影失败，都不能向 Agent 主流程传播。

## 2. 启动

Agent 默认把脱敏后的观测事件写到当前 workspace：

```bash
krelcode
```

执行 `krelcode` 进入交互式 REPL 时，会在同一 CLI 进程内自动启动 Web 前端并打印一次性
token URL；不需要额外命令。服务随 CLI 启动，并在 CLI 退出时关闭。headless、pipe 与
stream-json 默认不监听端口：

```toml
[webui]
enabled = "auto"      # auto / true / false
host = "127.0.0.1"    # 只允许 loopback
port = 8765
port_scan = 10
open_browser = false
```

也可以按本轮覆盖：

```bash
krelcode --webui       # 强制启动
krelcode --no-webui    # 关闭
```

服务只允许绑定 loopback，启动时生成随机 token。浏览器第一次通过带 token 的 URL 打开后，
token 会兑换成 `HttpOnly + SameSite=Strict` cookie，并从地址栏移除。

关闭 Agent 侧记录：

```bash
krelcode --no-observability
```

或在配置中关闭：

```toml
[observability]
enabled = false
```

## 3. 存储布局

```text
.krelcode/observability/
  active-experiment.json        # 下一轮实验 profile，Web 写入
  runs/
    <run-id>/
      manifest.json             # 运行前冻结的 RunManifest
      events.jsonl              # 带 sequence 的 EventEnvelope
```

目录权限目标为 `0700`，文件为 `0600`。事件采用 append-only JSONL；队列满时发生丢事件，
消费者通过 `sequence` 缺口识别不完整 trace，而不是静默假设数据连续。

## 4. EventEnvelope

```json
{
  "schema_version": 1,
  "event_id": "…",
  "run_id": "…",
  "sequence": 12,
  "timestamp": 1795571453.99,
  "session_id": "…",
  "turn_id": "turn-1",
  "step": 2,
  "span_id": "call-abc",
  "parent_span_id": "run-id",
  "kind": "tool",
  "name": "tool_call_started",
  "payload": {
    "tool_call": {
      "id": "call-abc",
      "name": "read_file",
      "arguments": {"path": "src/app.py"}
    }
  }
}
```

`kind` 的稳定分类包括：`run / step / query / model / tool / permission / context /
memory / plan / interaction / hook / job / subagent / error`。

## 5. RunManifest

每次运行在首个事件前冻结以下信息：

- code version；
- provider / model；
- REPL / print / JSON / SDK / subagent 运行模式；
- 权限模式；
- layered config hash；
- tool catalog hash 与工具名快照；
- experiment id / name / hash / knobs；
- 实验依赖警告。

这使 Baseline 和 Variant 的比较不会把配置变化、模型变化或工具池变化误当成 Prompt 效果。

## 6. Web 页面

- **Live Timeline**：模型、工具、Context、Memory、MCP、Subagent、错误事件；
- **Context Inspector**：token 分布、stable/dynamic、cache hash、cache breakpoint、
  ContextSource、compact 与 memory 事件；
- **Experiments**：安全白名单开关，生成下一轮 profile；
- 工具输入输出与 event payload 默认折叠，可展开查看已脱敏 JSON。

## 7. 实验开关

当前白名单：

| Knob | 作用 |
|---|---|
| `context.enabled` | Context v3 装配 |
| `context.compression` | microcompact / structured compact |
| `context.directory_rules` | 按目录加载规则 |
| `memory.mode` | `off / suggest / auto` |
| `prompt.authoring` | Prompt Authoring Contract 与 Prompt Lab |
| `mcp.enabled` | MCP 工具与 Prompt |
| `tools.disabled` | 从动态工具池移除工具 |
| `features.enabled` | Runtime feature flags |

安全底线不是可消融变量：Sandbox、PermissionMode、网络、auto-approve、managed policy、
凭据路径等不能由实验 profile 修改。

profile 在进程启动时校验并应用，turn 中途不热改。Web 保存成功后只影响下一轮 Run。
CLI 也可以显式指定：

```bash
krelcode --experiment-profile .krelcode/observability/my-profile.json
```

## 8. HTTP / SSE API

```text
GET /api/v1/status
GET /api/v1/runs
GET /api/v1/runs/{run_id}
GET /api/v1/runs/{run_id}/events?after=N
GET /api/v1/runs/{run_id}/stream?after=N
GET /api/v1/experiments/knobs
GET /api/v1/experiments/active
PUT /api/v1/experiments/active
```

实时事件使用 SSE。`after` 或 `Last-Event-ID` 对应 EventEnvelope sequence，浏览器断线重连后
从断点继续；缺口仍可从事件序列中检测。

## 9. 隐私边界

- 写入前统一执行 `SecretRedactor`；
- API key、Bearer token、Cookie、常见云厂商 token 与 private key 被替换为 `***`；
- image base64 不落盘，只记录类型和字节数；
- 字符串、集合和嵌套深度都有上限；
- 可通过 `include_text_previews = false` 关闭正文预览；
- Web 默认不开放公网，不允许配置非 loopback 地址。

## 10. 当前阶段与后续

本阶段已完成旁路事件底座、RunManifest、实验 profile、独立 Web 前端、HTTP/SSE 和只读监控。

下一阶段可在不改变 UI/core 边界的前提下增加：

1. Web 创建并启动 baseline/variant Run；
2. 通过 daemon 或 Session Service 统一取消、审批和 steering；
3. Baseline vs Variant 指标差异和实验报告导出；
4. Trace waterfall、artifact 大输出、SQLite 索引；
5. 多 workspace 与企业审计视图。
