import assert from "node:assert/strict";
import { test } from "node:test";

import { EditorBridge } from "../src/bridge.ts";
import { classifySensitiveEditorPath, prepareEditorContext, redactSecrets } from "../src/context.ts";
import type {
  AgentMessage,
  AgentQueryOptions,
  AgentTransport,
  AgentTransportFactory,
  AgentTransportOptions,
  EditorEvent,
  PermissionDecision,
} from "../src/types.ts";

const workspace = "/tmp/krelcode-editor-workspace";

class Message implements AgentMessage {
  readonly type: string;
  readonly subtype: string;
  readonly requestId?: string;
  readonly isTerminal: boolean;
  readonly isError: boolean;
  private readonly data: Record<string, unknown>;

  constructor(data: Record<string, unknown>) {
    this.data = data;
    this.type = String(data.type ?? "");
    this.subtype = String(data.subtype ?? "");
    this.requestId = data.request_id === undefined ? undefined : String(data.request_id);
    this.isTerminal = this.type === "result";
    this.isError = Boolean(data.is_error ?? false);
  }

  get<T = unknown>(key: string, fallback?: T): T | unknown | undefined {
    return Object.hasOwn(this.data, key) ? this.data[key] : fallback;
  }
}

class FakeTransport implements AgentTransport {
  sessionId = "session-1";
  init = new Message({type: "system", subtype: "init"});
  permissionResponses: Array<[string, PermissionDecision]> = [];
  inputResponses: Array<[string, string]> = [];
  cancelledInputs: string[] = [];
  interrupted = false;
  closed = false;
  prompts: string[] = [];
  private readonly pendingResult: Message;
  private resolvePending: ((message: Message) => void) | undefined;
  private storedResponse: Message | undefined;
  private readonly messages: AgentMessage[];
  private readonly waitForResponse: boolean;

  constructor(
    messages: AgentMessage[] = [],
    waitForResponse = false,
  ) {
    this.messages = messages;
    this.waitForResponse = waitForResponse;
    this.pendingResult = new Message({
      type: "result",
      subtype: "success",
      result: "done",
      is_error: false,
    });
  }

  async *query(prompt: string, _options: AgentQueryOptions): AsyncIterable<AgentMessage> {
    this.prompts.push(prompt);
    const response = this.waitForResponse && !this.storedResponse
      ? new Promise<Message>((resolve) => { this.resolvePending = resolve; })
      : undefined;
    for (const message of this.messages) yield message;
    if (this.storedResponse) {
      yield this.storedResponse;
    } else if (response) {
      const followUp = await response;
      yield followUp;
    }
    yield this.pendingResult;
  }

  async respondPermission(permissionId: string, decision: PermissionDecision): Promise<boolean> {
    this.permissionResponses.push([permissionId, decision]);
    this.complete(this.pendingResult);
    return true;
  }

  async respondUserInput(inputId: string, content: string): Promise<boolean> {
    this.inputResponses.push([inputId, content]);
    this.complete(this.pendingResult);
    return true;
  }

  async cancelUserInput(inputId: string): Promise<boolean> {
    this.cancelledInputs.push(inputId);
    this.complete(this.pendingResult);
    return true;
  }

  async interrupt(): Promise<boolean> {
    this.interrupted = true;
    this.complete(this.pendingResult);
    return true;
  }

  async close(): Promise<void> {
    this.closed = true;
    this.complete(this.pendingResult);
  }

  private complete(message: Message): void {
    if (this.resolvePending) {
      const resolve = this.resolvePending;
      this.resolvePending = undefined;
      resolve(message);
    } else {
      this.storedResponse = message;
    }
  }
}

class FakeFactory implements AgentTransportFactory {
  readonly transport: FakeTransport;

  constructor(transport: FakeTransport) {
    this.transport = transport;
  }

  async start(_options: AgentTransportOptions): Promise<AgentTransport> {
    return this.transport;
  }
}

function options() {
  return {
    workspace,
    permissionMode: "default" as const,
    autoApprove: false,
    noSandbox: false,
    permissionTimeoutMs: 60_000,
    inputTimeoutMs: 60_000,
  };
}

