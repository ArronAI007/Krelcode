/**
 * Zero-runtime-dependency TypeScript SDK for Krelcode stream-json v1.
 *
 * The SDK is a process/protocol adapter only. Provider selection, tools,
 * permissions, MCP, hooks, context assembly and persistence stay in the Python
 * CLI. API keys therefore enter only through the inherited child environment,
 * never argv, request messages or diagnostic strings.
 */

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { delimiter, dirname, isAbsolute, join, resolve } from "node:path";
import { createInterface, type Interface as ReadLineInterface } from "node:readline";

export const PROTOCOL_NAME = "krelcode.stream-json";
export const PROTOCOL_VERSION = "1";
const INPUT_DONE = Symbol("input-done");
const RESERVED_ARGS = new Set([
  "-m", "--message", "--input-format", "--output-format", "--version",
]);

export class KrelcodeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = new.target.name;
  }
}

export class KrelcodeProtocolError extends KrelcodeError {}
export class KrelcodeProcessError extends KrelcodeError {}

export class KrelcodeResultError extends KrelcodeError {
  readonly result: KrelcodeMessage;

  constructor(message: string, result: KrelcodeMessage) {
    super(message);
    this.result = result;
  }
}

export interface KrelcodeOptions {
  workspace?: string;
  provider?: string;
  model?: string;
  baseUrl?: string;
  config?: string;
  mock?: boolean;
  permissionMode?: "default" | "read-only" | "auto-approve" | "plan";
  autoApprove?: boolean;
  noSandbox?: boolean;
  noContext?: boolean;
  noSkills?: boolean;
  skillsDirs?: string[];
  noMcp?: boolean;
  noSession?: boolean;
  noCheckpoint?: boolean;
  autoCommit?: boolean;
  noCompress?: boolean;
  noWeb?: boolean;
  maxSteps?: number;
  repeatFailureLimit?: number;
  tokenBudget?: number;
  retries?: number;
  contextTokens?: number;
  sandboxBackend?: "auto" | "static" | "seatbelt" | "bubblewrap";
  sandboxNetwork?: boolean;
  resume?: string;
  cwd?: string;
  executable?: string;
  extraArgs?: string[];
  env?: Record<string, string | undefined>;
  startupTimeoutMs?: number;
  closeTimeoutMs?: number;
  maxLineBytes?: number;
}

interface ResolvedOptions {
  workspace: string;
  provider?: string;
  model?: string;
  baseUrl?: string;
  config?: string;
  mock: boolean;
  permissionMode?: string;
  autoApprove: boolean;
  noSandbox: boolean;
  noContext: boolean;
  noSkills: boolean;
  skillsDirs: string[];
  noMcp: boolean;
  noSession: boolean;
  noCheckpoint: boolean;
  autoCommit: boolean;
  noCompress: boolean;
  noWeb: boolean;
  maxSteps?: number;
  repeatFailureLimit?: number;
  tokenBudget?: number;
  retries?: number;
  contextTokens?: number;
  sandboxBackend?: string;
  sandboxNetwork: boolean;
  resume?: string;
  cwd: string;
  executable: string;
  extraArgs: string[];
  env: Record<string, string | undefined>;
  startupTimeoutMs: number;
  closeTimeoutMs: number;
  maxLineBytes: number;
}

export interface ImageInputData {
  mediaType: string;
  dataB64: string;
  sourcePath?: string;
}

export class ImageInput {
  readonly mediaType: string;
  readonly dataB64: string;
  readonly sourcePath?: string;

  constructor(mediaType: string, dataB64: string, sourcePath?: string) {
    this.mediaType = mediaType;
    this.dataB64 = dataB64;
    this.sourcePath = sourcePath;
  }

  toBlock(): Record<string, unknown> {
    const source: Record<string, unknown> = {
      type: "base64",
      media_type: this.mediaType,
      data: this.dataB64,
    };
    if (this.sourcePath) source.path = this.sourcePath;
    return {type: "image", source};
  }
}

export interface QueryOptions {
  requestId?: string;
  images?: ImageInput[];
  signal?: AbortSignal;
}

export type PermissionDecision = "allow" | "deny" | "always";

export class KrelcodeMessage {
  readonly data: Record<string, unknown>;

  constructor(data: Record<string, unknown>) {
    this.data = data;
  }

