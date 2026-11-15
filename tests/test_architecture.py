"""架构依赖守护：用 AST 检查关键层不能反向依赖外层/具体实现。"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent / "src" / "krelcode"


def imported_modules(path: Path) -> set[str]:
    """返回 Python 文件中的静态 import 及相对 import 的完整模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    module_parts = list(path.relative_to(ROOT.parent).with_suffix("").parts)
    package_parts = module_parts[:-1]
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = list(package_parts)
                if node.level > 1:
                    base = base[:-(node.level - 1)]
                if node.module:
                    base.extend(node.module.split("."))
                imported = ".".join(base)
            else:
                imported = node.module or ""
            if imported:
                found.add(imported)
    return found


def modules_under(package: str) -> list[Path]:
    return sorted((ROOT / package).rglob("*.py"))


def depends_on(imports: set[str], package: str) -> list[str]:
    return sorted(
        name for name in imports
        if name == package or name.startswith(package + ".")
    )


def test_core_has_no_upward_dependencies():
    violations = {}
    for path in modules_under("core"):
        imports = imported_modules(path)
        outside = [name for name in imports
                   if name.startswith("krelcode.")
                   and not depends_on({name}, "krelcode.core")]
        if outside:
            violations[str(path.relative_to(ROOT))] = sorted(outside)
    assert not violations


def test_agent_does_not_import_outer_or_concrete_adapter_layers():
    forbidden = (
        "krelcode.ui",
        "krelcode.session",
        "krelcode.mcp",
        "krelcode.app",
        "krelcode.entrypoints",
        "krelcode.llm.providers",
        "krelcode.tools.builtin",
    )
    violations = {}
    for path in modules_under("agent"):
        imports = imported_modules(path)
        bad = [name for name in imports
               if any(depends_on({name}, prefix) for prefix in forbidden)]
        if bad:
            violations[str(path.relative_to(ROOT))] = sorted(bad)
    assert not violations


def test_session_modules_do_not_import_ui_or_composition_root():
    violations = {}
    for path in modules_under("session"):
        imports = imported_modules(path)
        bad = [name for name in imports
               if depends_on({name}, "krelcode.ui")
               or depends_on({name}, "krelcode.app")
               or depends_on({name}, "krelcode.entrypoints")]
        if bad:
            violations[str(path.relative_to(ROOT))] = sorted(bad)
    assert not violations


def test_input_pipeline_is_independent_of_agent_and_concrete_adapters():
    allowed_prefixes = ("krelcode.core", "krelcode.session")
    violations = {}
    for filename in ("input.py", "attachments.py"):
        path = ROOT / "session" / filename
        imports = imported_modules(path)
        bad = [name for name in imports
               if name.startswith("krelcode.")
               and not any(depends_on({name}, prefix)
                           for prefix in allowed_prefixes)]
        if bad:
            violations[filename] = sorted(bad)
    assert not violations


def test_ui_does_not_import_composition_root_or_entrypoints():
    violations = {}
    for path in modules_under("ui"):
        imports = imported_modules(path)
        bad = [name for name in imports
               if depends_on({name}, "krelcode.app")
               or depends_on({name}, "krelcode.entrypoints")]
        if bad:
            violations[str(path.relative_to(ROOT))] = sorted(bad)
    assert not violations


def test_relative_import_resolver_handles_parent_packages():
    imports = imported_modules(ROOT / "session" / "restore.py")
    assert "krelcode.agent.loop" in imports
    assert "krelcode.session.recorder" in imports
