import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {test} from "node:test";
import {resolve} from "node:path";

import {
  KrelcodeController,
  type BridgeEvent,
  type BridgeQueryRequest,
  type EditorBridgeLike,
  type EditorHost,
  type PermissionDecision,
} from "../src/controller.ts";

class FakeBridge implements EditorBridgeLike {
  readonly sessionId = "session-vscode";
  permissionResponses: Array<[string, PermissionDecision]> = [];
  inputResponses: Array<[string, string]> = [];
  cancelledInputs: string[] = [];
  interrupted = false;
  closed = false;
  private readonly events: BridgeEvent[];
  private readonly wait: boolean;
  private release: (() => void) | undefined;

  constructor(events: BridgeEvent[], wait = false) {
    this.events = events;
    this.wait = wait;
  }

  async *query(request: BridgeQueryRequest): AsyncIterable<BridgeEvent> {
    for (const event of this.events) yield event;
    if (this.wait) {
      await new Promise<void>((resolveValue) => { this.release = resolveValue; });
    }
  }

  async respondPermission(permissionId: string, decision: PermissionDecision): Promise<boolean> {
    this.permissionResponses.push([permissionId, decision]);
    return true;
  }

  async respondInput(inputId: string, content: string): Promise<boolean> {
    this.inputResponses.push([inputId, content]);
    return true;
  }

  async cancelInput(inputId: string): Promise<boolean> {
    this.cancelledInputs.push(inputId);
    return true;
  }

  async interrupt(): Promise<boolean> {
    this.interrupted = true;
    this.release?.();
    return true;
  }

  async close(): Promise<void> {
    this.closed = true;
    this.release?.();
  }
}

class FakeHost implements EditorHost {
  output = "";
  warnings: string[] = [];
  busy = false;
  session: string | undefined;
  permission: PermissionDecision = "allow";
  input: string | undefined = "A";

  append(text: string): void { this.output += text; }
  setBusy(busy: boolean, _detail?: string): void { this.busy = busy; }
  setSession(sessionId?: string): void { this.session = sessionId; }
  warn(message: string): void { this.warnings.push(message); }

  async confirmPermission(
    _toolName: string,
    _argumentsValue: Record<string, unknown>,
    _reason: string,
  ): Promise<PermissionDecision> {
    return this.permission;
  }

  async askInput(_prompt: string, _options: string[], _allowFreeText: boolean): Promise<string | undefined> {
    return this.input;
  }
}

test("streams assistant text and maps session status", async () => {
  const bridge = new FakeBridge([
    {type: "session_started", sessionId: "session-vscode"},
    {type: "context_prepared", sources: [], skipped: [{path: ".env", reason: "敏感"}]},
    {type: "text_delta", text: "hello"},
    {type: "result", result: "hello", isError: false},
  ]);
  const host = new FakeHost();
  const controller = new KrelcodeController(bridge, host);
  await controller.run("hello");
  assert.equal(host.session, "session-vscode");
  assert.match(host.output, /hello/);
  assert.equal(host.busy, false);
  assert.match(host.warnings[0] ?? "", /\.env/);
});

test("asks user for permission and AskUser input", async () => {
  const bridge = new FakeBridge([
    {
      type: "permission_requested",
      permissionId: "call-1",
      tool: {name: "run_command", arguments: {command: "npm test"}},
      reason: "运行测试",
    },
    {
      type: "input_requested",
      inputId: "input-1",
      prompt: "选择方案",
      options: ["A", "B"],
      allowFreeText: true,
    },
    {type: "result", result: "done", isError: false},
  ]);
  const host = new FakeHost();
  host.permission = "always";
  const controller = new KrelcodeController(bridge, host);
  await controller.run("run");
  assert.deepEqual(bridge.permissionResponses, [["call-1", "always"]]);
  assert.deepEqual(bridge.inputResponses, [["input-1", "A"]]);
});

test("stop interrupts active query and close releases the bridge", async () => {
  const bridge = new FakeBridge([], true);
  const host = new FakeHost();
  const controller = new KrelcodeController(bridge, host);
  const running = controller.run("wait");
  await new Promise((resolveValue) => setTimeout(resolveValue, 0));
  assert.equal(controller.running, true);
  assert.equal(await controller.stop(), true);
  await running;
  await controller.close();
  assert.equal(bridge.interrupted, true);
  assert.equal(bridge.closed, true);
});

test("manifest exposes the expected minimal editor commands", () => {
  const packagePath = resolve(import.meta.dirname, "../package.json");
  const manifest = JSON.parse(readFileSync(packagePath, "utf8")) as {
    contributes: {commands: Array<{command: string}>};
  };
  const commands = manifest.contributes.commands.map((item) => item.command);
  assert.deepEqual(commands, [
    "krelcode.ask",
    "krelcode.explainSelection",
    "krelcode.reviewFile",
    "krelcode.stop",
    "krelcode.newSession",
    "krelcode.showStatus",
  ]);
});