  get type(): string {
    return String(this.data.type ?? "");
  }

  get subtype(): string {
    return String(this.data.subtype ?? "");
  }

  get requestId(): string | undefined {
    const value = this.data.request_id;
    return value === undefined || value === null ? undefined : String(value);
  }

  get sessionId(): string | undefined {
    const value = this.data.session_id;
    return value === undefined || value === null ? undefined : String(value);
  }

  get turnId(): string | undefined {
    const value = this.data.turn_id;
    return value === undefined || value === null ? undefined : String(value);
  }

  get isTerminal(): boolean {
    return this.type === "result";
  }

  get isError(): boolean {
    return Boolean(this.data.is_error ?? false);
  }

  get<T = unknown>(key: string, fallback?: T): T | unknown | undefined {
    return Object.prototype.hasOwnProperty.call(this.data, key)
      ? this.data[key]
      : fallback;
  }
}

class AsyncQueue<T> {
  private readonly values: T[] = [];
  private readonly waiters: Array<(value: T) => void> = [];

  put(value: T): void {
    const waiter = this.waiters.shift();
    if (waiter) waiter(value);
    else this.values.push(value);
  }

  async get(): Promise<T> {
    if (this.values.length > 0) return this.values.shift() as T;
    return await new Promise<T>((resolveValue) => this.waiters.push(resolveValue));
  }

  drain(value: T): void {
    while (this.values.length > 0) this.values.shift();
    while (this.waiters.length > 0) {
      const waiter = this.waiters.shift();
      waiter?.(value);
    }
  }
}

function resolveOptions(options: KrelcodeOptions): ResolvedOptions {
  const workspace = resolve(options.workspace ?? process.cwd());
  const cwd = resolve(options.cwd ?? workspace);
  const executable = options.executable
    ?? process.env.KRELCODE_PYTHON
    ?? process.env.PYTHON
    ?? "python3";
  const extraArgs = [...(options.extraArgs ?? [])];
  for (const arg of extraArgs) {
    const name = arg.split("=", 1)[0] ?? "";
    if (RESERVED_ARGS.has(name)) {
      throw new Error(`extraArgs 不能覆盖 SDK 保留参数: ${name}`);
    }
  }
  const startupTimeoutMs = options.startupTimeoutMs ?? 30_000;
  const closeTimeoutMs = options.closeTimeoutMs ?? 5_000;
  const maxLineBytes = options.maxLineBytes ?? 16 * 1024 * 1024;
  if (startupTimeoutMs <= 0 || closeTimeoutMs <= 0) {
    throw new Error("startupTimeoutMs 与 closeTimeoutMs 必须大于 0");
  }
  if (maxLineBytes < 64 * 1024) {
    throw new Error("maxLineBytes 不能小于 64 KiB");
  }
  return {
    workspace,
    provider: options.provider,
    model: options.model,
    baseUrl: options.baseUrl,
    config: options.config,
    mock: Boolean(options.mock),
    permissionMode: options.permissionMode,
    autoApprove: Boolean(options.autoApprove),
    noSandbox: Boolean(options.noSandbox),
    noContext: Boolean(options.noContext),
    noSkills: Boolean(options.noSkills),
    skillsDirs: [...(options.skillsDirs ?? [])],
    noMcp: Boolean(options.noMcp),
    noSession: Boolean(options.noSession),
    noCheckpoint: Boolean(options.noCheckpoint),
    autoCommit: Boolean(options.autoCommit),
    noCompress: Boolean(options.noCompress),
    noWeb: Boolean(options.noWeb),
    maxSteps: options.maxSteps,
    repeatFailureLimit: options.repeatFailureLimit,
    tokenBudget: options.tokenBudget,
    retries: options.retries,
    contextTokens: options.contextTokens,
    sandboxBackend: options.sandboxBackend,
    sandboxNetwork: Boolean(options.sandboxNetwork),
    resume: options.resume,
    cwd,
    executable,
    extraArgs,
    env: {...(options.env ?? {})},
    startupTimeoutMs,
    closeTimeoutMs,
    maxLineBytes,
  };
}

