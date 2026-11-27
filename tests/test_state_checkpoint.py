"""Unified state checkpoint bundle persistence."""

from __future__ import annotations

from pathlib import Path

from krelcode.contracts.runtime import RuntimeTaskState, RuntimeTaskStatus
from krelcode.contracts.state import StateCheckpointBundle
from krelcode.session.state_checkpoint import LocalStateCheckpointStore


def test_state_checkpoint_bundle_round_trip(tmp_path: Path) -> None:
    store = LocalStateCheckpointStore(tmp_path)
    bundle = StateCheckpointBundle(
        bundle_id="state-test",
        session_id="session-1",
        turn_id="turn-1",
        task_id="task-1",
        context_checkpoint_id="ctx-1",
        compact_checkpoint_id="compact-1",
        working_context_sequence=4,
        history_digest="sha256:history",
        plan={"status": "in_progress"},
        todos={"items": ["test"]},
        runtime_state=RuntimeTaskState(
            task_id="task-1",
            turn_id="turn-1",
            status=RuntimeTaskStatus.RUNNING,
            sequence=3,
            updated_at=123.0,
        ),
    )

    assert store.save(bundle)
    loaded = store.load("state-test")
    assert loaded is not None
    assert loaded.context_checkpoint_id == "ctx-1"
    assert loaded.plan["status"] == "in_progress"
    assert loaded.runtime_state is not None
    assert loaded.runtime_state.status is RuntimeTaskStatus.RUNNING
    assert store.latest_for_session("session-1") is not None
