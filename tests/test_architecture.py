"""架构依赖守护：用 AST 检查关键层不能反向依赖外层/具体实现。"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).parent.parent
ROOT = PROJECT_ROOT / "src" / "krelcode"
BASELINE_PATH = PROJECT_ROOT / "architecture" / "baseline.toml"


DYNAMIC_IMPORT_CALLS = frozenset({
    "importlib.import_module",
    "importlib.util.find_spec",
    "importlib.util.spec_from_file_location",
    "importlib.util.module_from_spec",
    "__import__",
})


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _module_package(path: Path) -> str:
    module = _module_name(path)
    return module if path.name == "__init__.py" else module.rpartition(".")[0]


def _qualified_name(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_qualified_name(node.value, aliases)}.{node.attr}"
    return ""


def _resolve_dynamic_target(path: Path, value: str) -> str:
    if not value.startswith("."):
        return value
    package = _module_package(path)
    level = len(value) - len(value.lstrip("."))
    suffix = value.lstrip(".")
    parts = package.split(".") if package else []
    if level > 1:
        parts = parts[: -(level - 1)]
    if suffix:
        parts.extend(suffix.split("."))
    return ".".join(part for part in parts if part)


def _dynamic_import_calls(path: Path) -> tuple[tuple[str, str | None], ...]:
    """Return ``(kind, resolved_target)`` for dynamic module-loading calls."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = (
                    f"{node.module}.{alias.name}")
    calls: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _qualified_name(node.func, aliases)
        if kind not in DYNAMIC_IMPORT_CALLS:
            continue
        target = None
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
            node.args[0].value, str
        ):
            raw = node.args[0].value
            if kind != "importlib.util.spec_from_file_location":
                target = _resolve_dynamic_target(path, raw)
        calls.append((kind, target))
    return tuple(sorted(calls, key=lambda item: (item[0], item[1] or "")))


@lru_cache(maxsize=None)
def imported_modules(path: Path) -> set[str]:
    """Return static and resolvable dynamic imports for one module."""
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
                # ``from . import repl`` carries the imported module in the
                # alias, not in ``node.module``. Preserve those edges so the
                # module graph and cycle gate see real imports.
                if node.module is None:
                    found.update(f"{imported}.{alias.name}" for alias in node.names)
    found.update(
        target for _kind, target in _dynamic_import_calls(path)
        if target is not None
    )
    return found


@lru_cache(maxsize=1)
def architecture_baseline() -> dict[str, Any]:
    return tomllib.loads(BASELINE_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def production_modules() -> dict[str, Path]:
    """Return every production module keyed by import name."""
    modules: dict[str, Path] = {}
    for path in ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(ROOT.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        modules[".".join(parts)] = path
    return modules


@lru_cache(maxsize=1)
def imported_module_dependencies() -> dict[str, set[str]]:
    """Build the exact module graph, including root-level modules."""
    modules = production_modules()
    graph = {name: set() for name in modules}
    for name, path in modules.items():
        graph[name].update(
            target
            for target in imported_modules(path)
            if target in modules and target != name
        )
    return graph


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
    assert "krelcode.agent.context.context_checkpoint_ports" in imports
    assert "krelcode.agent.prompt.prompt_snapshot_ports" in imports
    assert "krelcode.session.recorder" in imports


def test_module_cycles_match_the_frozen_baseline():
    assert architecture_baseline()["version"] == 1
    graph = imported_module_dependencies()
    actual = {
        tuple(module.removeprefix("krelcode.") for module in component)
        for component in strongly_connected_components(graph)
        if len(component) > 1
    }
    expected = {
        tuple(sorted(cycle["modules"]))
        for cycle in architecture_baseline().get("cycles", [])
    }
    assert actual == expected, {
        "new_cycles": sorted(actual - expected),
        "stale_baseline": sorted(expected - actual),
    }


def test_dynamic_module_loading_is_confined_to_reviewed_boundaries():
    policy = architecture_baseline().get("dynamic_import", {})
    allowed = set(policy.get("allowed_files", ()))
    actual = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*.py")
        if "__pycache__" not in path.parts and _dynamic_import_calls(path)
    }
    assert actual == allowed, {
        "new_dynamic_loaders": sorted(actual - allowed),
        "stale_allowlist": sorted(allowed - actual),
    }


def test_resolvable_dynamic_imports_are_part_of_the_module_graph():
    graph = imported_module_dependencies()
    assert "krelcode.entrypoints.main" in graph["krelcode.entrypoints.cli"]
    # The live CLI consumes focused benchmark modules directly.  The
    # compatibility facade may point at live_cli, but the executable domain
    # must not point back at the facade (which would create a package cycle).
    assert "krelcode.benchmarks.behavior" not in graph["krelcode.benchmarks.live_cli"]


def test_oversized_files_match_the_frozen_baseline():
    baseline = architecture_baseline()["file_size"]
    limits = baseline["limits"]
    unlisted_limit = int(baseline["max_unlisted_lines"])
    violations: dict[str, dict[str, int]] = {}
    seen: set[str] = set()
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        current = len(path.read_text(encoding="utf-8").splitlines())
        if relative in limits:
            seen.add(relative)
            limit = int(limits[relative])
            if current != limit:
                violations[relative] = {
                    "current": current,
                    "limit": limit,
                    "action": (
                        "remove stale baseline entry"
                        if current <= unlisted_limit
                        else "ratchet baseline to current size"
                    ),
                }
        elif current > unlisted_limit:
            violations[relative] = {
                "current": current,
                "limit": unlisted_limit,
                "action": "split the file or add an explicitly reviewed baseline",
            }
    missing = set(limits) - seen
    assert not violations, violations
    assert not missing, f"baseline references missing files: {sorted(missing)}"


def _ends_in_loop(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Name) and node.id == "loop"
    ) or (
        isinstance(node, ast.Attribute) and node.attr == "loop"
    )