function commandFor(options: ResolvedOptions): string[] {
  const command = [
    options.executable,
    "-m",
    "krelcode",
    "--workspace",
    options.workspace,
  ];
  if (options.mock) command.push("--mock");
  else if (options.provider) command.push("--provider", options.provider);
  if (options.model) command.push("--model", options.model);
  if (options.baseUrl) command.push("--base-url", options.baseUrl);
  if (options.config) command.push("--config", options.config);
  if (options.permissionMode) command.push("--permission-mode", options.permissionMode);
  if (options.autoApprove) command.push("--auto-approve");
  if (options.noSandbox) command.push("--no-sandbox");
  if (options.noContext) command.push("--no-context");
  if (options.noSkills) command.push("--no-skills");
  for (const path of options.skillsDirs) command.push("--skills-dir", path);
  if (options.noMcp) command.push("--no-mcp");
  if (options.noSession) command.push("--no-session");
  if (options.noCheckpoint) command.push("--no-checkpoint");
  if (options.autoCommit) command.push("--auto-commit");
  if (options.noCompress) command.push("--no-compress");
  if (options.noWeb) command.push("--no-web");
  if (options.maxSteps !== undefined) command.push("--max-steps", String(options.maxSteps));
  if (options.repeatFailureLimit !== undefined) {
    command.push("--repeat-failure-limit", String(options.repeatFailureLimit));
  }
  if (options.tokenBudget !== undefined) command.push("--token-budget", String(options.tokenBudget));
  if (options.retries !== undefined) command.push("--retries", String(options.retries));
  if (options.contextTokens !== undefined) command.push("--context-tokens", String(options.contextTokens));
  if (options.sandboxBackend) command.push("--sandbox-backend", options.sandboxBackend);
  if (options.sandboxNetwork) command.push("--sandbox-network");
  if (options.resume) command.push("--resume", options.resume);
  command.push(...options.extraArgs);
  command.push("--input-format", "stream-json", "--output-format", "stream-json");
  return command;
}

function environmentFor(options: ResolvedOptions): Record<string, string> {
  const env: Record<string, string> = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value !== undefined) env[key] = value;
  }
  for (const [key, value] of Object.entries(options.env)) {
    if (value === undefined) delete env[key];
    else env[key] = value;
  }
  const pythonPath = env.PYTHONPATH;
  if (!pythonPath) {
    const candidate = resolve(options.workspace, "src");
    // Do not mutate a caller's environment merely because the source tree
    // happens to exist; this fallback only helps local source checkouts.
    if (existsSync(join(options.workspace, "pyproject.toml")) && existsSync(candidate)) {
      env.PYTHONPATH = candidate;
    }
  }
  return env;
}

function sensitiveEnvironmentValues(env: Record<string, string>): string[] {
  const values: string[] = [];
  for (const [key, value] of Object.entries(env)) {
    const upper = key.toUpperCase();
    if (
      /(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)$/.test(upper)
      && value.length >= 6
      && !values.includes(value)
    ) {
      values.push(value);
    }
  }
  return values.sort((a, b) => b.length - a.length);
}

function scrubSecrets(text: string, known: readonly string[]): string {
  let result = text;
  for (const value of known) result = result.split(value).join("***");
  result = result.replace(/sk-[A-Za-z0-9][A-Za-z0-9_-]{5,}/g, "***");
  result = result.replace(/(bearer\s+)[A-Za-z0-9._~+/=-]{8,}/gi, "$1***");
  result = result.replace(
    /((?:api[_-]?key|access[_-]?token|secret)\s*[=:]\s*)[^\s"']{6,}/gi,
    "$1***",
  );
  return result;
}

function messageFrom(value: unknown): KrelcodeMessage {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new KrelcodeProtocolError("stream-json 每行必须是 JSON 对象");
  }
  const record = value as Record<string, unknown>;
  if (typeof record.type !== "string") {
    throw new KrelcodeProtocolError("stream-json 消息缺少字符串 type");
  }
  return new KrelcodeMessage(record);
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  return new Promise<T>((resolveValue, rejectValue) => {
    const timer = setTimeout(() => rejectValue(new KrelcodeProcessError(message)), timeoutMs);
    promise.then(
      (value) => { clearTimeout(timer); resolveValue(value); },
      (error: unknown) => { clearTimeout(timer); rejectValue(error); },
    );
  });
}

