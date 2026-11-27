"""StateCheckpointBundle inspection command."""

from __future__ import annotations

from pathlib import Path

from ..context import CommandContext
from ..types import CommandResult
from ...state_checkpoint import LocalStateCheckpointStore
from .handler_utils import _ok


async def handle_state(ctx: CommandContext) -> CommandResult:
    path = (
        ctx.session_lifecycle.session_path
        if ctx.session_lifecycle is not None
        else (Path(ctx.session_path) if ctx.session_path else None)
    )
    session_id = path.stem if path is not None else ""
    store = LocalStateCheckpointStore(
        ctx.workspace_root,
        redactor=ctx.loop.services.core.redactor,
    )
    bundle = store.latest_for_session(session_id)
    if bundle is None:
        return _ok("  （没有 StateCheckpointBundle）")
    lines = [
        f"  bundle: {bundle.bundle_id}",
        f"  session: {bundle.session_id or '-'}",
        f"  turn: {bundle.turn_id or '-'} · task: {bundle.task_id or '-'}",
        f"  context: {bundle.context_checkpoint_id or '-'}",
        f"  compact: {bundle.compact_checkpoint_id or '-'}",
        f"  history: {(bundle.history_digest or '-')[:24]}",
        f"  working context sequence: {bundle.working_context_sequence}",
    ]
    if bundle.runtime_state is not None:
        lines.append(
            f"  runtime: {bundle.runtime_state.status.value}"
            f" · sequence {bundle.runtime_state.sequence}")
    return _ok(*lines)


__all__ = ["handle_state"]
