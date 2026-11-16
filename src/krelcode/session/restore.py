"""会话历史恢复：供启动参数和会话控制层共用。"""

from __future__ import annotations

from pathlib import Path

from ..agent.fragments import refresh_registry_snapshot, skills_fragment
from ..agent.loop import AgentLoop
from ..agent.prompts import build_system_prompt
from ..core.messages import Message
from .recorder import load_history


def refresh_skill_system_prompt(loop: AgentLoop) -> None:
    """热重载/恢复会话后，把最新 Skill 索引同步到 Prompt Registry。"""
    manager = getattr(loop, "skill_manager", None)
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
    """把会话历史装入 loop（REPL /resume 命令与 --resume 参数共用）。"""
    if not Path(path).is_file():
        log(f"（会话文件不存在: {path}）")
        return False
    history = load_history(Path(path))
    if not history:
        log(f"（会话 {path} 中没有历史快照）")
        return False
    if history[0].role != "system":
        history.insert(0, Message.system(build_system_prompt()))
    loop.history = history
    refresh_skill_system_prompt(loop)
    log(f"（已恢复会话 {path}，共 {len(history)} 条消息）")
    return True
