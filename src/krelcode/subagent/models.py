"""Stable subagent runtime models and role prompt."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from ..agent.turn.loop import AgentLoop
from ..agent.prompts import PROMPT_PROFILE_AUTO, build_system_prompt
from ..contracts.application import ApplicationRequest
from ..contracts.agent import AgentContract
from .control import AgentControl
from .contracts import STATUS_RUNNING
from .feed import AgentFeed
from .sidechain import AgentSidechainWriter

SUBAGENT_ROLE_PROMPT = """\
You are a Krelcode SUBAGENT working on a delegated subtask.
- Focus strictly on the task you were given; do not ask the user questions.
- Use tools to do the work and verify results when possible.
- Your final text message is the report handed back to the parent agent: make it
  dense and self-contained (what was done, key results, file paths, caveats)."""


def build_subagent_system_prompt(
    *,
    profile: str = PROMPT_PROFILE_AUTO,
    provider: object = "",
    model: object = "",
) -> str:
    return build_system_prompt(
        SUBAGENT_ROLE_PROMPT,
        profile=profile,
        provider=provider,
        model=model,
    )


SUBAGENT_SYSTEM_PROMPT = build_subagent_system_prompt()

PREVIEW_CHARS = 160

INTERRUPT_MARKER = "\n[被打断，用户新指示] "


@dataclass
class AgentHandle:
    id: str
    task: str
    loop: AgentLoop
    started_at: float = field(default_factory=time.time)
    status: str = STATUS_RUNNING
    result: Optional[str] = None
    error: Optional[str] = None
    turns: int = 0
    queue: List[str] = field(default_factory=list)
    queued_application_requests: List[Optional[ApplicationRequest]] = field(
        default_factory=list, repr=False)
    task_obj: Optional[asyncio.Task[None]] = field(default=None, repr=False)
    worktree: Optional[Path] = None
    branch: Optional[str] = None
    forked: bool = False
    definition: str = ""
    feed: Optional[AgentFeed] = field(default=None, repr=False)
    control: AgentControl = field(default_factory=AgentControl, repr=False)
    sidechain_path: Optional[Path] = None
    sidechain: Optional[AgentSidechainWriter] = field(default=None, repr=False)
    context_checkpoint_id: str = ""
    inherited_memory_ids: Tuple[str, ...] = field(default_factory=tuple)
    instruction_layers: Tuple[str, ...] = field(default_factory=tuple)
    model: str = ""
    provider: str = ""
    prompt_profile: str = ""
    permission_mode: str = ""
    mcp_servers: Tuple[str, ...] = field(default_factory=tuple)
    kill_requested: bool = field(default=False, repr=False)
    cache_ledger_hash: str = ""
    application_request: Optional[ApplicationRequest] = None
    agent_contract: Optional[AgentContract] = None
    global_slot_acquired: bool = False

    def preview(self) -> str:
        text = self.result or self.error or ""
        if len(text) > PREVIEW_CHARS:
            text = text[:PREVIEW_CHARS] + "…"
        return text.replace("\n", " ")


__all__ = [
    "AgentHandle",
    "INTERRUPT_MARKER",
    "PREVIEW_CHARS",
    "SUBAGENT_ROLE_PROMPT",
    "SUBAGENT_SYSTEM_PROMPT",
    "build_subagent_system_prompt",
]
