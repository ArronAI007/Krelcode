# `@krelcode/editor-bridge`

Shared, zero-runtime-dependency adapter between editor hosts and
`@krelcode/sdk`. It owns only editor-facing concerns:

- process/session lifecycle through the existing SDK;
- one active turn and explicit interrupt semantics;
- selection/diagnostic Evidence assembly with workspace and sensitive-file checks;
- one-shot permission and AskUser routing with fail-closed timeouts;
- bounded, redacted event mapping for editor renderers.

It does **not** contain Provider, tool, MCP, sandbox, context or session logic.
