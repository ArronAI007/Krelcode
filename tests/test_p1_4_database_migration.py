"""P1-4 database-migration discovery, static risk, and scaffold tests."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.database_migration import (
    MigrationCreateTool,
    MigrationInspectTool,
    MigrationOptions,
    MigrationValidateTool,
    build_migration_tools,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _alembic_tree(root: Path) -> None:
    _write(root / "alembic.ini", "[alembic]\n")
    _write(root / "alembic/env.py", "from alembic import context\n")
    _write(
        root / "alembic/versions/001_create_users.py",
        '''
"""create users"""
revision = "abc123"
down_revision = None

def upgrade():
    pass

def downgrade():
    pass
'''.strip(),
    )
    _write(
        root / "alembic/versions/002_add_status.py",
        '''
"""add status"""
revision = "def456"
down_revision = "abc123"

def upgrade():
    pass

def downgrade():
    pass
'''.strip(),
    )


def test_migration_tools_are_discoverable_and_config_disable_removes_all():
    tools = build_migration_tools(MigrationOptions())
    assert [tool.name for tool in tools] == [
        "migration_inspect", "migration_validate", "migration_create"]
    assert all(tool.tool_tier.value == "discoverable" for tool in tools)
    assert build_migration_tools(MigrationOptions(enabled=False)) == []


def test_inspect_detects_alembic_graph_heads_and_never_reads_credentials(tmp_path: Path):
    _alembic_tree(tmp_path)
    _write(tmp_path / ".env", "DATABASE_URL=postgresql://user:secret@db/prod\n")
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)

    result = asyncio.run(MigrationInspectTool(MigrationOptions()).execute(
        {"path": "."}, ctx))

    assert not result.is_error
    assert "alembic" in result.data["frameworks"]
    assert result.data["alembic_heads"] == ["def456"]
    assert result.data["alembic_revisions"] == ["abc123", "def456"]
    assert result.data["database_connected"] is False
    assert "postgresql://user:secret" not in result.content
    assert "alembic current" in result.content


def test_inspect_detects_multiple_frameworks_and_bounds_file_list(tmp_path: Path):
    _write(tmp_path / "manage.py", "print('django')\n")
    _write(tmp_path / "app/migrations/0001_initial.py", "# migration\n")
    _write(tmp_path / "prisma/schema.prisma", "datasource db {}\n")
    _write(tmp_path / "prisma/migrations/20250101_init/migration.sql", "CREATE TABLE t(id int);\n")
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)

    result = asyncio.run(MigrationInspectTool(MigrationOptions()).execute(
        {"path": "."}, ctx))

    assert not result.is_error
    assert {"django", "prisma"} <= set(result.data["frameworks"])
    assert any(path.endswith("migration.sql") for path in result.data["migration_files"])


def test_validate_flags_destructive_sql_missing_where_and_lock_risk(tmp_path: Path):
    _write(
        tmp_path / "migrations/001_destructive.sql",
        """
-- DROP TABLE ignored_in_comment;
DELETE FROM users;
UPDATE users SET admin = true;
DROP TABLE audit_log;
CREATE INDEX idx_users_email ON users(email);
""".strip(),
    )
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)

    result = asyncio.run(MigrationValidateTool(MigrationOptions()).execute(
        {"path": "migrations"}, ctx))
    codes = {item["code"] for item in result.data["findings"]}

    assert result.is_error  # critical findings are surfaced as a failing review
    assert {"MIG002", "MIG101", "MIG102", "MIG007"} <= codes
    assert "ignored_in_comment" not in result.content
    assert "database_connected" in result.data
    assert result.data["database_connected"] is False


def test_validate_flags_missing_or_unimplemented_alembic_downgrade(tmp_path: Path):
    _write(
        tmp_path / "migrations/versions/001_change.py",
        '''
revision = "r1"
down_revision = None

def upgrade():
    op.drop_column("users", "legacy")

def downgrade():
    raise NotImplementedError("rollback later")
'''.strip(),
    )
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)

    result = asyncio.run(MigrationValidateTool(MigrationOptions()).execute(
        {"path": "migrations/versions"}, ctx))
    codes = {item["code"] for item in result.data["findings"]}

    assert result.is_error is False  # high findings are reported; no critical exists
    assert {"MIG102", "MIG201"} <= codes
    assert any(item["severity"] == "high" for item in result.data["findings"])


def test_validate_checks_flyway_rollback_artifact_evidence(tmp_path: Path):
    _write(
        tmp_path / "db/migration/V1__drop_legacy.sql",
        "DROP TABLE legacy;\n",
    )
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)

    result = asyncio.run(MigrationValidateTool(MigrationOptions()).execute(
        {"path": "db/migration"}, ctx))
    codes = {item["code"] for item in result.data["findings"]}

    assert result.is_error
    assert "MIG002" in codes
    assert "MIG202" in codes


def test_generic_scaffold_is_comment_only_and_does_not_overwrite(tmp_path: Path):
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)
    creator = MigrationCreateTool(MigrationOptions())
    args = {
        "framework": "generic",
        "name": "add_account_status",
        "path": "migrations",
    }

    created = asyncio.run(creator.execute(args, ctx))
    files = list((tmp_path / "migrations").glob("*_add_account_status.sql"))
    duplicate = asyncio.run(creator.execute(args, ctx))

    assert not created.is_error
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "scaffold" in text
    assert "DROP " not in text
    assert duplicate.is_error
    assert created.data["database_connected"] is False


def test_alembic_scaffold_uses_head_and_fails_closed_until_implemented(tmp_path: Path):
    _alembic_tree(tmp_path)
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)

    result = asyncio.run(MigrationCreateTool(MigrationOptions()).execute(
        {"framework": "alembic", "name": "add_profile"},
        ctx,
    ))
    files = sorted((tmp_path / "alembic/versions").glob("*_add_profile.py"))
    text = files[0].read_text(encoding="utf-8")

    assert not result.is_error
    assert len(files) == 1
    assert 'down_revision = \'def456\'' in text or 'down_revision = "def456"' in text
    assert text.count("NotImplementedError") == 2
    assert "尚未连接数据库或应用迁移" in result.content


def test_scaffold_rejects_path_escape_and_sensitive_destination(tmp_path: Path):
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)
    creator = MigrationCreateTool(MigrationOptions())

    escaped = asyncio.run(creator.execute(
        {"framework": "generic", "name": "escape", "path": "../outside"},
        ctx,
    ))
    sensitive = asyncio.run(creator.execute(
        {"framework": "generic", "name": "secret", "path": ".krelcode/migrations"},
        ctx,
    ))

    assert escaped.is_error
    assert "越出工作区" in escaped.content
    assert sensitive.is_error
    assert "敏感路径" in sensitive.content


def test_validation_ignores_symlink_escape(tmp_path: Path):
    outside = tmp_path.parent / "outside-migrations"
    _write(outside / "001_outside.sql", "DROP TABLE outside;\n")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations/link.sql").symlink_to(outside / "001_outside.sql")
    ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)

    result = asyncio.run(MigrationInspectTool(MigrationOptions()).execute(
        {"path": "migrations"}, ctx))

    assert not result.is_error
    assert result.data["migration_files"] == []
