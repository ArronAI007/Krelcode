declare module "vscode" {
  export interface Disposable { dispose(): void }
  export interface Position { line: number; character: number }
  export interface Range { start: Position; end: Position }
  export interface Selection extends Range { isEmpty: boolean }
  export interface Uri { fsPath: string; scheme: string }
  export interface Diagnostic {
    range: Range;
    severity: number;
    message: string;
    source?: string;
  }
  export interface TextDocument {
    uri: Uri;
    fileName: string;
    languageId: string;
    lineCount: number;
    getText(range?: Range): string;
  }
  export interface TextEditor {
    document: TextDocument;
    selection: Selection;
  }
  export interface WorkspaceFolder { uri: Uri; name: string }
  export interface WorkspaceConfiguration {
    get<T>(section: string, defaultValue: T): T;
  }
  export interface OutputChannel extends Disposable {
    append(value: string): void;
    appendLine(value: string): void;
    clear(): void;
    show(preserveFocus?: boolean): void;
  }
  export interface StatusBarItem extends Disposable {
    text: string;
    tooltip?: string;
    command?: string;
    show(): void;
  }
  export interface ExtensionContext {
    subscriptions: Disposable[];
  }
  export interface QuickPickItem { label: string; description?: string }

  export const DiagnosticSeverity: {
    Error: number;
    Warning: number;
    Information: number;
    Hint: number;
  };
  export const StatusBarAlignment: {Left: number; Right: number};
  export const ViewColumn: {Beside: number};
  export const workspace: {
    workspaceFolders?: WorkspaceFolder[];
    getConfiguration(section?: string): WorkspaceConfiguration;
    onDidChangeConfiguration(listener: (event: unknown) => void): Disposable;
  };
  export const window: {
    activeTextEditor?: TextEditor;
    showInputBox(options?: {prompt?: string; placeHolder?: string; password?: boolean; ignoreFocusOut?: boolean}): Promise<string | undefined>;
    showQuickPick(items: QuickPickItem[], options?: {placeHolder?: string}): Promise<QuickPickItem | undefined>;
    showWarningMessage(message: string, options?: {modal: boolean}, ...items: string[]): Promise<string | undefined>;
    showInformationMessage(message: string): Promise<string | undefined>;
    showErrorMessage(message: string): Promise<string | undefined>;
    createOutputChannel(name: string): OutputChannel;
    createStatusBarItem(alignment: number, priority?: number): StatusBarItem;
  };
  export const commands: {
    registerCommand(id: string, handler: (...args: unknown[]) => unknown): Disposable;
  };
  export const languages: {
    getDiagnostics(uri: Uri): Diagnostic[];
  };
}