export class KrelcodeClient {
  readonly options: ResolvedOptions;
  private process: ChildProcessWithoutNullStreams | undefined;
  private stdoutReader: ReadLineInterface | undefined;
  private stdoutTask: Promise<void> | undefined;
  private stderrTask: Promise<void> | undefined;
  private exitPromise: Promise<number | null> | undefined;
  private readonly requestQueues = new Map<string, AsyncQueue<KrelcodeMessage | typeof INPUT_DONE>>();
  private readonly controlQueue = new AsyncQueue<KrelcodeMessage | typeof INPUT_DONE>();
  private readonly stderrLines: string[] = [];
  private stderrBytes = 0;
  private readonly sensitiveValues: string[];
  private fatalError: KrelcodeError | undefined;
  private spawnError: Error | undefined;
  private initMessage: KrelcodeMessage | undefined;
  private sessionIdentifier: string | undefined;
  private started = false;
  private closed = false;

  constructor(options: KrelcodeOptions = {}) {
    this.options = resolveOptions(options);
    this.sensitiveValues = sensitiveEnvironmentValues(environmentFor(this.options));
  }

  static async start(options: KrelcodeOptions = {}): Promise<KrelcodeClient> {
    const client = new KrelcodeClient(options);
    await client.start();
    return client;
  }

  get init(): KrelcodeMessage | undefined {
    return this.initMessage;
  }

  get sessionId(): string | undefined {
    return this.sessionIdentifier;
  }

  get processId(): number | undefined {
    return this.process?.pid;
  }

  command(): string[] {
    return commandFor(this.options);
  }

