"""Transactional State Snapshot commit semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from krelcode.contracts.state import (
    StateCheckpointBundle,
    StateSnapshotStatus,
)
from krelcode.core.redact import SecretRedactor
from krelcode.storage.state_store import (
    LocalTransactionalStateStore,
    StateCommitError,
)


def _bundle(bundle_id: str = "snapshot-1") -> StateCheckpointBundle:
    return StateCheckpointBundle(
        bundle_id=bundle_id,
        session_id="session-1",
        turn_id="turn-1",
        created_at=100.0,
        components={
            "context": {"checkpoint_id": "ctx-1", "sequence": 3},
            "workspace": {"stale_files": []},
            "runtime": {"status": "running"},
        },
    )


def test_state_store_commits_all_components_atomically(
    tmp_path: Path,
) -> None:
    store = LocalTransactionalStateStore(tmp_path)

    committed = store.commit(_bundle())
    loaded = store.load(committed.bundle_id)

    assert committed.status is StateSnapshotStatus.COMMITTED
    assert committed.transaction_id
    assert committed.snapshot_digest
    assert loaded is not None
    assert loaded.status is StateSnapshotStatus.COMMITTED
    assert loaded.sequence == 1
    assert dict(loaded.components["context"])["checkpoint_id"] == "ctx-1"
    assert store.latest_for_session("session-1") == loaded


def test_state_store_manifest_failure_is_not_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = LocalTransactionalStateStore(tmp_path)

    def fail_manifest(
        path: Path,
        manifest: object,
    ) -> None:
        raise OSError("simulated manifest failure")

    monkeypatch.setattr(store, "_write_manifest", fail_manifest)
    with pytest.raises(StateCommitError):
        store.commit(_bundle())
    assert store.load("snapshot-1") is None
    assert store.latest_for_session("session-1") is None


def test_state_store_rejects_tampered_component(tmp_path: Path) -> None:
    store = LocalTransactionalStateStore(tmp_path)
    committed = store.commit(_bundle())
    component = (
        store.transactions
        / committed.transaction_id
        / "components"
        / "context.json"
    )
    component.write_text('{"checkpoint_id":"tampered"}\n', encoding="utf-8")

    assert store.load(committed.bundle_id) is None


def test_state_store_advances_session_sequence(tmp_path: Path) -> None:
    store = LocalTransactionalStateStore(tmp_path)

    first = store.commit(_bundle("snapshot-1"))
    second = store.commit(_bundle("snapshot-2"))

    assert first.sequence == 1
    assert second.sequence == 2
    assert second.parent_snapshot_id == "snapshot-1"


def test_state_store_redacts_before_computing_snapshot_digest(
    tmp_path: Path,
) -> None:
    store = LocalTransactionalStateStore(
        tmp_path,
        redactor=SecretRedactor(["sk-secret-123"]),
    )
    bundle = StateCheckpointBundle(
        bundle_id="redacted",
        session_id="session-1",
        components={
            "runtime": {"error": "failed with sk-secret-123"},
        },
    )

    committed = store.commit(bundle)
    loaded = store.load(committed.bundle_id)

    assert loaded is not None
    assert loaded.snapshot_digest == committed.snapshot_digest
    assert "sk-secret-123" not in str(loaded.components)
