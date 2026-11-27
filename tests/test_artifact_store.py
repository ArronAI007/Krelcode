"""Content-addressed artifact persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from krelcode.storage.artifact_store import (
    ArtifactIntegrityError,
    ArtifactNotFound,
    LocalArtifactStore,
)


def test_artifact_store_is_content_addressed_and_deduplicated(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    first = store.put(
        b"hello world",
        media_type="text/plain",
        name="hello.txt",
        metadata={"tenant": "local"},
    )
    second = store.put(b"hello world", name="duplicate.txt")

    assert first.artifact_id == second.artifact_id
    assert store.get(first.artifact_id) == b"hello world"
    assert store.stat(first.artifact_id).digest == first.digest
    assert len(store.list()) == 1
    assert store.delete(first.artifact_id)
    with pytest.raises(ArtifactNotFound):
        store.get(first.artifact_id)


def test_artifact_store_rejects_traversal_and_detects_tampering(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = store.put(b"immutable")

    with pytest.raises(ArtifactNotFound):
        store.get("../../etc/passwd")

    (tmp_path / "artifacts" / "blobs" / ref.digest).write_bytes(b"changed")
    with pytest.raises(ArtifactIntegrityError):
        store.get(ref.artifact_id)

    (tmp_path / "artifacts" / "blobs" / ref.digest).unlink()
    with pytest.raises(ArtifactNotFound):
        store.stat(ref.artifact_id)
