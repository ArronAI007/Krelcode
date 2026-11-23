"""ripgrep acceleration with Python-equivalent security and fallback tests."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

import pytest

from krelcode.tools import workspace_search
from krelcode.tools.base import ToolContext, TurnAbortSignal
from krelcode.tools.builtin.grep import GrepTool
from krelcode.tools.workspace_search import (
    RipgrepExecutionError,
    WorkspaceSearcher,
    _ripgrep_pattern_compatible,
)


RG_AVAILABLE = shutil.which("rg") is not None
skip_without_rg = pytest.mark.skipif(not RG_AVAILABLE, reason="ripgrep is not installed")


def run(coro):
    return asyncio.run(coro)


async def search(
    searcher: WorkspaceSearcher,
    root: Path,
    pattern: str,
    *,
    include: str | None = None,
    ignore_case: bool = False,
    max_results: int = 100,
):
    flags = re.IGNORECASE if ignore_case else 0
    return await searcher.search(
        pattern=pattern,
        regex=re.compile(pattern, flags),
        base=root,
        workspace_root=root,
        include=include,
        ignore_case=ignore_case,
        max_results=max_results,
        abort=TurnAbortSignal(),
    )


def test_python_backend_is_sorted_and_supports_include(tmp_path):
    (tmp_path / "b.py").write_text("first\nneedle\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("needle\n", encoding="utf-8")

    batch = run(search(
        WorkspaceSearcher(backend="python"),
        tmp_path,
        "needle",
        include="*.py",
    ))

    assert batch.backend == "python"
    assert [(item.path, item.line) for item in batch.matches] == [
        ("a.py", 1),
        ("b.py", 2),
    ]
    assert batch.files_scanned == 2


@skip_without_rg
def test_forced_ripgrep_backend_reports_real_backend(tmp_path):
    (tmp_path / "app.py").write_text("alpha\nneedle\n", encoding="utf-8")
    result = run(GrepTool(backend="ripgrep").execute(
        {"pattern": "needle"}, ToolContext(workspace_root=tmp_path)))

    assert not result.is_error
    assert result.data["backend"] == "ripgrep"
    assert "app.py:2: needle" in result.content


def test_auto_backend_falls_back_when_ripgrep_is_missing(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(workspace_search.shutil, "which", lambda _: None)

    batch = run(search(WorkspaceSearcher(backend="auto"), tmp_path, "needle"))

    assert batch.backend == "python"
    assert [item.path for item in batch.matches] == ["app.py"]


@pytest.mark.parametrize(
    "pattern",
    [
        r"(?=needle)",
        r"(?<!-)(needle)",
        r"(a)\1",
        r"(?P<word>needle)(?P=word)",
        r"(?>needle)",
        r"needle\Z",
        r"a++",
    ],
)
def test_python_only_regex_is_not_sent_to_ripgrep(pattern):
    assert _ripgrep_pattern_compatible(pattern) is False


def test_python_only_regex_runs_on_python_without_spawning_rg(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("needle-value\n", encoding="utf-8")

    async def unexpected_ripgrep(**_kwargs):
        raise AssertionError("Python-only regex must not reach ripgrep")

    monkeypatch.setattr(workspace_search, "_ripgrep_search", unexpected_ripgrep)
    batch = run(search(
        WorkspaceSearcher(backend="auto"),
        tmp_path,
        r"(?<=needle)-value",
    ))
    assert batch.backend == "python"
    assert [item.path for item in batch.matches] == ["app.py"]


def test_ripgrep_execution_error_falls_back_without_changing_results(
    tmp_path, monkeypatch,
):
    (tmp_path / "app.py").write_text("needle\n", encoding="utf-8")

    async def broken_ripgrep(**_kwargs):
        raise RipgrepExecutionError("simulated backend failure")

    monkeypatch.setattr(workspace_search, "_ripgrep_search", broken_ripgrep)
    batch = run(search(WorkspaceSearcher(backend="auto"), tmp_path, "needle"))

    assert batch.backend == "python"
    assert [item.path for item in batch.matches] == ["app.py"]


@skip_without_rg
def test_missing_ripgrep_process_falls_back_to_python(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("needle\n", encoding="utf-8")

    async def missing_process(*_args, **_kwargs):
        raise FileNotFoundError("rg disappeared")

    monkeypatch.setattr(
        workspace_search.asyncio, "create_subprocess_exec", missing_process)
    batch = run(search(WorkspaceSearcher(backend="auto"), tmp_path, "needle"))

    assert batch.backend == "python"
    assert [item.path for item in batch.matches] == ["app.py"]


@skip_without_rg
def test_ripgrep_handles_and_truncates_a_long_matching_line(tmp_path):
    (tmp_path / "long.txt").write_text(
        "needle" + ("x" * 100_000) + "\n", encoding="utf-8")

    batch = run(search(
        WorkspaceSearcher(backend="ripgrep"), tmp_path, "needle"))

    assert batch.backend == "ripgrep"
    assert len(batch.matches) == 1
    assert batch.matches[0].text.startswith("needlex")
    assert batch.matches[0].text.endswith("…")
    assert len(batch.matches[0].text) < 400


def test_ripgrep_path_preserves_file_security_boundary(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("needle-outside\n", encoding="utf-8")

    (root / "normal.txt").write_text("needle-safe\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=needle-env\n", encoding="utf-8")
    (root / "credentials.json").write_text(
        '{"token": "needle-credentials"}\n', encoding="utf-8")
    (root / "krelcode.toml").write_text(
        'api_key = "needle-toml"\n', encoding="utf-8")
    (root / "binary.dat").write_bytes(b"needle-binary\x00tail\n")
    os.symlink(outside, root / "outside-link.txt")
    os.link(outside, root / "outside-hardlink.txt")

    result = run(GrepTool(backend="ripgrep").execute(
        {"pattern": "needle", "max_results": 50},
        ToolContext(workspace_root=root),
    ))

    assert not result.is_error
    assert "normal.txt" in result.content
    for secret in (
        ".env", "credentials.json", "krelcode.toml", "binary.dat",
        "outside-link.txt", "outside-hardlink.txt", "outside.txt",
    ):
        assert secret not in result.content
    assert "needle-env" not in result.content
    assert "needle-credentials" not in result.content
    assert "needle-toml" not in result.content


def test_search_honors_case_include_and_result_limit(tmp_path):
    (tmp_path / "many.py").write_text("Hit\nHIT\nhit\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("HIT\n", encoding="utf-8")

    batch = run(search(
        WorkspaceSearcher(backend="python"),
        tmp_path,
        "HIT",
        include="*.py",
        ignore_case=True,
        max_results=2,
    ))

    assert [(item.path, item.line) for item in batch.matches] == [
        ("many.py", 1),
        ("many.py", 2),
    ]
    assert batch.truncated is True
