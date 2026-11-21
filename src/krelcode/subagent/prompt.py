"""Canonical model-facing guidance for delegating work to a subagent.

This Prompt teaches the parent agent how to write ``spawn_agent.task``.  It is
not injected into the child's user message: the task must remain the exact
handoff written by the parent.  ToolPromptSpec renders this text into the L5
managed tool contract.
"""

from __future__ import annotations


SPAWN_AGENT_PROMPT_VERSION = "1"


SPAWN_AGENT_BASE_PROMPT = """\
Write the `task` argument as a self-contained mental-model handoff for a fresh subagent. The
child cannot see this conversation, your private reasoning, or prior tool
output. It sees only the task you pass plus its inherited instructions and
task-relevant memory. Do not write the task as a note to a colleague who
already shares your mental model.

Inject the mental model the child needs before acting:
- Current state: what is already true, including verified findings and prior
  decisions that constrain the work.
- Why delegate: the bounded objective, why it is independent, and why it can
  run without the parent's full history.
- Starting points: exact file paths, symbols, commands, failing tests, error
  text, and relevant data flow.
- Boundaries: what the child may change, what it must not change, and which
  unrelated areas or expensive searches to avoid.
- Deliverable: the concrete artifact or report the parent needs back.
- Verification: commands, assertions, or evidence that prove completion.
- Ownership: what the parent will continue doing, so work is not duplicated.

Do not make the subagent guess:
- Replace phrases such as "the thing we discussed", "the usual fix", or "you
  know what I mean" with explicit facts and locations.
- Distinguish confirmed facts from assumptions. State uncertainty and give the
  precise file, command, or test that can resolve it.
- If an unknown requires exploration, say what to inspect and when to stop;
  do not imply that the child should invent missing context.
- Never rely on the child remembering prior messages. It only has this task.

Good delegation example:
`In src/krelcode/auth/session.py, refresh_session() drops provider_meta when it
rebuilds Session. Bug report: expired OAuth sessions fail only after resume.
Reproduce with tests/test_session_resume.py::test_oauth_metadata. Preserve all
other fields and update only session.py plus a focused regression test. Run the
focused test and the session suite. Return changed paths, test results, and any
remaining uncertainty.`

Bad delegation examples:
- `Investigate the bug we discussed.`
- `Fix the failing test and clean things up if needed.`
- `Continue from where I left off; use your judgment.`

Before calling spawn_agent, confirm that the task contains enough evidence for
an independent agent to begin without guessing. Keep the scope minimal and do
not delegate work you will also do locally.

When `definition` is omitted, the runtime selects the built-in
`general-purpose` agent for complex exploration-plus-action tasks. Prefer
`Explore` for fast read-only repository research, `Plan` for read-only
implementation planning, and `claude` as the catch-all when no specialized
agent fits. `statusline-setup` and `claude-code-guide` are focused helpers for
their named domains. Explore and Plan intentionally omit user/project rule files
and must not be delegated work that requires modifying the workspace.

For risky changes that need independent adversarial verification, use
`definition="verifier"` and provide the exact change scope, baseline, commands
already run, expected behavior, and claims that must be challenged. The fork
flag does not copy the parent's full history; the task must carry the handoff.
"""


SPAWN_AGENT_FORK_ADDENDUM = """\
Fork-context handoff (`fork_context=true`):
In Krelcode, fork_context records lineage and compatibility; it does not copy
the parent's full conversation history. The child still receives only the
explicit task, inherited instructions, and task-relevant memory.

- Hand-carry the current task state: confirmed facts, unresolved assumptions,
  decisions already made, completed work, remaining goal, and active blockers.
- Mark each item as confirmed or unconfirmed. Do not present hypotheses as
  facts merely because the parent was considering them.
- State what has already been tried and what must not be repeated.
- Describe the exact point from which the child should continue. Do not write
  "continue as before" or assume the child shares the parent's mental state.
- If the handoff is incomplete, gather the missing evidence first or ask a
  concise clarification instead of asking the child to infer it.

Fork is a workflow handoff, not a copied brain. The quality of the child's work
depends on the explicit context you transfer in `task`.
"""


def build_spawn_agent_prompt(*, include_fork: bool = True) -> str:
    """Return the managed delegation Prompt, optionally with fork guidance."""
    parts = [SPAWN_AGENT_BASE_PROMPT.strip()]
    if include_fork:
        parts.append(SPAWN_AGENT_FORK_ADDENDUM.strip())
    return "\n\n".join(parts)


__all__ = [
    "SPAWN_AGENT_BASE_PROMPT",
    "SPAWN_AGENT_FORK_ADDENDUM",
    "SPAWN_AGENT_PROMPT_VERSION",
    "build_spawn_agent_prompt",
]
