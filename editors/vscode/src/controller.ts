export type PermissionDecision = "allow" | "deny" | "always";

export interface SelectionContext {
  path: string;
  text: string;
  startLine: number;
  startColumn: number;
  endLine: number;
  endColumn: number;
  languageId?: string;
}

export interface DiagnosticContext {
  path: string;
  line: number;
  column: number;
  severity: "error" | "warning" | "information" | "hint";
  message: string;
  source?: string;
}

export interface BridgeQueryRequest {
  prompt: string;
  context?: {selection?: SelectionContext; diagnostics?: DiagnosticContext[]};
  signal?: AbortSignal;
}

export type BridgeEvent =
  | {type: "session_started"; sessionId?: string}
  | {type: "context_prepared"; sources: unknown[]; skipped: Array<{path: string; reason: string}>}
  | {type: "text_delta"; text: string}
  | {type: "thinking_delta"; text: string}
  | {type: "status"; phase: string; detail: string}
  | {type: "tool"; phase: string; tool?: {name: string}; content?: string; isError?: boolean}
  | {type: "permission_requested"; permissionId: string; tool: {name: string; arguments: Record<string, unknown>}; reason: string}
  | {type: "input_requested"; inputId: string; prompt: string; options: string[]; allowFreeText: boolean}
  | {type: "input_resolved"; inputId: string; answered: boolean}
  | {type: "control"; accepted: boolean; detail?: string}
  | {type: "result"; result: string; isError: boolean}
  | {type: "error"; message: string};

export interface EditorBridgeLike {
  readonly sessionId?: string;
  query(request: BridgeQueryRequest): AsyncIterable<BridgeEvent>;
  respondPermission(permissionId: string, decision: PermissionDecision): Promise<boolean>;
  respondInput(inputId: string, content: string): Promise<boolean>;
  cancelInput(inputId: string): Promise<boolean>;
  interrupt(): Promise<boolean>;
  close(): Promise<void>;
}

export interface EditorHost {
  append(text: string): void;
  setBusy(busy: boolean, detail?: string): void;
  setSession(sessionId?: string): void;
  warn(message: string): void;
  confirmPermission(toolName: string, argumentsValue: Record<string, unknown>, reason: string): Promise<PermissionDecision>;
  askInput(prompt: string, options: string[], allowFreeText: boolean): Promise<string | undefined>;
}

export class KrelcodeController {
  private readonly bridge: EditorBridgeLike;
  private readonly host: EditorHost;
  private active: AbortController | undefined;

  constructor(bridge: EditorBridgeLike, host: EditorHost) {
    this.bridge = bridge;
    this.host = host;
  }

  get running(): boolean {
    return this.active !== undefined;
  }

  async run(prompt: string, context?: BridgeQueryRequest["context"]): Promise<void> {
    if (this.active) throw new Error("Krelcode 已有运行中的请求");
    if (!prompt.trim()) throw new Error("问题不能为空");
    const active = new AbortController();
    this.active = active;
    this.host.setBusy(true, "Krelcode 正在处理请求");
    try {
      for await (const event of this.bridge.query({prompt, context, signal: active.signal})) {
        await this.handle(event);
      }
    } catch (error) {
      if (!active.signal.aborted) {
        this.host.warn(error instanceof Error ? error.message : String(error));
      }
    } finally {
      if (this.active === active) this.active = undefined;
      this.host.setBusy(false);
    }
  }

  async stop(): Promise<boolean> {
    const wasRunning = this.active !== undefined;
    this.active?.abort();
    const interrupted = await this.bridge.interrupt();
    this.host.setBusy(false, "Krelcode 已停止");
    return wasRunning || interrupted;
  }

  async close(): Promise<void> {
    this.active?.abort();
    this.active = undefined;
    await this.bridge.close();
    this.host.setBusy(false);
  }

  private async handle(event: BridgeEvent): Promise<void> {
    if (event.type === "session_started") {
      this.host.setSession(event.sessionId);
      return;
    }
    if (event.type === "context_prepared") {
      for (const skipped of event.skipped) {
        this.host.warn(`跳过编辑器上下文 ${skipped.path}: ${skipped.reason}`);
      }
      if (event.sources.length > 0) {
        this.host.append(`\n[context] 已附加 ${event.sources.length} 个来源\n`);
      }
      return;
    }
    if (event.type === "text_delta") {
      this.host.append(event.text);
      return;
    }
    if (event.type === "thinking_delta") {
      this.host.setBusy(true, "Krelcode 正在思考");
      return;
    }
    if (event.type === "status") {
      this.host.setBusy(true, event.detail || event.phase);
      return;
    }
    if (event.type === "tool") {
      if (event.phase === "started") this.host.append(`\n\n[tool] ${event.tool?.name ?? "unknown"}\n`);
      if (event.phase === "completed" && event.isError) this.host.append(`[tool error] ${event.content ?? ""}\n`);
      return;
    }
    if (event.type === "permission_requested") {
      const decision = await this.host.confirmPermission(
        event.tool.name,
        event.tool.arguments,
        event.reason,
      );
      const accepted = await this.bridge.respondPermission(event.permissionId, decision);
      if (!accepted) this.host.warn("权限响应已过期，Krelcode 将按安全默认值继续");
      return;
    }
    if (event.type === "input_requested") {
      const answer = await this.host.askInput(event.prompt, event.options, event.allowFreeText);
      if (answer === undefined) await this.bridge.cancelInput(event.inputId);
      else await this.bridge.respondInput(event.inputId, answer);
      return;
    }
    if (event.type === "control" && !event.accepted) {
      this.host.warn(event.detail || "交互响应未被接受");
      return;
    }
    if (event.type === "error") {
      this.host.warn(event.message);
      return;
    }
    if (event.type === "result") {
      if (event.isError) this.host.warn(event.result || "Krelcode 查询失败");
      else this.host.append("\n\n");
    }
  }
}
