# Krelcode for Visual Studio Code

Use the Krelcode coding agent from VS Code without moving Provider, tool, MCP,
Sandbox, context or memory logic into the extension. The extension starts the
installed Krelcode CLI through `@krelcode/sdk` and consumes its `stream-json`
protocol.

## Commands

- `Krelcode: Ask in Workspace`
- `Krelcode: Explain Selection`
- `Krelcode: Review Current File`
- `Krelcode: Stop`
- `Krelcode: New Session`
- `Krelcode: Show Status`

Results stream into the **Krelcode** Output channel. Permission and Ask User
prompts are routed back to the same persistent session. Cancelled or timed-out
interactions fail closed.

## Setup

Install Krelcode globally, then ensure the CLI is available on `PATH`:

```bash
krelcode --version
```

The extension normally uses `python3 -m krelcode`. Set `krelcode.pythonPath`
when the Extension Host needs an explicit Python executable. API keys are never
placed in command-line arguments; inherited environment variables and the
normal Krelcode configuration layers remain the only credential sources.

## Development

```bash
cd editors
npm install
npm run test
npm run typecheck
npm run build
cd vscode
npm run package
```

Open `editors/vscode` as an Extension Development Host or install the generated
VSIX. See `../README.md` for the shared protocol and security contract.
