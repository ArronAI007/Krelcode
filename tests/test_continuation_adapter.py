"""Session continuation adapter maps runtime refs to live context state."""

from __future__ import annotations

from pathlib import Path

from krelcode.agent.context.context_checkpoint import ContextCheckpointStore
from krelcode.agent.loop import AgentLoop
from krelcode.contracts.runtime import RuntimeCheckpointRef
from krelcode.core.messages import Message
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.continuation import SessionContinuationAdapter
from krelcode.session.state_checkpoint import LocalStateCheckpointStore
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def test_continuation_adapter_restores_checkpoint_ref(tmp_path: Path) -> None:
    loop = AgentLoop(
        llm=MockProvider(scripted=["unused"]),
        registry=ToolRegistry(),
        tool_context=ToolContext(
            workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )
    store = ContextCheckpointStore(tmp_path)
    checkpoint = store.record(
        history=[
            Message.system("SYS"),
            Message.user("original"),
            Message.assistant("answer"),
        ],
        session_id="session-1",
        turn_id="turn-1",
    )
    assert checkpoint is not None

    adapter = SessionContinuationAdapter(
        loop,
        context_checkpoints=store,
    )
    result = adapter.resume(RuntimeCheckpointRef(
        checkpoint_id=checkpoint.id,
        kind="context",
        session_id="session-1",
        turn_id="turn-1",
    ))

    assert result.checkpoint_id == checkpoint.id
    assert result.state_bundle_id.startswith("state-")
    assert result.history_messages == 3
    assert [message.content for message in loop.history] == [
        "SYS", "original", "answer"]
    assert loop.last_context_restore is not None
    state_store = LocalStateCheckpointStore(tmp_path)
    bundle = state_store.load(result.state_bundle_id)
    assert bundle is not None
    assert bundle.context_checkpoint_id == checkpoint.id
    assert bundle.history_digest
