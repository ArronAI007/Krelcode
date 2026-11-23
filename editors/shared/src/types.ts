export const EDITOR_BRIDGE_PROTOCOL_VERSION = "1";

export type EditorKind = "vscode" | "jetbrains" | "other";
export type PermissionMode = "default" | "read-only" | "auto-approve" | "plan";
export type PermissionDecision = "allow" | "deny" | "always";

export interface EditorSelection {
  path: string;
  text: string;
  startLine: number;
  startColumn: number;
  endLine: number;
  endColumn: number;
  languageId?: string;
}

export interface EditorDiagnostic {
  path: string;
  line: number;
  column: number;
  severity: "error" | "warning" | "information" | "hint";
  message: string;
  source?: string;
}

export interface EditorContext {
  selection?: EditorSelection;
  diagnostics?: EditorDiagnostic[];
}

export interface ContextSource {
  kind: "selection" | "diagnostics";
  path: string;
  startLine?: number;
  endLine?: number;
  characters: number;
  truncated: boolean;
}

export interface ContextSkip {
  path: string;
  reason: string;
}

export interface PreparedEditorContext {
  prompt: string;
  sources: ContextSource[];
  skipped: ContextSkip[];
}

export interface AgentMessage {
  readonly type: string;
  readonly subtype: string;
  readonly requestId?: string;
  readonly isTerminal: boolean;
  readonly isError: boolean;
  get<T = unknown>(key: string, fallback?: T): T | unknown | undefined;
}

export interface AgentQueryOptions {
  requestId: string;
  signal?: AbortSignal;
}

export interface AgentTransport {
  readonly sessionId?: string;
  readonly init?: AgentMessage;
  query(prompt: string, options: AgentQueryOptions): AsyncIterable<AgentMessage>;
  respondPermission(permissionId: string, decision: PermissionDecision): Promise<boolean>;
  respondUserInput(inputId: string, content: string): Promise<boolean>;
  cancelUserInput(inputId: string): Promise<boolean>;
  interrupt(): Promise<boolean>;
  close(): Promise<void>;
}

export interface AgentTransportOptions {
  workspace: string;
  permissionMode: PermissionMode;
  autoApprove: boolean;
  noSandbox: boolean;
  pythonPath?: string;
  configPath?: string;
  model?: string;
  provider?: string;
  env?: Record<string, string | undefined>;
}

export interface AgentTransportFactory {
  start(options: AgentTransportOptions): Promise<AgentTransport>;
}

export interface EditorQueryRequest {
  prompt: string;
  context?: EditorContext;
  requestId?: string;
  signal?: AbortSignal;
}

export interface EditorBridgeOptions extends AgentTransportOptions {
  permissionTimeoutMs?: number;
  inputTimeoutMs?: number;
  maxSelectionCharacters?: number;
  maxDiagnostics?: number;
}

export interface ToolReference {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export type EditorEvent =
  | {type: "session_started"; sessionId?: string; protocolVersion: string}
  | {type: "context_prepared"; sources: ContextSource[]; skipped: ContextSkip[]}
  | {type: "text_delta"; text: string}
  | {type: "thinking_delta"; text: string}
  | {type: "status"; phase: string; detail: string}
  | {type: "tool"; phase: "started" | "progress" | "completed"; tool?: ToolReference; message?: string; content?: string; isError?: boolean}
  | {type: "permission_requested"; permissionId: string; tool: ToolReference; reason: string; expiresAt: number}
  | {type: "input_requested"; inputId: string; prompt: string; options: string[]; allowFreeText: boolean; expiresAt: number}
  | {type: "input_resolved"; inputId: string; answered: boolean}
  | {type: "control"; subtype: string; requestId: string; accepted: boolean; detail?: string}
  | {type: "result"; subtype: string; result: string; isError: boolean; requestId?: string}
  | {type: "error"; message: string; requestId?: string};
