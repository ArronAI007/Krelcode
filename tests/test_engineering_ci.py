from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_runtime_stays_dependency_free_and_dev_extra_is_explicit():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = pyproject["project"]

    assert project["dependencies"] == []
    assert set(project["optional-dependencies"]["dev"]) == {"pytest", "mypy"}
    mypy = pyproject["tool"]["mypy"]
    assert mypy["strict"] is True
    assert mypy["files"] == [
        "src/krelcode/core",
        "src/krelcode/contracts",
        "src/krelcode/llm",
        "src/krelcode/safety",
        "src/krelcode/agent",
        "src/krelcode/session",
        "src/krelcode/subagent",
        "src/krelcode/bootstrap",
    ]


def test_primary_ci_covers_required_platforms_and_guarantees():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    for value in (
        "ubuntu-latest",
        "macos-latest",
        "windows-latest",
        '"3.11"',
        '"3.12"',
        "tests/test_architecture.py",
        "python -m mypy",
        "python -m pytest -q",
        "python -m build",
    ):
        assert value in workflow
    assert "install-smoke:" in workflow
    assert "typescript-sdk:" in workflow
    assert "vscode-extension:" in workflow
