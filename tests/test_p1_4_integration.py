"""Composition, dynamic tool-pool, and CLI flags for P1-4 capabilities."""

from __future__ import annotations

from pathlib import Path

from krelcode.agent.tool_pool import TurnToolPool
from krelcode.app import default_tools
from krelcode.browser.runtime import BrowserOptions, BrowserRuntime
from krelcode.entrypoints.main import build_parser
from krelcode.tools.base import PermissionMode, RuntimeSnapshot
from krelcode.tools.builtin.database_migration import MigrationOptions
from krelcode.tools.builtin.notebook import NotebookOptions
from krelcode.tools.registry import ToolRegistry


def test_feature_tools_are_composed_only_when_runtime_or_options_are_passed(tmp_path: Path):
    baseline = {tool.name for tool in default_tools()}
    browser = BrowserRuntime.from_options(BrowserOptions())
    composed = {
        tool.name for tool in default_tools(
            browser_runtime=browser,
            notebook_options=NotebookOptions(),
            migration_options=MigrationOptions(),
        )
    }

    assert "browser_open" not in baseline
    assert "notebook_inspect" not in baseline
    assert "migration_inspect" not in baseline
    assert {
        "browser_open",
        "browser_snapshot",
        "browser_close",
        "notebook_inspect",
        "notebook_edit",
        "notebook_create",
        "migration_inspect",
        "migration_validate",
        "migration_create",
    } <= composed


def test_read_only_pool_keeps_inspection_and_excludes_feature_mutations():
    browser = BrowserRuntime.from_options(BrowserOptions())
    registry = ToolRegistry(default_tools(
        browser_runtime=browser,
        notebook_options=NotebookOptions(),
        migration_options=MigrationOptions(),
    ))
    pool = TurnToolPool(
        registry,
        runtime=RuntimeSnapshot(permission_mode=PermissionMode.PLAN),
    )
    names = set(pool.names())

    assert {
        "browser_open", "browser_snapshot", "browser_close",
        "notebook_inspect", "migration_inspect", "migration_validate",
    } <= names
    assert not {"notebook_edit", "notebook_create", "migration_create"} & names


def test_disabled_or_missing_feature_surfaces_fail_closed():
    disabled_browser = BrowserRuntime.from_options(BrowserOptions(enabled=False))
    names = {
        tool.name for tool in default_tools(
            browser_runtime=disabled_browser,
            notebook_options=NotebookOptions(enabled=False),
            migration_options=MigrationOptions(enabled=False),
        )
    }

    assert not {
        "browser_open", "browser_snapshot", "browser_close",
        "notebook_inspect", "notebook_edit", "notebook_create",
        "migration_inspect", "migration_validate", "migration_create",
    } & names


def test_cli_exposes_independent_feature_disable_flags():
    args = build_parser().parse_args([
        "--no-web", "--no-browser", "--no-notebook", "--no-database",
    ])

    assert args.no_web is True
    assert args.no_browser is True
    assert args.no_notebook is True
    assert args.no_database is True


def test_feature_options_apply_conservative_config_bounds():
    browser = BrowserOptions.from_config({
        "backend": "unknown",
        "max_sessions": 999,
        "session_ttl": 1,
        "allow_private": False,
    })
    notebook = NotebookOptions.from_config({
        "max_file_bytes": 999_999_999,
        "max_cells": 99_999,
        "max_cell_chars": 999_999_999,
    })
    migration = MigrationOptions.from_config({
        "max_files": 99_999,
        "max_file_bytes": 999_999_999,
    })

    assert browser.backend == "static"
    assert browser.max_sessions == 16
    assert browser.session_ttl == 30.0
    assert browser.allow_private is False
    assert notebook.max_file_bytes == 10_000_000
    assert notebook.max_cells == 1_000
    assert notebook.max_cell_chars == 100_000
    assert migration.max_files == 2_000
    assert migration.max_file_bytes == 10_000_000
