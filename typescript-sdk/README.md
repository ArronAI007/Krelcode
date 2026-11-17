# Krelcode TypeScript SDK

Zero-runtime-dependency TypeScript client for Krelcode's `stream-json` v1
protocol. It starts the Python CLI as a child process and does not duplicate the
agent, provider, tool, MCP, sandbox, hook or context implementations.

```ts
import { KrelcodeClient } from "@krelcode/sdk";

const client = await KrelcodeClient.start({
  workspace: process.cwd(),
  mock: true,
  noSession: true,
  noContext: true,
  noMcp: true,
});

try {
  for await (const message of client.query("Explain this repository")) {
    if (message.type === "assistant" && message.subtype === "text_delta") {
      process.stdout.write(String(message.get("text", "")));
    }
  }
} finally {
  await client.close();
}
```

API keys are inherited through the child environment and must never be placed in
`extraArgs`. `options.env` can explicitly override environment variables.

The CLI must be importable by the configured Python executable. Set
`KRELCODE_PYTHON`, or pass `executable`, when `python3 -m krelcode` is not on the
default path.

Development tests use Node's built-in TypeScript stripping:

```bash
npm test
```
