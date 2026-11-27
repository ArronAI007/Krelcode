"""Transactionally committed local state snapshots."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Mapping, Optional, Protocol

from krelcode.contracts.state import (
    StateCheckpointBundle,
    StateCheckpointStorePort,
    StateSnapshotStatus,
)

STATE_TRANSACTION_VERSION = 2
STATE_LEGACY_VERSION = 1


class StateStoreError(RuntimeError):
    """Base error for transactional state persistence."""


class StateCommitError(StateStoreError):
    """A transaction could not be made visible atomically."""


class StateIntegrityError(StateStoreError):
    """A committed transaction failed digest or manifest validation."""


class _RedactorPort(Protocol):
    def redact(self, text: str) -> str: ...


class LocalTransactionalStateStore(StateCheckpointStorePort):
    """Filesystem snapshot store with a commit-last manifest.

    A transaction is written into a private staging directory, fsynced, and
    exposed by renaming the complete directory.  The manifest is written last,
    so readers never observe a partially committed component set.
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        directory: Optional[Path] = None,
        redactor: Optional[_RedactorPort] = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.base = (
            Path(directory)
            if directory is not None
            else self.workspace_root / ".krelcode" / "state"
        )
        self.transactions = self.base / "transactions"
        self.index = self.base / "index"
        self.legacy = self.base / "checkpoints"
        self.redactor = redactor
        self.last_error = ""
        self._lock = threading.RLock()
        self.transactions.mkdir(parents=True, exist_ok=True)
        self.index.mkdir(parents=True, exist_ok=True)

    def commit(self, bundle: StateCheckpointBundle) -> StateCheckpointBundle:
        with self._lock:
            transaction_id = (
                bundle.transaction_id
                or f"txn-{time.time_ns()}-{uuid.uuid4().hex[:12]}"
            )
            sequence = bundle.sequence or self._next_sequence(bundle.session_id)
            parent_snapshot_id = (
                bundle.parent_snapshot_id
                or self._latest_bundle_id(bundle.session_id)
            )
            committed = bundle.with_commit(
                transaction_id=transaction_id,
                sequence=sequence,
                parent_snapshot_id=parent_snapshot_id,
            )
            if self.redactor is not None:
                redacted = _redact_mapping(
                    committed.as_dict(), self.redactor)
                redacted["component_digests"] = {}
                redacted["snapshot_digest"] = ""
                committed = StateCheckpointBundle.from_dict(
                    redacted).with_commit(
                        transaction_id=transaction_id,
                        sequence=sequence,
                        parent_snapshot_id=parent_snapshot_id,
                    )
            staging = self.transactions / (
                f".staging-{transaction_id}-{uuid.uuid4().hex[:8]}")
            destination = self.transactions / _safe_id(transaction_id)
            components = committed.effective_components()
            try:
                component_dir = staging / "components"
                component_dir.mkdir(parents=True, exist_ok=False)
                component_files: dict[str, dict[str, object]] = {}
                for name, data in sorted(components.items()):
                    filename = f"{_safe_component_name(name)}.json"
                    _write_json_file(
                        component_dir / filename,
                        data,
                        redactor=None,
                    )
                    component_files[name] = {
                        "file": filename,
                        "digest": committed.component_digests[name],
                    }
                snapshot_record = committed.as_dict()
                snapshot_record.pop("components", None)
                _write_json_file(
                    staging / "snapshot.json",
                    snapshot_record,
                    redactor=None,
                )
                manifest = {
                    "version": STATE_TRANSACTION_VERSION,
                    "type": "state_snapshot_transaction",
                    "transaction_id": transaction_id,
                    "bundle_id": committed.bundle_id,
                    "session_id": committed.session_id,
                    "sequence": committed.sequence,
                    "parent_snapshot_id": committed.parent_snapshot_id,
                    "created_at": committed.created_at,
                    "snapshot_digest": committed.snapshot_digest,
                    "components": component_files,
                }
                self._write_manifest(staging / "manifest.json", manifest)
                _fsync_directory(staging)
                if destination.exists():
                    raise StateCommitError(
                        f"state transaction 已存在: {transaction_id}")
                os.replace(staging, destination)
                _fsync_directory(self.transactions)
                self._write_index(committed)
            except Exception as exc:
                self._remove_staging(staging)
                if isinstance(exc, StateStoreError):
                    raise
                raise StateCommitError(
                    f"{type(exc).__name__}: {exc}") from exc
            self.last_error = ""
            return committed

    def save(self, bundle: StateCheckpointBundle) -> bool:
        try:
            self.commit(bundle)
        except (OSError, TypeError, ValueError, StateStoreError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        return True

    def load(self, bundle_id: str) -> Optional[StateCheckpointBundle]:
        identifier = _safe_id(bundle_id)
        transaction_path = self.transactions / identifier
        if transaction_path.is_dir():
            loaded = self._load_transaction(transaction_path)
            if loaded is not None:
                return loaded
        indexed = self._indexed_transaction(identifier)
        if indexed is not None:
            loaded = self._load_transaction(
                self.transactions / indexed)
            if loaded is not None:
                return loaded
        candidates = [
            bundle for bundle in self._transaction_bundles()
            if bundle.bundle_id == bundle_id
        ]
        if candidates:
            return max(candidates, key=lambda item: item.sequence)
        return self._load_legacy(bundle_id)

    def latest_for_session(
        self,
        session_id: str,
    ) -> Optional[StateCheckpointBundle]:
        candidates = [
            bundle for bundle in self._transaction_bundles()
            if not session_id or bundle.session_id == session_id
        ]
        legacy = self._legacy_bundles(session_id)
        candidates.extend(legacy)
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (item.created_at, item.sequence),
        )

    def _write_manifest(
        self,
        path: Path,
        manifest: Mapping[str, object],
    ) -> None:
        _write_json_file(path, manifest, redactor=self.redactor)

    def _write_index(self, bundle: StateCheckpointBundle) -> None:
        payload = {
            "bundle_id": bundle.bundle_id,
            "transaction_id": bundle.transaction_id,
            "session_id": bundle.session_id,
            "sequence": bundle.sequence,
            "created_at": bundle.created_at,
        }
        try:
            _write_json_file(
                self.index / f"{_safe_id(bundle.bundle_id)}.json",
                payload,
                redactor=self.redactor,
            )
        except Exception as exc:
            # The immutable transaction is already visible by directory scan;
            # the pointer only accelerates lookup.
            self.last_error = f"index: {type(exc).__name__}: {exc}"

    def _indexed_transaction(self, bundle_id: str) -> Optional[str]:
        path = self.index / f"{bundle_id}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, Mapping):
            return None
        transaction_id = str(raw.get("transaction_id") or "").strip()
        return _safe_id(transaction_id) if transaction_id else None

    def _next_sequence(self, session_id: str) -> int:
        sequences = [
            bundle.sequence
            for bundle in self._transaction_bundles()
            if bundle.session_id == session_id
        ]
        sequences.extend(
            bundle.sequence
            for bundle in self._legacy_bundles(session_id)
        )
        return max(sequences, default=0) + 1

    def _latest_bundle_id(self, session_id: str) -> str:
        latest = self.latest_for_session(session_id)
        return latest.bundle_id if latest is not None else ""

    def _transaction_bundles(self) -> list[StateCheckpointBundle]:
        bundles: list[StateCheckpointBundle] = []
        try:
            paths = sorted(self.transactions.iterdir())
        except OSError:
            return bundles
        for path in paths:
            if not path.is_dir() or path.name.startswith(".staging-"):
                continue
            bundle = self._load_transaction(path)
            if bundle is not None:
                bundles.append(bundle)
        return bundles

    def _load_transaction(
        self,
        path: Path,
    ) -> Optional[StateCheckpointBundle]:
        try:
            manifest = json.loads(
                (path / "manifest.json").read_text(encoding="utf-8"))
            snapshot_raw = json.loads(
                (path / "snapshot.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            not isinstance(manifest, Mapping)
            or not isinstance(snapshot_raw, Mapping)
            or manifest.get("version") != STATE_TRANSACTION_VERSION
            or manifest.get("type") != "state_snapshot_transaction"
        ):
            return None
        component_specs = manifest.get("components")
        if not isinstance(component_specs, Mapping):
            return None
        components: dict[str, Mapping[str, object]] = {}
        component_digests: dict[str, str] = {}
        try:
            for name, raw_spec in component_specs.items():
                if not isinstance(raw_spec, Mapping):
                    return None
                filename = str(raw_spec.get("file") or "")
                if not filename or Path(filename).name != filename:
                    return None
                data_raw = json.loads(
                    (path / "components" / filename).read_text(
                        encoding="utf-8"))
                if not isinstance(data_raw, Mapping):
                    return None
                components[str(name)] = dict(data_raw)
                component_digests[str(name)] = str(
                    raw_spec.get("digest") or "")
            snapshot = dict(snapshot_raw)
            snapshot["components"] = components
            snapshot["component_digests"] = component_digests
            snapshot["transaction_id"] = str(
                manifest.get("transaction_id") or "")
            snapshot["status"] = StateSnapshotStatus.COMMITTED.value
            snapshot["sequence"] = int(manifest.get("sequence") or 0)
            snapshot["parent_snapshot_id"] = str(
                manifest.get("parent_snapshot_id") or "")
            bundle = StateCheckpointBundle.from_dict(snapshot)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if bundle.snapshot_digest != str(
            manifest.get("snapshot_digest") or ""):
            return None
        return bundle

    def _load_legacy(
        self,
        bundle_id: str,
    ) -> Optional[StateCheckpointBundle]:
        path = self.legacy / f"{_safe_id(bundle_id)}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, Mapping):
            return None
        if (
            raw.get("version") != STATE_LEGACY_VERSION
            or raw.get("type") != "state_checkpoint_bundle"
        ):
            return None
        try:
            return StateCheckpointBundle.from_dict(raw)
        except (TypeError, ValueError):
            return None

    def _legacy_bundles(
        self,
        session_id: str,
    ) -> list[StateCheckpointBundle]:
        if not self.legacy.is_dir():
            return []
        bundles: list[StateCheckpointBundle] = []
        for path in sorted(self.legacy.glob("*.json")):
            bundle = self._load_legacy(path.stem)
            if bundle is not None and (
                not session_id or bundle.session_id == session_id
            ):
                bundles.append(bundle)
        return bundles

    @staticmethod
    def _remove_staging(path: Path) -> None:
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError:
            pass


def _write_json_file(
    path: Path,
    payload: Mapping[str, object],
    *,
    redactor: Optional[_RedactorPort],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ) + "\n"
    if redactor is not None:
        line = redactor.redact(line)
        json.loads(line)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _redact_mapping(
    payload: Mapping[str, object],
    redactor: _RedactorPort,
) -> dict[str, object]:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    redacted = redactor.redact(rendered)
    try:
        decoded = json.loads(redacted)
    except json.JSONDecodeError:
        return {
            "bundle_id": str(payload.get("bundle_id") or ""),
            "metadata": {"redaction_error": True},
        }
    return dict(decoded) if isinstance(decoded, Mapping) else {}


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


def _safe_id(value: str) -> str:
    safe = "".join(
        char for char in str(value)
        if char.isalnum() or char in {"-", "_", "."}
    )
    return safe or "state-snapshot"


def _safe_component_name(value: str) -> str:
    safe = "".join(
        char for char in str(value)
        if char.isalnum() or char in {"-", "_", "."}
    ).strip(".")
    return safe or f"component-{uuid.uuid4().hex[:12]}"


__all__ = [
    "LocalTransactionalStateStore",
    "LocalStateStore",
    "STATE_LEGACY_VERSION",
    "STATE_TRANSACTION_VERSION",
    "StateCommitError",
    "StateIntegrityError",
    "StateStoreError",
]

LocalStateStore = LocalTransactionalStateStore