test("blocks sensitive and out-of-workspace editor context", () => {
  assert.match(classifySensitiveEditorPath(workspace, ".env") ?? "", /环境变量/);
  assert.match(classifySensitiveEditorPath(workspace, "src/.git/config") ?? "", /敏感目录/);
  assert.match(classifySensitiveEditorPath(workspace, "credentials.json") ?? "", /敏感文件/);
  assert.ok(classifySensitiveEditorPath(workspace, "../outside.ts"));

  const prepared = prepareEditorContext(workspace, "解释这段", {
    selection: {
      path: ".env",
      text: "DEEPSEEK_API_KEY=sk-secret-value",
      startLine: 0,
      startColumn: 0,
      endLine: 0,
      endColumn: 20,
    },
  });
  assert.equal(prepared.sources.length, 0);
  assert.equal(prepared.skipped[0]?.path, ".env");
  assert.equal(prepared.prompt.includes("sk-secret-value"), false);
});

test("redacts secrets in the user prompt before creating a request", () => {
  const prepared = prepareEditorContext(workspace, "检查 DEEPSEEK_API_KEY=sk-secret-value", undefined);
  assert.equal(prepared.prompt.includes("sk-secret-value"), false);
  assert.match(prepared.prompt, /\*\*\*/);
});

test("bounds and redacts selection evidence without loading other files", () => {
  const prepared = prepareEditorContext(workspace, "review", {
    selection: {
      path: "src/app.ts",
      text: `const token = "sk-super-secret";\n${"x".repeat(100)}`,
      startLine: 2,
      startColumn: 0,
      endLine: 4,
      endColumn: 8,
      languageId: "typescript",
    },
    diagnostics: [{
      path: "src/app.ts",
      line: 2,
      column: 1,
      severity: "error",
      message: "invalid token=secret-value",
    }],
  }, {maxSelectionCharacters: 40});

  assert.equal(prepared.sources.length, 2);
  assert.equal(prepared.sources[0]?.truncated, true);
  assert.match(prepared.prompt, /untrusted evidence/);
  assert.equal(prepared.prompt.includes("sk-super-secret"), false);
  assert.match(prepared.prompt, /src\/app\.ts:3-5/);
  assert.equal(redactSecrets("DEEPSEEK_API_KEY=sk-secret-value").includes("sk-secret-value"), false);
});

test("routes permission responses by one-time permission id", async () => {
  const tool = {id: "call-1", name: "run_command", arguments: {command: "npm test"}};
  const transport = new FakeTransport([
    new Message({type: "permission", subtype: "requested", permission_id: "call-1", tool_call: tool, reason: "需要运行测试"}),
  ], true);
  const bridge = new EditorBridge(new FakeFactory(transport), options());
  const iterator = bridge.query({prompt: "run tests"});

  const first = await iterator.next();
  assert.equal((first.value as EditorEvent).type, "session_started");
  const second = await iterator.next();
  assert.equal((second.value as EditorEvent).type, "context_prepared");
  const third = await iterator.next();
  assert.equal((third.value as EditorEvent).type, "permission_requested");
  assert.equal(await bridge.respondPermission("call-1", "allow"), true);
  assert.equal(await bridge.respondPermission("call-1", "deny"), false);
  await iterator.next();
  assert.deepEqual(transport.permissionResponses, [["call-1", "allow"]]);
  await bridge.close();
});

test("routes AskUser input and cancels pending interaction on close", async () => {
  const transport = new FakeTransport([
    new Message({type: "input", subtype: "requested", input_id: "input-1", prompt: "选择方案", options: ["A", "B"], allow_free_text: true}),
  ], true);
  const bridge = new EditorBridge(new FakeFactory(transport), options());
  const iterator = bridge.query({prompt: "choose"});
  await iterator.next();
  await iterator.next();
  const event = await iterator.next();
  assert.equal((event.value as EditorEvent).type, "input_requested");
  assert.equal(await bridge.respondInput("input-1", "A"), true);
  await iterator.next();
  assert.deepEqual(transport.inputResponses, [["input-1", "A"]]);
  await bridge.close();
});

test("rejects concurrent turns and sends interrupt", async () => {
  const transport = new FakeTransport([], true);
  const bridge = new EditorBridge(new FakeFactory(transport), options());
  const iterator = bridge.query({prompt: "one"});
  await iterator.next();
  await iterator.next();
  const second = bridge.query({prompt: "two"});
  await assert.rejects(async () => second.next(), /已有运行中的查询/);
  await bridge.interrupt();
  assert.equal(transport.interrupted, true);
  await iterator.next();
  await bridge.close();
  assert.equal(transport.closed, true);
});
