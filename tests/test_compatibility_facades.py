"""Compatibility-facade retirement policy regressions."""

from __future__ import annotations

from pathlib import Path

from architecture.compatibility import (
    audit_repository,
    facade_ast_issues,
    facade_summary,
    load_manifest,
)


def test_compatibility_facade_registry_is_clean():
    assert audit_repository() == []


def test_compatibility_manifest_has_explicit_lifecycle_fields():
    manifest = load_manifest()

    assert manifest.schema_version == 1
    assert manifest.policy == "reexport-only"
    assert manifest.review_cycle == "major"
    assert manifest.facades
    for facade in manifest.facades:
        assert facade.deprecated_in
        assert facade.remove_in
        assert facade.new_imports
        assert facade.internal_use is bool(facade.internal_uses)


def test_compatibility_summary_is_content_light():
    summary = facade_summary()

    assert summary["facades"] > 0
    assert summary["next_removal"] == "1.0.0"
    assert set(summary) == {
        "current_version",
        "reviewed_at",
        "facades",
        "internal_use",
        "internal_edges",
        "next_removal",
    }


def test_facade_ast_rejects_business_logic(tmp_path: Path):
    path = tmp_path / "legacy.py"
    path.write_text(
        '"""Compatibility facade for :mod:`krelcode.new_path`."""\n\n'
        "from krelcode.new_path import *\n\n"
        "class NewImplementation:\n"
        "    pass\n",
        encoding="utf-8",
    )

    issues = facade_ast_issues(path)

    assert issues
    assert "ClassDef" in issues[0]


def test_facade_ast_allows_delegating_reexports(tmp_path: Path):
    path = tmp_path / "legacy.py"
    path.write_text(
        '"""Compatibility facade for :mod:`krelcode.new_path`."""\n\n'
        "from __future__ import annotations\n"
        "from typing import Any\n"
        "from krelcode import new_path as _impl\n"
        "from krelcode.new_path import *\n\n"
        "__all__ = ['PublicName']\n\n"
        "def __getattr__(name: str) -> Any:\n"
        "    return getattr(_impl, name)\n\n"
        "def __dir__() -> list[str]:\n"
        "    return sorted(set(globals()) | set(dir(_impl)))\n",
        encoding="utf-8",
    )

    assert facade_ast_issues(path) == []


def test_app_facade_preserves_provider_factory_exports():
    from krelcode.app import create_provider, resolve_settings

    assert callable(create_provider)
    assert callable(resolve_settings)
