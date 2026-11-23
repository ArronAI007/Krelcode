import { randomUUID } from "node:crypto";

import { prepareEditorContext, redactSecrets } from "./context.ts";
import {
  EDITOR_BRIDGE_PROTOCOL_VERSION,
  type AgentMessage,
  type AgentTransport,
  type AgentTransportFactory,
  type EditorBridgeOptions,
  type EditorEvent,
  type EditorQueryRequest,
  type PermissionDecision,
  type ToolReference,
} from "./types.ts";

interface PendingInteraction {
  kind: "permission" | "input";
  timer: ReturnType<typeof setTimeout>;
}

const MAX_EVENT_TEXT = 64_000;

function stringValue(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function boundedText(value: unknown): string {
  const text = redactSecrets(stringValue(value));
  return text.length <= MAX_EVENT_TEXT
    ? text
    : `${text.slice(0, MAX_EVENT_TEXT)}\n[truncated]`;
}

function sanitizeJson(value: unknown, depth = 0): unknown {
  if (depth > 8) return "[depth-limit]";
  if (typeof value === "string") return boundedText(value);
  if (value === null || typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (Array.isArray(value)) return value.slice(0, 200).map((item) => sanitizeJson(item, depth + 1));
  if (typeof value === "object") {
    const output: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>).slice(0, 200)) {
      output[key] = sanitizeJson(item, depth + 1);
    }
    return output;
  }
  return String(value);
}

function toolReference(value: unknown): ToolReference | undefined {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
  const record = value as Record<string, unknown>;
  const args = typeof record.arguments === "object" && record.arguments !== null
    && !Array.isArray(record.arguments)
    ? record.arguments as Record<string, unknown>
    : {};
  return {
    id: stringValue(record.id),
    name: stringValue(record.name, "unknown"),
    arguments: sanitizeJson(args) as Record<string, unknown>,
  };
}

function eventFromMessage(message: AgentMessage, workspace: string): EditorEvent | undefined {
  if (message.type === "assistant" && message.subtype === "text_delta") {
    return {type: "text_delta", text: boundedText(message.get("text"))};
  }
  if (message.type === "assistant" && message.subtype === "thinking_delta") {
    return {type: "thinking_delta", text: boundedText(message.get("text"))};
  }
  if (message.type === "query" && message.subtype === "phase_changed") {
    return {
      type: "status",
      phase: stringValue(message.get("phase"), "working"),
      detail: boundedText(message.get("detail")),
    };
  }
  if (message.type === "tool" && ["started", "progress", "completed"].includes(message.subtype)) {
    const tool = toolReference(message.get("tool_call"));
    return {
      type: "tool",
      phase: message.subtype as "started" | "progress" | "completed",
      tool,
      message: redactSecrets(stringValue(message.get("message"))),
      content: message.subtype === "completed" ? boundedText(message.get("content")) : undefined,
      isError: Boolean(message.get("is_error", false)),
    };
  }
  if (message.type === "permission" && message.subtype === "requested") {
    const tool = toolReference(message.get("tool_call"));
    if (!tool) return {type: "error", message: "权限请求缺少工具信息"};
    return {
      type: "permission_requested",
      permissionId: stringValue(message.get("permission_id"), tool.id),
      tool,
      reason: boundedText(message.get("reason")),
      expiresAt: 0,
    };
  }
  if (message.type === "input" && message.subtype === "requested") {
    const options = message.get("options", []);
    return {
      type: "input_requested",
      inputId: stringValue(message.get("input_id")),
      prompt: boundedText(message.get("prompt")),
      options: Array.isArray(options) ? options.map((item) => boundedText(item)).slice(0, 8) : [],
      allowFreeText: Boolean(message.get("allow_free_text", true)),
      expiresAt: 0,
    };
  }
  if (message.type === "input" && message.subtype === "resolved") {
    return {
      type: "input_resolved",
      inputId: stringValue(message.get("input_id")),
      answered: Boolean(message.get("answered")),
    };
  }
  if (message.type === "control" && message.subtype.endsWith("_ack")) {
    return {
      type: "control",
      subtype: message.subtype,
      requestId: stringValue(message.get("request_id")),
      accepted: Boolean(message.get("accepted")),
      detail: redactSecrets(stringValue(message.get("detail"))),
    };
  }
  if (message.isTerminal) {
    return {
      type: "result",
      subtype: message.subtype,
      result: boundedText(message.get("result")),
      isError: message.isError,
      requestId: message.requestId,
    };
  }
  if (message.type === "error") {
    return {
      type: "error",
      message: boundedText(message.get("error", message.get("result", "Krelcode error"))),
      requestId: message.requestId,
    };
  }
  void workspace;
  return undefined;
}

export class EditorBridge {
  private readonly factory: AgentTransportFactory;
  private readonly options: EditorBridgeOptions;
  private transport: AgentTransport | undefined;
  private activeQuery: AbortController | undefined;
  private readonly pending = new Map<string, PendingInteraction>();
  private closed = false;

  constructor(factory: AgentTransportFactory, options: EditorBridgeOptions) {
    this.factory = factory;
    this.options = options;
  }

  get sessionId(): string | undefined {
    return this.transport?.sessionId;
  }

  async start(): Promise<void> {
    if (this.closed) throw new Error("EditorBridge 已关闭");
    if (this.transport) return;
    this.transport = await this.factory.start(this.options);
  }

