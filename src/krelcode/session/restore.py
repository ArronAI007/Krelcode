"""Session history restoration with full context-checkpoint support."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, cast

from ..agent.fragments import PromptSnapshot, refresh_registry_snapshot, skills_fragment
from ..agent.context.context_checkpoint_ports import (
    CheckpointLoopPort,
)
from ..agent.prompt.prompt_snapshot_ports import PromptSnapshotHostPort
from ..agent.prompts import build_system_prompt
from ..agent.cyber_risk import prepend_cyber_risk_prompt
from ..core.messages import Message
from .context_checkpoint import restore_context_checkpoint
from .recorder import (
    load_history,
    load_latest_context_checkpoint_id,
)


def refresh_skill_system_prompt(loop: CheckpointLoopPort) -> None:
    """热重载/恢复会话后，只替换受影响的 catalog attachment。"""
    services = loop.services
    manager = services.extensions.skills
    loop.sync_context_attachments(
        include_environment=False,
        include_catalogs=True,
        include_mcp=False,
        include_reminders=False,
    )
    # Compatibility for third-party loops without an attachment store.
    registry = services.prompt.registry
    if registry is not None:
        if manager is not None:
            fragment = skills_fragment(manager.prompt_index())
            if fragment is None:
                registry.remove("skills:index")
            else:
                registry.upsert(fragment)
        refresh_registry_snapshot(
            cast(PromptSnapshotHostPort[PromptSnapshot, Any], loop))
        return
    # 兼容手工构造的旧 AgentLoop（没有 PromptFragmentRegistry）。
    if manager is None:
        return
    prompt = manager.apply_to_prompt(loop.skill_base_prompt)
    if loop.history and loop.history[0].role == "system":
        loop.history[0].content = prompt
    elif not loop.history:
        loop.history.append(Message.system(prompt))


def restore_session(
    loop: CheckpointLoopPort,
    path: Path,
    log: Callable[[str], None] = print,
) -> bool:
    """Restore history and the latest full-context checkpoint when available.

    A legacy session without ContextCheckpoint still restores its last history
    snapshot. A checkpointed session additionally restores compact state,
    Working Context, task-filtered memory and file-integrity evidence. Git,
    tree and rules are always re-read from the current workspace.
    """
    path = Path(path)
    if not path.is_file():
        log(f"（会话文件不存在: {path}）")
        return False

    services = loop.services
    store = services.context.checkpoint
    checkpoint = None
    if store is not None:
        checkpoint_id = load_latest_context_checkpoint_id(path)
        if checkpoint_id:
            checkpoint = store.load(checkpoint_id)
        if checkpoint is None:
            checkpoint = store.latest_for_session(path.stem)

    history = load_history(path)
    memory_store = services.session.memory
    if checkpoint is not None:
        if memory_store is not None:
            # Retrieval must see the source session's scoped memory before the
            # resumed copy-on-write branch receives a new session id.
            memory_store.bind_session(path.stem)
        result = restore_context_checkpoint(
            loop,
            checkpoint,
            compact_store=services.context.compact_checkpoint,
            memory_store=memory_store,
            history_override=checkpoint.history or history,
        )
        refresh_skill_system_prompt(loop)
        stale = result.integrity.stale
        if stale:
            log(
                "（文件状态已变化，旧上下文事实已失效: "
                + ", ".join(stale[:6])
                + ("…" if len(stale) > 6 else "")
                + "）")
        log(
            f"（已恢复会话 {path}：{result.history_messages} 条消息"
            f" · context {result.checkpoint_id}"
            f" · compact {result.compact_checkpoint_id or '无'}"
            f" · working {result.working_context_entries} 条）")
        return True

    if not history:
        log(f"（会话 {path} 中没有历史快照）")
        return False
    if history[0].role != "system":
        history.insert(0, Message.system(prepend_cyber_risk_prompt(
            build_system_prompt(
                profile=str(loop.prompt_profile_config or "auto"),
                provider=services.core.llm,
                model=getattr(services.core.llm, "model", ""),
            ))))
    loop.update_runtime_state(
        history=history,
        pending_context_refresh=True,
    )
    loop.rebuild_working_context()
    _refresh_now_if_possible(loop)
    refresh_skill_system_prompt(loop)
    log(f"（已恢复会话 {path}，共 {len(history)} 条消息；旧格式无 ContextCheckpoint）")
    return True


def _refresh_now_if_possible(loop: CheckpointLoopPort) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(loop.refresh_workspace_context())
        except Exception:
            loop.update_runtime_state(pending_context_refresh=True)
    else:
        loop.update_runtime_state(pending_context_refresh=True)


__all__ = ["refresh_skill_system_prompt", "restore_session"]
