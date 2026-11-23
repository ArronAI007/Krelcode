import * as vscode from "vscode";

import {EditorBridge, SdkAgentTransportFactory} from "@krelcode/editor-bridge";

import {
  KrelcodeController,
  type DiagnosticContext,
  type EditorHost,
  type PermissionDecision,
  type SelectionContext,
} from "./controller.ts";

let controller: KrelcodeController | undefined;
let output: vscode.OutputChannel;
let status: vscode.StatusBarItem;
let activeSession: string | undefined;

function workspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function configuration(): vscode.WorkspaceConfiguration {
  return vscode.workspace.getConfiguration("krelcode");
}

function buildController(root: string): KrelcodeController {
  const config = configuration();
  const bridge = new EditorBridge(new SdkAgentTransportFactory(), {
    workspace: root,
    permissionMode: config.get("permissionMode", "default"),
    autoApprove: config.get("autoApprove", false),
    noSandbox: config.get("noSandbox", false),
    pythonPath: config.get("pythonPath", "") || undefined,
    provider: config.get("provider", "") || undefined,
    model: config.get("model", "") || undefined,
    configPath: config.get("configPath", "") || undefined,
    permissionTimeoutMs: config.get("permissionTimeoutSeconds", 120) * 1_000,
  });
  const host: EditorHost = {
    append(text: string): void {
      output.append(text);
    },
    setBusy(busy: boolean, detail?: string): void {
      status.text = busy ? "$(sync~spin) Krelcode" : "$(sparkle) Krelcode";
      status.tooltip = busy ? detail ?? "Krelcode 正在处理请求" : `Krelcode${activeSession ? ` · ${activeSession}` : ""}`;
    },
    setSession(sessionId?: string): void {
      activeSession = sessionId;
      status.tooltip = `Krelcode${sessionId ? ` · ${sessionId}` : ""}`;
    },
    warn(message: string): void {
      output.appendLine(`\n[warn] ${message}`);
      void vscode.window.showWarningMessage(message);
    },
    async confirmPermission(
      toolName: string,
      argumentsValue: Record<string, unknown>,
      reason: string,
    ): Promise<PermissionDecision> {
      const preview = JSON.stringify(argumentsValue, null, 2).slice(0, 4_000);
      const answer = await vscode.window.showWarningMessage(
        `Krelcode 请求执行 ${toolName}: ${reason}\n\n${preview}`,
        {modal: true},
        "仅本次允许",
        "始终允许",
        "拒绝",
      );
      if (answer === "仅本次允许") return "allow";
      if (answer === "始终允许") return "always";
      return "deny";
    },
    async askInput(prompt: string, options: string[], allowFreeText: boolean): Promise<string | undefined> {
      if (options.length === 0) {
        return await vscode.window.showInputBox({prompt, ignoreFocusOut: true});
      }
      const custom = "输入自定义答案…";
      const items = options.map((label) => ({label}));
      if (allowFreeText) items.push({label: custom});
      const selected = await vscode.window.showQuickPick(items, {placeHolder: prompt});
      if (!selected) return undefined;
      if (selected.label !== custom) return selected.label;
      return await vscode.window.showInputBox({prompt, ignoreFocusOut: true});
    },
  };
  return new KrelcodeController(bridge, host);
}

function ensureController(): KrelcodeController {
  const root = workspaceRoot();
  if (!root) throw new Error("请先打开一个 VS Code 工作区");
  if (!controller) controller = buildController(root);
  return controller;
}

function getSelection(
  document: vscode.TextDocument,
  selection: vscode.Selection,
  wholeFile = false,
): SelectionContext {
  const useWholeFile = wholeFile || selection.isEmpty;
  return {
    path: document.fileName,
    text: useWholeFile ? document.getText() : document.getText(selection),
    startLine: useWholeFile ? 0 : selection.start.line,
    startColumn: useWholeFile ? 0 : selection.start.character,
    endLine: useWholeFile ? document.lineCount - 1 : selection.end.line,
    endColumn: useWholeFile ? 0 : selection.end.character,
    languageId: document.languageId,
  };
}

function getDiagnostics(document: vscode.TextDocument): DiagnosticContext[] {
  if (!configuration().get("includeDiagnostics", true)) return [];
  const severityName = (severity: number): DiagnosticContext["severity"] => {
    if (severity === vscode.DiagnosticSeverity.Error) return "error";
    if (severity === vscode.DiagnosticSeverity.Warning) return "warning";
    if (severity === vscode.DiagnosticSeverity.Information) return "information";
    return "hint";
  };
  return vscode.languages.getDiagnostics(document.uri).slice(0, 20).map((diagnostic) => ({
    path: document.fileName,
    line: diagnostic.range.start.line,
    column: diagnostic.range.start.character,
    severity: severityName(diagnostic.severity),
    message: diagnostic.message,
    source: diagnostic.source,
  }));
}

async function ask(): Promise<void> {
  const prompt = await vscode.window.showInputBox({
    prompt: "询问 Krelcode",
    placeHolder: "例如：解释当前认证流程并指出风险",
    ignoreFocusOut: true,
  });
  if (!prompt) return;
  output.show(true);
  await ensureController().run(prompt);
}

async function explainSelection(): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    await vscode.window.showWarningMessage("请先打开一个文件");
    return;
  }
  if (editor.selection.isEmpty) {
    await vscode.window.showWarningMessage("请选择要解释的代码");
    return;
  }
  output.show(true);
  await ensureController().run("解释所选代码，说明其作用、调用关系、边界条件和潜在问题。", {
    selection: getSelection(editor.document, editor.selection),
    diagnostics: getDiagnostics(editor.document),
  });
}

async function reviewFile(): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    await vscode.window.showWarningMessage("请先打开一个文件");
    return;
  }
  output.show(true);
  await ensureController().run("审查当前文件，优先报告正确性、安全性和可维护性问题，并给出可执行建议。", {
    selection: getSelection(editor.document, editor.selection, true),
    diagnostics: getDiagnostics(editor.document),
  });
}

async function newSession(): Promise<void> {
  await controller?.close();
  controller = undefined;
  activeSession = undefined;
  output.clear();
  status.tooltip = "Krelcode";
  output.appendLine("[session] 新会话将在下一次请求时启动");
}

export function activate(context: vscode.ExtensionContext): void {
  output = vscode.window.createOutputChannel("Krelcode");
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.text = "$(sparkle) Krelcode";
  status.command = "krelcode.showStatus";
  status.show();
  context.subscriptions.push(output, status);
  context.subscriptions.push(
    vscode.commands.registerCommand("krelcode.ask", ask),
    vscode.commands.registerCommand("krelcode.explainSelection", explainSelection),
    vscode.commands.registerCommand("krelcode.reviewFile", reviewFile),
    vscode.commands.registerCommand("krelcode.stop", async () => controller?.stop()),
    vscode.commands.registerCommand("krelcode.newSession", newSession),
    vscode.commands.registerCommand("krelcode.showStatus", async () => {
      await vscode.window.showInformationMessage(
        `Krelcode ${controller?.running ? "运行中" : "空闲"}${activeSession ? ` · ${activeSession}` : ""}`,
      );
    }),
    vscode.workspace.onDidChangeConfiguration((event) => {
      void event;
      if (!controller?.running) {
        void controller?.close();
        controller = undefined;
      }
    }),
  );
}

export async function deactivate(): Promise<void> {
  await controller?.close();
  controller = undefined;
}
