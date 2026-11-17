from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from krelcode.ui.status_line import (
    DEFAULT_TEMPLATE,
    StatusLineOptions,
    StatusLineRenderer,
    read_git_branch,
)


def test_default_status_line_cleans_empty_status_separator():
    renderer = StatusLineRenderer()
    line = renderer.render(
        permission="权限 default",
        status="",
    )
    assert line == "权限 default · Shift-Tab 切换 · Ctrl-O 面板"


def test_custom_template_supports_runtime_values_and_truncation():
    renderer = StatusLineRenderer(StatusLineOptions(
        template="{provider}/{model} · {branch} · {context} · {status}",
        max_length=24,
    ))
    line = renderer.render(
        provider="deepseek", model="deepseek-chat", branch="main",
        context="ctx 100/200k", status="thinking",
    )
    assert line.startswith("deepseek/deepseek-chat")
    assert line.endswith("…")
    assert len(line) == 24


def test_template_validation_rejects_unknown_fields():
    with pytest.raises(ValueError, match="未知变量"):
        StatusLineRenderer(StatusLineOptions(template="{shell}"))


def test_control_characters_are_removed():
    renderer = StatusLineRenderer(StatusLineOptions(template="{status}"))
    assert "\x00" not in renderer.render(status="ok\x00\nmessage")
    assert renderer.render(status="ok\x00\nmessage") == "ok message"


def test_from_config_disabled_and_branch_lookup(tmp_path):
    options = StatusLineOptions.from_config({"enabled": False})
    assert not options.enabled
    assert StatusLineRenderer(options).render(status="x") == ""
    assert read_git_branch(tmp_path) == ""
    with patch("krelcode.ui.status_line.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "feature/status\n"
        assert read_git_branch(tmp_path) == "feature/status"