def loop_dynamic_access_counts(path: Path) -> dict[str, int]:
    """Count reflective loop access that bypasses the typed state view."""
    counts = {"getattr": 0, "setattr": 0, "dynamic_update": 0, "dict_access": 0}
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "__dict__"
            and _ends_in_loop(node.value)
        ):
            counts["dict_access"] += 1
        if not isinstance(node, ast.Call):
            continue
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "update_runtime_state"
            and _ends_in_loop(node.func.value)
        ):
            counts["dynamic_update"] += 1
        if not node.args:
            continue
        if not _ends_in_loop(node.args[0]):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "getattr":
            counts["getattr"] += 1
        elif isinstance(node.func, ast.Name) and node.func.id == "setattr":
            counts["setattr"] += 1
        elif isinstance(node.func, ast.Name) and node.func.id == "vars":
            counts["dict_access"] += 1
    return counts


def test_loop_dynamic_access_is_forbidden():
    limits = architecture_baseline()["loop_dynamic_access"]["limits"]
    assert not limits, "loop dynamic-access exceptions were fully retired"
    violations: dict[str, dict[str, int]] = {}
    seen: set[str] = set()
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative in limits:
            seen.add(relative)
        actual = loop_dynamic_access_counts(path)
        allowed = {
            key: int(limits.get(relative, {}).get(key, 0))
            for key in actual
        }
        if any(actual[key] > allowed[key] for key in actual):
            violations[relative] = {
                "actual": actual,
                "allowed": allowed,
            }
    missing = set(limits) - seen
    assert not violations, violations
    assert not missing, f"baseline references missing files: {sorted(missing)}"


def test_runtime_state_groups_do_not_expose_any_typed_fields():
    path = ROOT / "agent" / "turn" / "runtime_state_models.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    state_classes = {
        "TurnRuntimeState",
        "ContextRuntimeState",
        "PromptRuntimeState",
        "ExtensionRuntimeState",
        "SessionRuntimeState",
    }
    violations: dict[str, str] = {}

    def uses_any(annotation: ast.expr) -> bool:
        return any(
            (isinstance(item, ast.Name) and item.id == "Any")
            or (isinstance(item, ast.Attribute) and item.attr == "Any")
            for item in ast.walk(annotation)
        )

    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in state_classes:
            continue
        found.add(node.name)
        for item in node.body:
            if not isinstance(item, ast.AnnAssign):
                continue
            annotation = ast.unparse(item.annotation)
            if uses_any(item.annotation):
                field_name = ast.unparse(item.target)
                violations[f"{node.name}.{field_name}"] = annotation
    assert found == state_classes, sorted(state_classes - found)
    assert not violations, violations


