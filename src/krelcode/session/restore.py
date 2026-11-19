"""Session history restoration with full context-checkpoint support."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..agent.fragments import refresh_registry_snapshot, skills_fragment
from ..agent.loop import AgentLoop
from ..agent.prompts import build_system_prompt
from ..core.messages import Message
from .context_checkpoint import restore_context_checkpoint
from .recorder import (
    load_history,
    load_latest_context_checkpoint_id,
)


def refresh_skill_system_prompt(loop: AgentLoop) -> None:
    """热重载/恢复会话后，只替换受影响的 catalog attachment。"""
    manager = getattr(loop, "skill_manager", None)
    sync = getattr(loop, "sync_context_attachments", None)
    if callable(sync):
        sync(
            include_environment=False,
            include_catalogs=True,
            include_mcp=False,
            include_reminders=False,
        )
        return
    # Compatibility for third-party loops without an attachment store.
    registry = getattr(loop, "prompt_registry", None)
    if registry is not None:
        if manager is not None:
            fragment = skills_fragment(manager.prompt_index())
            if fragment is None:
                registry.remove("skills:index")
            else:
                registry.upsert(fragment)
        refresh_registry_snapshot(loop)
        return
    # 兼容手工构造的旧 AgentLoop（没有 PromptFragmentRegistry）。
    if manager is None:
        return
    base = getattr(loop, "skill_base_prompt", None)
    if base is None:
        return
    prompt = manager.apply_to_prompt(base)
    if loop.history and loop.history[0].role == "system":
        loop.history[0].content = prompt
    elif not loop.history:
        loop.history.append(Message.system(prompt))


def restore_session(loop: AgentLoop, path: Path, log=print) -> bool:
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

    store = getattr(loop, "context_checkpoint_store", None)
    checkpoint = None
    if store is not None:
        checkpoint_id = load_latest_context_checkpoint_id(path)
        if checkpoint_id:
            checkpoint = store.load(checkpoint_id)
        if checkpoint is None:
            checkpoint = store.latest_for_session(path.stem)

    history = load_history(path)
    memory_store = getattr(loop, "memory_store", None)
    if checkpoint is not None:
        if memory_store is not None:
            # Retrieval must see the source session's scoped memory before the
            # resumed copy-on-write branch receives a new session id.
            memory_store.bind_session(path.stem)
        result = restore_context_checkpoint(
            loop,
            checkpoint,
            compact_store=getattr(loop, "compact_checkpoint_store", None),
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
        history.insert(0, Message.system(build_system_prompt(
            profile=getattr(loop, "prompt_profile_config", "auto"),
            provider=getattr(loop, "llm", ""),
            model=getattr(getattr(loop, "llm", None), "model", ""),
        )))
    loop.history = history
    loop.rebuild_working_context()
    loop.pending_context_refresh = True
    _refresh_now_if_possible(loop)
    refresh_skill_system_prompt(loop)
    log(f"（已恢复会话 {path}，共 {len(history)} 条消息；旧格式无 ContextCheckpoint）")
    return True


def _refresh_now_if_possible(loop: AgentLoop) -> None:
    refresh = getattr(loop, "refresh_workspace_context", None)
    if not callable(refresh):
        loop.pending_context_refresh = False
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(refresh())
        except Exception:
            loop.pending_context_refresh = True
    else:
        loop.pending_context_refresh = True


__all__ = ["refresh_skill_system_prompt", "restore_session"]
