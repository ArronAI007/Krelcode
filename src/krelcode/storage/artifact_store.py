"""Local content-addressed Artifact Store.

Artifacts are immutable deliverable bytes.  They are deliberately separate
from Workspace files, Event Log records and state snapshots: a workspace can be
edited, an event log is append-only, and a state snapshot is a recovery point.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Mapping, Optional, Sequence

from krelcode.contracts.artifact import ArtifactRef, ArtifactStorePort


class ArtifactStoreError(RuntimeError):
    """Base error for local artifact persistence."""


class ArtifactNotFound(ArtifactStoreError, KeyError):
    """The requested content address is absent."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Stored bytes no longer match their content address."""


class LocalArtifactStore(ArtifactStorePort):
    """Content-addressed local artifact store with atomic metadata writes."""

    def __init__(
        self,
        root: Path,
        *,
        blobs_directory: str = "blobs",
        metadata_directory: str = "metadata",
    ) -> None:
        self.root = Path(root)
        self.blobs = self.root / blobs_directory
        self.metadata = self.root / metadata_directory
        self._lock = threading.RLock()
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.metadata.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        name: str = "",
        metadata: Optional[Mapping[str, object]] = None,
    ) -> ArtifactRef:
        payload = bytes(data)
        digest = hashlib.sha256(payload).hexdigest()
        artifact_id = f"sha256:{digest}"
        blob_path = self._blob_path(digest)
        metadata_path = self._metadata_path(digest)
        with self._lock:
            if not blob_path.is_file():
                _atomic_write_bytes(blob_path, payload)
            else:
                existing = blob_path.read_bytes()
                if hashlib.sha256(existing).hexdigest() != digest:
                    _atomic_write_bytes(blob_path, payload)
            ref = ArtifactRef(
                artifact_id=artifact_id,
                digest=digest,
                media_type=media_type,
                size=len(payload),
                name=name,
                created_at=float(
                    metadata_path.stat().st_mtime
                    if metadata_path.exists() else 0.0
                ) or _now(),
                metadata=dict(metadata or {}),
            )
            _atomic_write_json(metadata_path, ref.as_dict())
            return ref

    def get(self, artifact_id: str) -> bytes:
        digest = _digest_from_id(artifact_id)
        with self._lock:
            path = self._blob_path(digest)
            try:
                payload = path.read_bytes()
            except FileNotFoundError as exc:
                raise ArtifactNotFound(artifact_id) from exc
            if hashlib.sha256(payload).hexdigest() != digest:
                raise ArtifactIntegrityError(
                    f"artifact integrity check failed: {artifact_id}")
            return payload

    def stat(self, artifact_id: str) -> ArtifactRef:
        digest = _digest_from_id(artifact_id)
        with self._lock:
            blob_path = self._blob_path(digest)
            if not blob_path.is_file():
                raise ArtifactNotFound(artifact_id)
            metadata = self._read_metadata(digest)
            if metadata is None:
                return ArtifactRef(
                    artifact_id=f"sha256:{digest}",
                    digest=digest,
                    size=blob_path.stat().st_size,
                    created_at=blob_path.stat().st_mtime,
                )
            return metadata

    def list(self, *, limit: int = 100) -> Sequence[ArtifactRef]:
        if limit < 1:
            return ()
        refs: list[ArtifactRef] = []
        with self._lock:
            paths = sorted(
                self.metadata.glob("*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for path in paths[:int(limit)]:
                ref = _read_ref(path)
                if ref is not None:
                    refs.append(ref)
        return tuple(refs)

    def delete(self, artifact_id: str) -> bool:
        digest = _digest_from_id(artifact_id)
        with self._lock:
            deleted = False
            for path in (
                self._metadata_path(digest),
                self._blob_path(digest),
            ):
                try:
                    path.unlink()
                    deleted = True
                except FileNotFoundError:
                    continue
            return deleted

    def _read_metadata(self, digest: str) -> Optional[ArtifactRef]:
        path = self._metadata_path(digest)
        ref = _read_ref(path)
        if ref is None:
            return None
        if ref.digest != digest:
            raise ArtifactIntegrityError(
                f"artifact metadata digest mismatch: {path.name}")
        return ref

    def _blob_path(self, digest: str) -> Path:
        return self.blobs / digest

    def _metadata_path(self, digest: str) -> Path:
        return self.metadata / f"{digest}.json"


def _digest_from_id(artifact_id: str) -> str:
    raw = str(artifact_id or "").strip()
    digest = raw.removeprefix("sha256:")
    if (
        len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest.lower())
    ):
        raise ArtifactNotFound(artifact_id)
    return digest.lower()


def _read_ref(path: Path) -> Optional[ArtifactRef]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, Mapping):
        return None
    try:
        return ArtifactRef.from_dict(raw)
    except (TypeError, ValueError):
        return None


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    data = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ) + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(path, data)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _now() -> float:
    import time
    return time.time()


__all__ = [
    "ArtifactIntegrityError",
    "ArtifactNotFound",
    "ArtifactStoreError",
    "LocalArtifactStore",
]