  async start(): Promise<KrelcodeClient> {
    if (this.closed) throw new KrelcodeError("KrelcodeClient 已关闭");
    if (this.process && this.process.exitCode === null && !this.process.killed) return this;
    if (this.started) {
      throw new KrelcodeProcessError("Krelcode 子进程已经退出，客户端不能重新启动");
    }
    const command = commandFor(this.options);
    const executable = command[0];
    if (!executable) throw new KrelcodeProcessError("Krelcode 启动命令为空");
    const child = spawn(executable, command.slice(1), {
      cwd: this.options.cwd,
      env: environmentFor(this.options),
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.process = child;
    this.started = true;
    // EPIPE can arrive asynchronously after a fast child exit.
    child.stdin.on("error", () => undefined);
    child.stdout.on("error", () => undefined);
    child.stderr.on("error", () => undefined);
    child.once("error", (error) => {
      this.spawnError = error;
      this.fatalError = new KrelcodeProcessError(
        `无法启动 Krelcode 子进程: ${error.name}: ${error.message}`,
      );
      this.controlQueue.put(INPUT_DONE);
      for (const queue of this.requestQueues.values()) queue.put(INPUT_DONE);
    });
    this.exitPromise = new Promise<number | null>((resolveValue) => {
      this.process?.once("close", (code) => resolveValue(code));
    });
    this.stdoutTask = this.readStdout();
    this.stderrTask = this.readStderr();

    let first: KrelcodeMessage | typeof INPUT_DONE;
    try {
      first = await withTimeout(
        this.controlQueue.get(),
        this.options.startupTimeoutMs,
        "等待 Krelcode system/init 超时",
      );
    } catch (error) {
      await this.close();
      throw error;
    }
    if (first === INPUT_DONE) {
      const detail = this.spawnError
        ? new KrelcodeProcessError(
          `无法启动 Krelcode 子进程: ${this.spawnError.name}: ${this.spawnError.message}`,
        )
        : this.processError("Krelcode 子进程在初始化前关闭");
      await this.close();
      throw detail;
    }
    if (first.type !== "system" || first.subtype !== "init") {
      await this.close();
      throw new KrelcodeProtocolError(
        `首条消息必须是 system/init，实际为 ${first.type}/${first.subtype}`,
      );
    }
    if (first.get("protocol") !== PROTOCOL_NAME) {
      await this.close();
      throw new KrelcodeProtocolError(
        `不支持的协议: ${String(first.get("protocol"))}`,
      );
    }
    if (String(first.get("protocol_version")) !== PROTOCOL_VERSION) {
      await this.close();
      throw new KrelcodeProtocolError(
        `不支持的协议版本: ${String(first.get("protocol_version"))}`,
      );
    }
    this.initMessage = first;
    this.sessionIdentifier = first.sessionId;
    return this;
  }

  async *query(prompt: string, options: QueryOptions = {}): AsyncGenerator<KrelcodeMessage> {
    await this.start();
    if (typeof prompt !== "string") throw new TypeError("prompt 必须是字符串");
    const images = options.images ?? [];
    if (!prompt.trim() && images.length === 0) {
      throw new Error("prompt 与 images 不能同时为空");
    }
    const requestId = options.requestId ?? `sdk-${randomUUID()}`;
    if (!requestId.trim()) throw new Error("requestId 不能为空");
    if (this.requestQueues.has(requestId)) {
      throw new Error(`requestId 正在使用: ${requestId}`);
    }
    const content: string | Array<Record<string, unknown>> = images.length
      ? [{"type": "text", "text": prompt}, ...images.map((image) => image.toBlock())]
      : prompt;
    const queue = new AsyncQueue<KrelcodeMessage | typeof INPUT_DONE>();
    this.requestQueues.set(requestId, queue);
    let abortHandler: (() => void) | undefined;
    try {
      if (options.signal?.aborted) throw new DOMException("Aborted", "AbortError");
      if (options.signal) {
        abortHandler = () => queue.put(INPUT_DONE);
        options.signal.addEventListener("abort", abortHandler, {once: true});
      }
      this.writeJson({
        type: "user",
        content,
        request_id: requestId,
      });
      while (true) {
        const item = await queue.get();
        if (item === INPUT_DONE) {
          if (options.signal?.aborted) throw new DOMException("Aborted", "AbortError");
          if (this.fatalError) throw this.fatalError;
          throw this.processError("Krelcode 子进程在查询结束前关闭了 stdout");
        }
        yield item;
        if (item.isTerminal) return;
      }
    } finally {
      if (abortHandler && options.signal) {
        options.signal.removeEventListener("abort", abortHandler);
      }
      this.requestQueues.delete(requestId);
    }
  }

  async run(prompt: string, options: QueryOptions = {}): Promise<string> {
    let result: KrelcodeMessage | undefined;
    for await (const message of this.query(prompt, options)) {
      if (message.isTerminal) result = message;
    }
    if (!result) throw new KrelcodeProcessError("查询未产生 result 终态");
    const text = String(result.get("result", ""));
    if (result.isError) throw new KrelcodeResultError(text || "Krelcode 查询失败", result);
    return text;
  }

  async interrupt(): Promise<boolean> {
    if (!this.isRunning()) return false;
    this.writeJson({type: "control", subtype: "interrupt"});
    return true;
  }

  async respondPermission(
    permissionId: string,
    decision: PermissionDecision,
  ): Promise<boolean> {
    if (!this.isRunning() || !permissionId.trim()) return false;
    if (!["allow", "deny", "always"].includes(decision)) {
      throw new TypeError("decision 必须为 allow/deny/always");
    }
    this.writeJson({
      type: "control",
      subtype: "permission_response",
      request_id: permissionId,
      decision,
    });
    return true;
  }

  async respondUserInput(inputId: string, content: string): Promise<boolean> {
    if (!this.isRunning() || !inputId.trim()) return false;
    if (!content.trim()) throw new TypeError("content 不能为空");
    if (content.length > 32_000) throw new TypeError("content 超过 32000 字符限制");
    this.writeJson({
      type: "control",
      subtype: "user_input_response",
      request_id: inputId,
      action: "accept",
      content,
    });
    return true;
  }

  async cancelUserInput(inputId: string): Promise<boolean> {
    if (!this.isRunning() || !inputId.trim()) return false;
    this.writeJson({
      type: "control",
      subtype: "user_input_response",
      request_id: inputId,
      action: "cancel",
    });
    return true;
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    const child = this.process;
    if (!child) return;
    if (child.exitCode === null && !child.killed) {
      try {
        this.writeJson({type: "control", subtype: "end"});
      } catch {
        // Process may already have closed between the check and write.
      }
      child.stdin.end();
      try {
        await withTimeout(
          this.exitPromise ?? Promise.resolve(null),
          this.options.closeTimeoutMs,
          "等待 Krelcode 子进程退出超时",
        );
      } catch {
        child.kill("SIGTERM");
        await new Promise<void>((resolveValue) => {
          const timer = setTimeout(() => {
            child.kill("SIGKILL");
            resolveValue();
          }, 1_000);
          if (child.exitCode !== null) {
            clearTimeout(timer);
            resolveValue();
          } else {
            child.once("close", () => { clearTimeout(timer); resolveValue(); });
          }
        });
      }
    }
    await Promise.allSettled([this.stdoutTask, this.stderrTask]);
    this.stdoutReader?.close();
    this.process = undefined;
  }

  stderrTail(limit = 8_000): string {
    const text = this.stderrLines.join("");
    return scrubSecrets(text.slice(-Math.max(1, limit)), this.sensitiveValues);
  }

  private isRunning(): boolean {
    return Boolean(this.process && this.process.exitCode === null && !this.process.killed);
  }

  private writeJson(payload: Record<string, unknown>): void {
    const child = this.process;
    if (!child || child.exitCode !== null || child.stdin.destroyed) {
      throw this.processError("Krelcode 子进程不可写");
    }
    child.stdin.write(`${JSON.stringify(payload)}\n`, "utf8");
  }

  private async readStdout(): Promise<void> {
    const child = this.process;
    if (!child) return;
    this.stdoutReader = createInterface({
      input: child.stdout,
      crlfDelay: Infinity,
    });
    try {
      for await (const line of this.stdoutReader) {
        if (!line.trim()) continue;
        if (Buffer.byteLength(line, "utf8") > this.options.maxLineBytes) {
          this.fatalError = new KrelcodeProtocolError("stream-json 单行输出超过限制");
          break;
        }
        let message: KrelcodeMessage;
        try {
          message = messageFrom(JSON.parse(line));
        } catch (error) {
          const detail = error instanceof Error ? error.message : String(error);
          this.fatalError = new KrelcodeProtocolError(`无效 stream-json 输出: ${detail}`);
          break;
        }
        const target = message.requestId
          ? this.requestQueues.get(message.requestId)
          : undefined;
        (target ?? this.controlQueue).put(message);
        if (message.type === "system" && message.subtype === "session_end") break;
      }
    } catch (error) {
      const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
      this.fatalError = new KrelcodeProtocolError(`读取 stream-json 输出失败: ${detail}`);
    } finally {
      this.stdoutReader.close();
      for (const queue of this.requestQueues.values()) queue.put(INPUT_DONE);
      this.controlQueue.put(INPUT_DONE);
    }
  }

  private async readStderr(): Promise<void> {
    const child = this.process;
    if (!child) return;
    await new Promise<void>((resolveValue) => {
      child.stderr.setEncoding("utf8");
      child.stderr.on("data", (chunk: string) => {
        const pieces = chunk.split(/\r?\n/).map((line) => line.slice(-4_000));
        for (const piece of pieces) {
          const line = `${piece}\n`;
          this.stderrLines.push(line);
          this.stderrBytes += Buffer.byteLength(line, "utf8");
          while (this.stderrBytes > 32 * 1024 && this.stderrLines.length > 0) {
            const removed = this.stderrLines.shift() ?? "";
            this.stderrBytes -= Buffer.byteLength(removed, "utf8");
          }
        }
      });
      child.stderr.once("end", resolveValue);
      child.stderr.once("error", resolveValue);
    });
  }

  private processError(prefix: string): KrelcodeProcessError {
    const code = this.process?.exitCode;
    const tail = this.stderrTail();
    return new KrelcodeProcessError(
      `${prefix}（exit=${String(code)}）${tail.trim() ? `\nstderr:\n${tail.trimEnd()}` : ""}`,
    );
  }
}

export async function* query(
  prompt: string,
  options: KrelcodeOptions = {},
  queryOptions: QueryOptions = {},
): AsyncGenerator<KrelcodeMessage> {
  const client = new KrelcodeClient(options);
  try {
    yield* client.query(prompt, queryOptions);
  } finally {
    await client.close();
  }
}

export function createClient(options: KrelcodeOptions = {}): KrelcodeClient {
  return new KrelcodeClient(options);
}

export async function run(
  prompt: string,
  options: KrelcodeOptions = {},
  queryOptions: QueryOptions = {},
): Promise<string> {
  const client = await KrelcodeClient.start(options);
  try {
    return await client.run(prompt, queryOptions);
  } finally {
    await client.close();
  }
}

// Keep the helper import reachable for bundlers that tree-shake path helpers.
void delimiter;
void dirname;
void isAbsolute;
