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


# ---------------------------------------------------------------------------
# Package dependency contract
# ---------------------------------------------------------------------------

# Every production package declares exactly which sibling packages it may
# import. New edges must be reviewed here, which makes dependency drift visible
# instead of silently growing another cycle.
PACKAGE_ALLOWED_DEPENDENCIES: dict[str, frozenset[str]] = {
    "core": frozenset(),
    "contracts": frozenset({"core"}),
    "llm": frozenset({"core"}),
    "safety": frozenset({"core", "contracts"}),
    "tools": frozenset({"core", "contracts", "safety"}),
    "jobs": frozenset({"core", "safety"}),
    "web": frozenset({"contracts", "tools"}),
    "browser": frozenset({"contracts", "core", "tools", "web"}),
    "lsp": frozenset({"contracts", "core", "tools"}),
    "mcp": frozenset(
        {"agent", "contracts", "core", "safety", "tools"}),
    "skills": frozenset({"contracts", "core", "safety", "tools"}),
    "plugins": frozenset({"contracts", "skills"}),
    "enterprise": frozenset({"contracts", "core"}),
    "agent": frozenset({"contracts", "core", "llm", "safety", "tools"}),
    "security_review": frozenset(
        {"agent", "contracts", "core", "llm", "safety", "tools"}),
    "subagent": frozenset(
        {"agent", "contracts", "core", "llm", "safety", "skills", "tools"}),
    "session": frozenset({
        "agent", "contracts", "core", "mcp", "safety", "security_review",
        "skills", "tools",
    }),
    "ui": frozenset({"agent", "core", "safety", "session"}),
    "daemon": frozenset({"core", "jobs", "safety", "subagent"}),
    "telemetry": frozenset({"core"}),
    "benchmarks": frozenset({
        "agent", "contracts", "core", "llm", "safety", "session",
        "subagent", "tools",
    }),
    "entrypoints": frozenset({
        "agent", "benchmarks", "core", "daemon", "enterprise", "jobs",
        "security_review", "telemetry",
    }),
}

BENCHMARK_IMPORT_BOUNDARY = frozenset({"benchmarks", "entrypoints"})


def production_packages() -> set[str]:
    """Return importable top-level production packages."""
    return {
        path.parent.name
        for path in ROOT.glob("*/__init__.py")
        if path.parent.is_dir()
    }


def imported_package_dependencies() -> dict[str, set[str]]:
    """Build the package graph from static imports, excluding composition files."""
    packages = production_packages()
    known = {f"krelcode.{package}" for package in packages}
    dependencies: dict[str, set[str]] = {package: set() for package in packages}
    for package in packages:
        for path in modules_under(package):
            imports = imported_modules(path)
            targets = {
                name.split(".", 2)[1]
                if len(name.split(".")) > 2 else name.removeprefix("krelcode.")
                for name in imports
                if name != "krelcode"
            }
            for target in targets:
                full_target = f"krelcode.{target}"
                if full_target in known and target != package:
                    dependencies[package].add(target)
    return dependencies


def strongly_connected_components(
    graph: dict[str, set[str]],
) -> list[tuple[str, ...]]:
    """Return all Tarjan SCCs (including singleton components)."""
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for neighbor in sorted(graph[node]):
            if neighbor not in indices:
                visit(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        components.append(tuple(sorted(component)))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return components


def test_every_production_package_has_an_explicit_dependency_policy():
    packages = production_packages()
    assert set(PACKAGE_ALLOWED_DEPENDENCIES) == packages


def test_package_dependencies_are_whitelisted():
    actual = imported_package_dependencies()
    violations = {
        package: sorted(dependencies - PACKAGE_ALLOWED_DEPENDENCIES[package])
        for package, dependencies in actual.items()
        if dependencies - PACKAGE_ALLOWED_DEPENDENCIES[package]
    }
    assert not violations


def test_production_package_graph_has_no_strongly_connected_components():
    actual = imported_package_dependencies()
    cycles = [
        component for component in strongly_connected_components(actual)
        if len(component) > 1
    ]
    assert not cycles


def test_dependency_whitelist_itself_is_acyclic():
    whitelist = {
        package: set(dependencies)
        for package, dependencies in PACKAGE_ALLOWED_DEPENDENCIES.items()
    }
    cycles = [
        component for component in strongly_connected_components(whitelist)
        if len(component) > 1
    ]
    assert not cycles


def test_only_the_entrypoint_boundary_may_import_benchmarks():
    violations: dict[str, list[str]] = {}
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        source_package = relative.parts[0]
        if len(relative.parts) < 2:
            continue
        if "__pycache__" in relative.parts or source_package in BENCHMARK_IMPORT_BOUNDARY:
            continue
        benchmark_imports = depends_on(imported_modules(path), "krelcode.benchmarks")
        if benchmark_imports:
            violations[str(relative)] = benchmark_imports
    assert not violations


def test_agent_never_imports_benchmarks():
    violations = {}
    for path in modules_under("agent"):
        imports = imported_modules(path)
        if depends_on(imports, "krelcode.benchmarks"):
            violations[str(path.relative_to(ROOT))] = sorted(imports)
    assert not violations


def test_tools_never_import_agent_or_session():
    violations = {}
    for path in modules_under("tools"):
        imports = imported_modules(path)
        bad = sorted(
            name for name in imports
            if depends_on({name}, "krelcode.agent")
            or depends_on({name}, "krelcode.session")
        )
        if bad:
            violations[str(path.relative_to(ROOT))] = bad
    assert not violations


def test_safety_never_imports_tools():
    violations = {}
    for path in modules_under("safety"):
        imports = imported_modules(path)
        if depends_on(imports, "krelcode.tools"):
            violations[str(path.relative_to(ROOT))] = sorted(imports)
    assert not violations


def test_contracts_only_depend_on_core_within_production():
    violations = {}
    for path in modules_under("contracts"):
        imports = imported_modules(path)
        bad = [
            name for name in imports
            if name.startswith("krelcode.")
            and not (
                depends_on({name}, "krelcode.core")
                or depends_on({name}, "krelcode.contracts")
            )
        ]
        if bad:
            violations[str(path.relative_to(ROOT))] = sorted(bad)
    assert not violations


def test_cross_layer_code_imports_tool_dtos_from_contracts():
    violations: dict[str, list[str]] = {}
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if (
            "__pycache__" in relative.parts
            or not relative.parts
            or relative.parts[0] == "tools"
        ):
            continue
        imports = imported_modules(path)
        legacy = depends_on(imports, "krelcode.tools.base")
        if legacy:
            violations[str(relative)] = legacy
    assert not violations
