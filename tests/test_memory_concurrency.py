"""Cross-process and optimistic concurrency guarantees for memory writes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from krelcode.session.memory import MemoryRevisionConflict, MemoryStore
from krelcode.session.memory_candidate_models import (
    MemoryCandidateDraft,
    MemoryCandidateTrigger,
)
from krelcode.session.memory_candidate_queue import (
    MemoryCandidateQueue,
    MemoryCandidateRevisionConflict,
)


def test_memory_optimistic_revision_rejects_stale_writer(tmp_path: Path) -> None:
    first = MemoryStore(tmp_path, user_path=tmp_path / "user.jsonl")
    second = MemoryStore(tmp_path, user_path=tmp_path / "user.jsonl")

    assert first.revision() == 0
    entry = first.add("项目测试命令是 pytest", expected_revision=0)
    assert first.revision() == 1
    assert entry.revision == 1
    assert entry.writer_id

    with pytest.raises(MemoryRevisionConflict) as caught:
        second.add("并发写入", expected_revision=0)
    assert caught.value.expected == 0
    assert caught.value.actual == 1

    second.add("后续写入", expected_revision=1)
    assert first.revision() == 2


def test_memory_store_serializes_writers_across_processes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = dict(os.environ)
    source = str(Path(__file__).parents[1] / "src")
    env["PYTHONPATH"] = (
        source
        + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    )
    code = (
        "import sys;"
        "from pathlib import Path;"
        "from krelcode.session.memory import MemoryStore;"
        f"s=MemoryStore(Path({str(workspace)!r}),"
        f"user_path=Path({str(tmp_path / 'user.jsonl')!r}));"
        "i=sys.argv[1];"
        "[s.add(i + ':' + str(j)) for j in range(8)]"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(index)],
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(4)
    ]
    failures = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        if process.returncode != 0:
            failures.append((process.returncode, stdout, stderr))
    assert failures == []

    store = MemoryStore(
        workspace, user_path=tmp_path / "user.jsonl")
    entries = store.list(limit=0, scopes=("project",))
    assert len(entries) == 32
    assert store.revision("project") == 32
    assert len({entry.id for entry in entries}) == 32


def test_candidate_queue_revision_conflict(tmp_path: Path) -> None:
    queue = MemoryCandidateQueue(tmp_path)
    draft = MemoryCandidateDraft(
        text="项目使用 asyncio 事件循环",
        trigger=MemoryCandidateTrigger.EXPLICIT_REMEMBER,
        confidence=0.9,
    )
    candidate, created = queue.merge(draft, expected_revision=0)
    assert created is True and candidate is not None
    assert queue.revision() == 1

    with pytest.raises(MemoryCandidateRevisionConflict, match="revision conflict"):
        queue.merge(draft, expected_revision=0)