  async *query(request: EditorQueryRequest): AsyncGenerator<EditorEvent> {
    if (this.closed) throw new Error("EditorBridge 已关闭");
    if (this.activeQuery) throw new Error("已有运行中的查询；请先停止或等待完成");
    const prepared = prepareEditorContext(
      this.options.workspace,
      request.prompt,
      request.context,
      {
        maxSelectionCharacters: this.options.maxSelectionCharacters,
        maxDiagnostics: this.options.maxDiagnostics,
      },
    );
    const controller = new AbortController();
    this.activeQuery = controller;
    const onAbort = () => controller.abort();
    if (request.signal?.aborted) controller.abort();
    else request.signal?.addEventListener("abort", onAbort, {once: true});
    try {
      await this.start();
      const transport = this.transport;
      if (!transport) throw new Error("Krelcode transport 未启动");
      yield {
        type: "session_started",
        sessionId: transport.sessionId,
        protocolVersion: EDITOR_BRIDGE_PROTOCOL_VERSION,
      };
      yield {type: "context_prepared", sources: prepared.sources, skipped: prepared.skipped};
      for await (const message of transport.query(prepared.prompt, {
        requestId: request.requestId ?? `editor-${randomUUID()}`,
        signal: controller.signal,
      })) {
        const event = eventFromMessage(message, this.options.workspace);
        if (!event) continue;
        if (event.type === "permission_requested") {
          this.trackPermission(event.permissionId);
          yield {...event, expiresAt: Date.now() + this.permissionTimeoutMs};
          continue;
        }
        if (event.type === "input_requested") {
          this.trackInput(event.inputId);
          yield {...event, expiresAt: Date.now() + this.inputTimeoutMs};
          continue;
        }
        if (event.type === "input_resolved") this.clearPending(event.inputId);
        if (event.type === "control") this.clearPending(event.requestId);
        yield event;
        if (event.type === "result") break;
      }
    } catch (error) {
      if (controller.signal.aborted) {
        yield {type: "error", message: "查询已取消"};
      } else {
        const detail = error instanceof Error ? error.message : String(error);
        yield {type: "error", message: redactSecrets(detail)};
      }
    } finally {
      request.signal?.removeEventListener("abort", onAbort);
      await this.rejectPending();
      if (this.activeQuery === controller) this.activeQuery = undefined;
    }
  }

  async respondPermission(permissionId: string, decision: PermissionDecision): Promise<boolean> {
    const pending = this.pending.get(permissionId);
    if (!pending || pending.kind !== "permission") return false;
    this.clearPending(permissionId);
    return (await this.transport?.respondPermission(permissionId, decision)) ?? false;
  }

  async respondInput(inputId: string, content: string): Promise<boolean> {
    const pending = this.pending.get(inputId);
    if (!pending || pending.kind !== "input") return false;
    if (!content.trim()) return false;
    this.clearPending(inputId);
    return (await this.transport?.respondUserInput(inputId, content)) ?? false;
  }

  async cancelInput(inputId: string): Promise<boolean> {
    const pending = this.pending.get(inputId);
    if (!pending || pending.kind !== "input") return false;
    this.clearPending(inputId);
    return (await this.transport?.cancelUserInput(inputId)) ?? false;
  }

  async interrupt(): Promise<boolean> {
    this.activeQuery?.abort();
    return (await this.transport?.interrupt()) ?? false;
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    await this.rejectPending();
    this.activeQuery?.abort();
    await this.transport?.close();
    this.transport = undefined;
  }

  private get permissionTimeoutMs(): number {
    return Math.max(1_000, this.options.permissionTimeoutMs ?? 120_000);
  }

  private get inputTimeoutMs(): number {
    return Math.max(1_000, this.options.inputTimeoutMs ?? 300_000);
  }

  private trackPermission(permissionId: string): void {
    if (!permissionId || this.pending.has(permissionId)) return;
    const timer = setTimeout(() => {
      this.pending.delete(permissionId);
      void this.transport?.respondPermission(permissionId, "deny").catch(() => undefined);
    }, this.permissionTimeoutMs);
    this.pending.set(permissionId, {kind: "permission", timer});
  }

  private trackInput(inputId: string): void {
    if (!inputId || this.pending.has(inputId)) return;
    const timer = setTimeout(() => {
      this.pending.delete(inputId);
      void this.transport?.cancelUserInput(inputId).catch(() => undefined);
    }, this.inputTimeoutMs);
    this.pending.set(inputId, {kind: "input", timer});
  }

  private clearPending(id: string): void {
    const pending = this.pending.get(id);
    if (!pending) return;
    clearTimeout(pending.timer);
    this.pending.delete(id);
  }

  private async rejectPending(): Promise<void> {
    const entries = [...this.pending.entries()];
    this.pending.clear();
    await Promise.allSettled(entries.map(([id, pending]) => {
      clearTimeout(pending.timer);
      if (pending.kind === "permission") {
        return this.transport?.respondPermission(id, "deny") ?? Promise.resolve(false);
      }
      return this.transport?.cancelUserInput(id) ?? Promise.resolve(false);
    }));
  }
}

export async function* query(
  factory: AgentTransportFactory,
  options: EditorBridgeOptions,
  request: EditorQueryRequest,
): AsyncGenerator<EditorEvent> {
  const bridge = new EditorBridge(factory, options);
  try {
    yield* bridge.query(request);
  } finally {
    await bridge.close();
  }
}

export function numericEventField(message: AgentMessage, key: string): number {
  return numberValue(message.get(key));
}
