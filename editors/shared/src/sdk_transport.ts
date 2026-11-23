import type {
  AgentMessage,
  AgentQueryOptions,
  AgentTransport,
  AgentTransportFactory,
  AgentTransportOptions,
  PermissionDecision,
} from "./types.ts";

interface SdkClient {
  readonly init?: AgentMessage;
  readonly sessionId?: string;
  query(prompt: string, options: AgentQueryOptions): AsyncIterable<AgentMessage>;
  respondPermission(permissionId: string, decision: PermissionDecision): Promise<boolean>;
  respondUserInput(inputId: string, content: string): Promise<boolean>;
  cancelUserInput(inputId: string): Promise<boolean>;
  interrupt(): Promise<boolean>;
  close(): Promise<void>;
}

interface SdkModule {
  KrelcodeClient: {start(options: Record<string, unknown>): Promise<SdkClient>};
}

class SdkAgentTransport implements AgentTransport {
  private readonly client: SdkClient;

  constructor(client: SdkClient) {
    this.client = client;
  }

  get sessionId(): string | undefined {
    return this.client.sessionId;
  }

  get init(): AgentMessage | undefined {
    return this.client.init;
  }

  query(prompt: string, options: AgentQueryOptions): AsyncIterable<AgentMessage> {
    return this.client.query(prompt, options);
  }

  respondPermission(permissionId: string, decision: PermissionDecision): Promise<boolean> {
    return this.client.respondPermission(permissionId, decision);
  }

  respondUserInput(inputId: string, content: string): Promise<boolean> {
    return this.client.respondUserInput(inputId, content);
  }

  cancelUserInput(inputId: string): Promise<boolean> {
    return this.client.cancelUserInput(inputId);
  }

  interrupt(): Promise<boolean> {
    return this.client.interrupt();
  }

  close(): Promise<void> {
    return this.client.close();
  }
}

export class SdkAgentTransportFactory implements AgentTransportFactory {
  async start(options: AgentTransportOptions): Promise<AgentTransport> {
    // Keep the package name dynamic so developing the adapter does not require
    // a pre-installed node_modules tree. Runtime resolution remains explicit.
    const packageName = "@krelcode/sdk";
    // Keep the module specifier statically visible so esbuild can bundle the
    // adapter into the VS Code extension while the package identity remains
    // explicit in diagnostics and source audits.
    const sdk = await import("@krelcode/sdk") as SdkModule;
    if (!sdk.KrelcodeClient) {
      throw new Error(`Krelcode SDK adapter (${packageName}) 缺少 KrelcodeClient`);
    }
    const client = await sdk.KrelcodeClient.start({
      workspace: options.workspace,
      cwd: options.workspace,
      executable: options.pythonPath,
      config: options.configPath,
      provider: options.provider,
      model: options.model,
      permissionMode: options.permissionMode,
      autoApprove: options.autoApprove,
      noSandbox: options.noSandbox,
      env: options.env,
    });
    return new SdkAgentTransport(client);
  }
}
