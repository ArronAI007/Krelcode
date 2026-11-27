"""Append-only event log sequence and hash-chain behavior."""

from __future__ import annotations

import json
from pathlib import Path

from krelcode.storage.event_log import EVENT_LOG_META_KEY, LocalEventLog


def test_event_log_sequence_and_hash_chain(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = LocalEventLog(path)

    first = log.append({"type": "start", "ts": 1.0})
    second = log.append({"type": "finish", "ts": 2.0})

    entries = log.read()
    assert first.sequence == 1
    assert second.sequence == 2
    assert entries[1].previous_hash == entries[0].digest
    assert entries[1].event_type == "finish"
    assert log.latest_sequence == 2


def test_event_log_recovers_after_malformed_tail(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"type":"legacy","ts":1}\n{"broken"', encoding="utf-8")

    log = LocalEventLog(path)
    appended = log.append({"type": "new", "ts": 2.0})

    assert appended.sequence == 2
    assert [entry.event_type for entry in log.read()] == ["legacy", "new"]
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[-1])[EVENT_LOG_META_KEY]["sequence"] == 2


def test_event_log_ignores_corrupt_middle_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"type":"one"}\nnot-json\n{"type":"two"}\n',
        encoding="utf-8",
    )

    entries = LocalEventLog(path).read()

    assert [entry.event_type for entry in entries] == ["one", "two"]
    assert entries[1].previous_hash == entries[0].digest