def test_runtime_state_has_no_reflective_fallback_api():
    """Typed properties are the only compatibility surface for loop state."""
    violations: dict[str, list[str]] = {}
    for path in sorted((ROOT / "agent" / "turn").glob("runtime_state*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "StateGroup":
                bad.append(f"{path.name}:StateGroup")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in {"__getattr__", "__setattr__", "update_runtime_state"}:
                    bad.append(f"{path.name}:{node.name}")
        if bad:
            violations[path.name] = bad
    assert not violations, violations


# ---------------------------------------------------------------------------
# Package dependency contract
# ---------------------------------------------------------------------------

# Every production package declares exactly which sibling packages it may
# import. New edges must be reviewed here, which makes dependency drift visible
# instead of silently growing another cycle.
PACKAGE_ALLOWED_DEPENDENCIES: dict[str, frozenset[str]] = {
    "core": frozenset(),
    "bootstrap": frozenset({
        "agent", "application", "browser", "config", "contracts", "core",
        "daemon",
        "enterprise", "extensions", "hooks", "jobs", "llm", "lsp", "mcp",
        "governance", "observability", "output_styles", "plugins", "review",
        "runtime", "safety", "security_review", "session", "skills",
        "subagent", "telemetry", "tools", "ui", "web", "webui",
    }),
    "application": frozenset({"contracts"}),
    "contracts": frozenset({"core"}),
    "llm": frozenset({"core"}),
    "safety": frozenset({"core", "contracts"}),
    "tools": frozenset({"core", "contracts", "safety"}),
    "jobs": frozenset({"core", "safety"}),
    "web": frozenset({"contracts", "tools"}),
    "browser": frozenset({"contracts", "core", "tools", "web"}),
    "lsp": frozenset({"contracts", "core", "tools"}),
    "mcp": frozenset({"contracts", "core", "safety", "tools"}),
    "skills": frozenset({"contracts", "core", "safety", "tools"}),
    "plugins": frozenset({"contracts", "skills"}),
    "enterprise": frozenset({"contracts", "core"}),
    "agent": frozenset({"contracts", "core", "llm", "safety", "tools"}),
    "runtime": frozenset({
        "agent", "contracts", "core", "daemon", "jobs", "mcp", "safety",
        "session", "tools",
    }),
    "governance": frozenset({"contracts", "core", "enterprise", "safety"}),
    "tuning": frozenset({
        "benchmarks", "contracts", "core", "governance", "observability",
        "security_review",
    }),
    "security_review": frozenset(
        {"agent", "config", "contracts", "core", "llm", "safety", "tools"}),
    "subagent": frozenset(
        {"agent", "contracts", "core", "llm", "safety", "skills", "tools"}),
    "session": frozenset({
        "agent", "contracts", "core", "mcp", "safety", "security_review",
        "skills", "tools",
    }),
    "ui": frozenset({"agent", "contracts", "core", "safety", "session"}),
    "daemon": frozenset({
        "app", "config", "contracts", "core", "jobs", "safety", "subagent",
    }),
    "telemetry": frozenset({"core"}),
    "observability": frozenset({"contracts", "core"}),
    "webui": frozenset({"contracts", "observability"}),
    "benchmarks": frozenset({
        "agent", "app", "bootstrap", "config", "contracts", "core",
        "extensions", "llm", "safety", "sdk", "session", "subagent", "tools",
    }),
    "entrypoints": frozenset({
        "agent", "app", "benchmarks", "bootstrap", "config", "core", "daemon",
        "doctor", "enterprise", "jobs", "review", "scaffold", "security_review",
        "telemetry",
    }),
}

# Root-level modules are production components too. Keeping this list explicit
# prevents configuration, hooks, SDK facades or new top-level modules from
# bypassing the dependency contract simply by living outside a package.
ROOT_MODULE_ALLOWED_DEPENDENCIES: dict[str, frozenset[str]] = {
    "__main__": frozenset({"entrypoints"}),
    "app": frozenset({"bootstrap", "core", "enterprise", "entrypoints", "llm"}),
    "config": frozenset({"core"}),
    "doctor": frozenset({
        "agent", "config", "core", "enterprise", "llm", "safety", "telemetry",
    }),
    "extensions": frozenset(),
    "hooks": frozenset({"agent", "contracts", "core", "safety"}),
    "output_styles": frozenset({"agent"}),
    "review": frozenset({
        "agent", "config", "contracts", "core", "llm", "safety", "tools",
    }),
    "scaffold": frozenset(),
    "sdk": frozenset({"contracts", "core"}),
}

COMPONENT_ALLOWED_DEPENDENCIES: dict[str, frozenset[str]] = {
    **PACKAGE_ALLOWED_DEPENDENCIES,
    **ROOT_MODULE_ALLOWED_DEPENDENCIES,
}

BENCHMARK_IMPORT_BOUNDARY = frozenset({"benchmarks", "entrypoints", "tuning"})


@lru_cache(maxsize=1)
def production_packages() -> set[str]:
    """Return importable top-level production packages."""
    return {
        path.parent.name
        for path in ROOT.glob("*/__init__.py")
        if path.parent.is_dir()
    }


@lru_cache(maxsize=1)
def production_root_modules() -> set[str]:
    """Return governed top-level modules, excluding the package initializer."""
    return {
        path.stem
        for path in ROOT.glob("*.py")
        if path.stem != "__init__"
    }


@lru_cache(maxsize=1)
def production_components() -> set[str]:
    return production_packages() | production_root_modules()


@lru_cache(maxsize=1)
def imported_component_dependencies() -> dict[str, set[str]]:
    """Build the package + root-module graph from static imports."""
    packages = production_packages()
    components = production_components()
    dependencies: dict[str, set[str]] = {
        component: set() for component in components
    }
    for component in components:
        paths = (
            modules_under(component)
            if component in packages
            else [ROOT / f"{component}.py"]
        )
        for path in paths:
            imports = imported_modules(path)
            targets = {
                name.split(".", 2)[1]
                if len(name.split(".")) > 2 else name.removeprefix("krelcode.")
                for name in imports
                if name != "krelcode"
            }
            dependencies[component].update(
                target
                for target in targets
                if target in components and target != component
            )
    return dependencies


@lru_cache(maxsize=1)
def imported_package_dependencies() -> dict[str, set[str]]:
    """Project the component graph onto package-only edges."""
    packages = production_packages()
    return {
        package: dependencies & packages
        for package, dependencies in imported_component_dependencies().items()
        if package in packages
    }


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


def test_every_root_module_has_an_explicit_dependency_policy():
    root_modules = production_root_modules()
    assert set(ROOT_MODULE_ALLOWED_DEPENDENCIES) == root_modules


def test_every_production_component_has_a_dependency_policy():
    components = production_components()
    assert set(COMPONENT_ALLOWED_DEPENDENCIES) == components


def test_component_dependencies_are_whitelisted():
    actual = imported_component_dependencies()
    violations = {
        component: sorted(dependencies - COMPONENT_ALLOWED_DEPENDENCIES[component])
        for component, dependencies in actual.items()
        if dependencies - COMPONENT_ALLOWED_DEPENDENCIES[component]
    }
    assert not violations


def test_package_dependencies_are_whitelisted():
    actual = imported_package_dependencies()
    packages = production_packages()
    violations = {
        package: sorted(
            dependencies - (PACKAGE_ALLOWED_DEPENDENCIES[package] & packages))
        for package, dependencies in actual.items()
        if dependencies - (PACKAGE_ALLOWED_DEPENDENCIES[package] & packages)
    }
    assert not violations


def test_mcp_does_not_depend_on_agent_prompt_surface():
    violations: dict[str, list[str]] = {}
    for path in modules_under("mcp"):
        dependencies = {
            module for module in imported_modules(path)
            if module == "krelcode.agent"
            or module.startswith("krelcode.agent.")
        }
        if dependencies:
            violations[str(path.relative_to(ROOT))] = sorted(dependencies)
    assert not violations, violations


def test_mcp_package_imports_without_loading_agent_modules():
    code = (
        "import sys; import krelcode.mcp.client; "
        "loaded = sorted(name for name in sys.modules "
        "if name == 'krelcode.agent' or name.startswith('krelcode.agent.')); "
        "assert not loaded, loaded"
    )
    env = dict(os.environ)
    python_path = str(PROJECT_ROOT / "src")
    if env.get("PYTHONPATH"):
        python_path += os.pathsep + env["PYTHONPATH"]
    env["PYTHONPATH"] = python_path
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_production_package_graph_has_no_strongly_connected_components():
    actual = imported_package_dependencies()
    cycles = [
        component for component in strongly_connected_components(actual)
        if len(component) > 1
    ]
    assert not cycles


def test_dependency_whitelist_itself_is_acyclic():
    packages = production_packages()
    whitelist = {
        package: set(dependencies) & packages
        for package, dependencies in PACKAGE_ALLOWED_DEPENDENCIES.items()
    }
    cycles = [
        component for component in strongly_connected_components(whitelist)
        if len(component) > 1
    ]
    assert not cycles


def test_root_module_dependency_whitelist_is_acyclic():
    root_modules = production_root_modules()
    whitelist = {
        module: set(dependencies) & root_modules
        for module, dependencies in ROOT_MODULE_ALLOWED_DEPENDENCIES.items()
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


# ---------------------------------------------------------------------------
# Agent turn runtime decomposition
# ---------------------------------------------------------------------------

def _class_node(path: Path, name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _method_node(path: Path, class_name: str, method_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    klass = _class_node(path, class_name)
    return next(
        node for node in klass.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    )


def test_agent_turn_runtime_has_explicit_executor_boundaries():
    expected = {
        "dependencies.py": "AgentDependencies",
        "context_turn.py": "ContextTurnCoordinator",
        "tool_turn.py": "ToolBatchExecutor",
        "model_turn.py": "ModelTurnExecutor",
    }
    for filename, class_name in expected.items():
        path = ROOT / "agent" / "turn" / filename
        assert path.exists(), filename
        _class_node(path, class_name)


def test_tool_batch_executor_delegates_to_execution_runtime():
    path = ROOT / "agent" / "turn" / "tool_turn.py"
    source = path.read_text(encoding="utf-8")

    assert "ToolRegistry" not in source
    assert "tool.execute(" not in source
    assert "deps.execution_runtime.execute(intent)" in source


def test_benchmark_port_boundary_has_no_agent_internals():
    path = ROOT / "benchmarks" / "ports.py"
    source = path.read_text(encoding="utf-8")

    assert "krelcode.agent" not in source
    assert "from ..agent" not in source
    assert "EvaluationHarnessPort" in source
    assert "TraceReplayPort" in source
    assert "VersionManifestPort" in source


def test_agent_package_does_not_import_production_runtime_package():
    violations: dict[str, list[str]] = {}
    for path in modules_under("agent"):
        dependencies = {
            module for module in imported_modules(path)
            if module == "krelcode.runtime"
            or module.startswith("krelcode.runtime.")
        }
        if dependencies:
            violations[str(path.relative_to(ROOT))] = sorted(dependencies)
    assert not violations, violations


def test_agent_loop_is_a_thin_turn_orchestration_facade():
    path = ROOT / "agent" / "turn" / "loop.py"
    source = path.read_text(encoding="utf-8")
    assert len(source.splitlines()) < 1_000
    # Concrete context/model/tool mechanics must not drift back into the loop.
    assert "async for event in stream" not in source
    assert "ContextCacheLedger.from_components" not in source
    assert "validate_tool_arguments(" not in source

    delegated_methods = {
        "_build_llm_request",
        "_llm_turn",
        "_apply_tool_results",
        "_execute_one",
        "microcompact_tools",
        "compress_context",
    }
    oversized = {}
    for method_name in delegated_methods:
        node = _method_node(path, "AgentLoop", method_name)
        end = getattr(node, "end_lineno", node.lineno)
        length = end - node.lineno + 1
        if length > 18:
            oversized[method_name] = length
    assert not oversized


def test_runtime_executors_do_not_import_outer_layers():
    forbidden = ("krelcode.ui", "krelcode.session", "krelcode.entrypoints")
    violations = {}
    for filename in ("dependencies.py", "context_turn.py", "tool_turn.py", "model_turn.py"):
        path = ROOT / "agent" / "turn" / filename
        imports = imported_modules(path)
        bad = sorted(
            name for name in imports
            if any(depends_on({name}, prefix) for prefix in forbidden)
        )
        if bad:
            violations[filename] = bad
    assert not violations


def test_agent_domain_compatibility_facades_reexport_public_classes():
    from krelcode.agent.context import ContextOptions
    from krelcode.agent.context.workspace import ContextOptions as ContextOptionsImpl
    from krelcode.agent.fragments import PromptFragmentRegistry
    from krelcode.agent.loop import AgentLoop
    from krelcode.agent.prompt.fragments import PromptFragmentRegistry as RegistryImpl
    from krelcode.agent.turn.loop import AgentLoop as AgentLoopImpl

    assert ContextOptions is ContextOptionsImpl
    assert PromptFragmentRegistry is RegistryImpl
    assert AgentLoop is AgentLoopImpl
