# Krelcode IDE Integrations

Krelcode keeps the agent core in one place. VS Code and JetBrains are thin
adapters over the existing `stream-json` v1 protocol:

```text
VS Code web/command host ──┐
                           ├── @krelcode/editor-bridge ── @krelcode/sdk
JetBrains tool window ─────┘                                  │
                                                       stream-json v1
                                                              │
                                                         Krelcode CLI
                                                    Provider / Tools / MCP
                                                    Sandbox / Context / Memory
```

The adapters never import a Provider, run tools directly, open a second Agent
runtime, or read a model API key. Krelcode keeps using its normal
`api_key_env` / inherited environment mechanism.

## Shared protocol behavior

The persistent CLI accepts these editor control messages in addition to `user`:

```json
{"type":"control","subtype":"interrupt"}
{"type":"control","subtype":"end"}
{"type":"control","subtype":"permission_response","request_id":"call-1","decision":"allow"}
{"type":"control","subtype":"user_input_response","request_id":"input-1","action":"accept","content":"A"}
{"type":"control","subtype":"user_input_response","request_id":"input-1","action":"cancel"}
```

Permission decisions are `allow`, `deny`, or `always`. Interaction IDs are
one-shot, and unmatched/stale responses are rejected. An editor must auto-deny
permissions and cancel AskUser requests when their UI deadline expires, the turn
is interrupted, or the host closes.

Outbound editor interactions use `permission_id` and `input_id` so they never
collide with the SDK's top-level `request_id` transport envelope.

## Context safety

`Explain Selection` and `Review Current File` send only the text explicitly
provided by the IDE. The bridge does not recursively read files. Context is:

- restricted to the current workspace, including symlink-safe path checks in the
  JetBrains adapter;
- blocked for `.git/`, `.krelcode/`, `.ssh/`, credential stores, private keys,
  `.env*`, `credentials*`, `secrets.*`, and related sensitive patterns;
- redacted for common API keys, bearer tokens and secret assignments;
- bounded to 120,000 selected characters and 20 diagnostics by default;
- labelled `[Evidence: EditorSelection]` with
  `requires_validation: true` and `trust: untrusted-editor-data`.

The current VS Code implementation shows a safe, cancellable approval dialog;
read-only/plan modes can be selected without enabling `autoApprove`.

## VS Code

Commands:

- `Krelcode: Ask in Workspace`
- `Krelcode: Explain Selection`
- `Krelcode: Review Current File`
- `Krelcode: Stop`
- `Krelcode: New Session`
- `Krelcode: Show Status`

Development:

```bash
cd editors
npm install
npm run test
npm run typecheck
npm run build
cd vscode
npm run package
```

Use **Run Extension** from a VS Code Extension Development Host, or install the
generated VSIX. Set `krelcode.pythonPath` when the Extension Host's `python3`
cannot import the installed Krelcode package.

## JetBrains

Requirements:

- IntelliJ Platform 2024.2 or newer;
- JDK 17 for building;
- `krelcode` on `PATH`, or configure its absolute path in
  **Settings → Tools → Krelcode**.

Development:

```bash
cd editors/jetbrains
gradle test
gradle buildPlugin
```

Install the ZIP from **Settings → Plugins → Install Plugin from Disk**. The
tool window sends API keys only through inherited environment variables. Its
permission/input dialogs fail closed on cancellation, and the process is
terminated when the project or plugin is disposed.

## Test strategy

- `editors/shared/tests`: fake transport tests for context filtering, event
  mapping, permission/input one-shot routing, timeout/close cleanup and interrupt;
- `editors/vscode/tests`: command manifest, stream mapping, dialog routing,
  stop/close and binary-independent controller tests;
- `editors/jetbrains/src/test`: pure Java tests for sensitive paths, Evidence
  wrapping and secret redaction;
- Python `stream-json` tests cover inbound permission parsing, early-response
  races, acknowledgements and AskUser broker routing.

No test requires a live IDE, network, real model or API key.
