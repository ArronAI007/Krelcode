"""Compatibility-facade registry and AST-based retirement checks."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = Path("architecture/compatibility_facades.toml")
SOURCE_ROOT = Path("src/krelcode")
_FACADE_PREFIXES = (
    "compatibility facade",
    "compatibility import",
    "compatibility re-export",
    "compatibility surface",
    "backward-compatible",
)
_VERSION_PARTS = 3


class CompatibilityPolicyError(ValueError):
    """The compatibility manifest or one facade violates policy."""


@dataclass(frozen=True)
class FacadeSpec:
    path: str
    legacy: str
    new_imports: tuple[str, ...]
    deprecated_in: str
    remove_in: str
    internal_use: bool
    internal_uses: tuple[str, ...]


@dataclass(frozen=True)
class CompatibilityManifest:
    schema_version: int
    policy: str
    review_cycle: str
    reviewed_at: str
    current_version: str
    facades: tuple[FacadeSpec, ...]


def version_tuple(value: str) -> tuple[int, int, int]:
    raw = str(value).strip()
    parts = raw.split(".")
    if len(parts) != _VERSION_PARTS or not all(part.isdigit() for part in parts):
        raise CompatibilityPolicyError(f"版本号必须是 X.Y.Z: {value!r}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def project_version(root: Path = PROJECT_ROOT) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    value = data.get("project", {}).get("version")
    if not isinstance(value, str) or not value:
        raise CompatibilityPolicyError("pyproject.toml 缺少 project.version")
    return value


def module_name(path: Path, source_root: Path = SOURCE_ROOT) -> str:
    relative = path.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(["krelcode", *parts])


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise CompatibilityPolicyError(f"{field} 必须是字符串数组")
    items = tuple(item.strip() for item in value)
    if len(items) != len(set(items)):
        raise CompatibilityPolicyError(f"{field} 不能包含重复项")
    return items


def load_manifest(
    root: Path = PROJECT_ROOT,
    manifest_path: Path = MANIFEST_PATH,
) -> CompatibilityManifest:
    path = manifest_path if manifest_path.is_absolute() else root / manifest_path
    value = tomllib.loads(path.read_text(encoding="utf-8"))
    facades: list[FacadeSpec] = []
    for index, raw in enumerate(value.get("facades", [])):
        if not isinstance(raw, dict):
            raise CompatibilityPolicyError(f"facades[{index}] 必须是 TOML table")
        path_value = raw.get("path")
        legacy = raw.get("legacy")
        deprecated_in = raw.get("deprecated_in")
        remove_in = raw.get("remove_in")
        internal_use = raw.get("internal_use")
        if not isinstance(path_value, str) or not path_value:
            raise CompatibilityPolicyError(f"facades[{index}].path 必须非空")
        if not isinstance(legacy, str) or not legacy:
            raise CompatibilityPolicyError(f"facades[{index}].legacy 必须非空")
        if not isinstance(deprecated_in, str) or not deprecated_in:
            raise CompatibilityPolicyError(
                f"facades[{index}].deprecated_in 必须非空")
        if not isinstance(remove_in, str) or not remove_in:
            raise CompatibilityPolicyError(f"facades[{index}].remove_in 必须非空")
        if not isinstance(internal_use, bool):
            raise CompatibilityPolicyError(
                f"facades[{index}].internal_use 必须是布尔值")
        new_imports = _string_list(
            raw.get("new_imports"), f"facades[{index}].new_imports")
        internal_uses = _string_list(
            raw.get("internal_uses", []), f"facades[{index}].internal_uses")
        facades.append(FacadeSpec(
            path=path_value,
            legacy=legacy,
            new_imports=new_imports,
            deprecated_in=deprecated_in,
            remove_in=remove_in,
            internal_use=internal_use,
            internal_uses=internal_uses,
        ))
    return CompatibilityManifest(
        schema_version=int(value.get("schema_version", 0)),
        policy=str(value.get("policy", "")),
        review_cycle=str(value.get("review_cycle", "")),
        reviewed_at=str(value.get("reviewed_at", "")),
        current_version=project_version(root),
        facades=tuple(facades),
    )


def production_modules(root: Path = PROJECT_ROOT) -> dict[str, Path]:
    source_root = root / SOURCE_ROOT
    modules: dict[str, Path] = {}
    for path in source_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        modules[module_name(path, source_root)] = path
    return modules


def imported_modules(path: Path, source_root: Path = SOURCE_ROOT) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = list(path.relative_to(source_root.parent).with_suffix("").parts)[:-1]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:]
                if node.level > 1:
                    base = base[:-(node.level - 1)]
                if node.module:
                    base.extend(node.module.split("."))
                imported = ".".join(base)
            else:
                imported = node.module or ""
            if imported:
                found.add(imported)
                if node.module is None:
                    found.update(
                        f"{imported}.{alias.name}"
                        for alias in node.names
                        if alias.name != "*"
                    )
    return found


def concrete_project_imports(
    path: Path,
    modules: Mapping[str, Path],
    source_root: Path = SOURCE_ROOT,
) -> set[str]:
    """Return project imports without recording a parent package alongside a child."""
    own = module_name(path, source_root)
    items = {
        name for name in imported_modules(path, source_root)
        if name in modules and name != own
    }
    return {
        name for name in items
        if not any(
            other != name and other.startswith(name + ".")
            for other in items
        )
    }


def discover_compatibility_facades(
    root: Path = PROJECT_ROOT,
) -> set[str]:
    source_root = root / SOURCE_ROOT
    discovered: set[str] = set()
    for path in source_root.rglob("*.py"):
        if path.name == "__init__.py" or "__pycache__" in path.parts:
            continue
        doc = ast.get_docstring(
            ast.parse(path.read_text(encoding="utf-8"))) or ""
        first_line = doc.splitlines()[0].strip().lower() if doc else ""
        if first_line.startswith(_FACADE_PREFIXES):
            discovered.add(path.relative_to(root).as_posix())
    return discovered


def internal_importers(
    legacy: str,
    root: Path = PROJECT_ROOT,
    *,
    modules: Mapping[str, Path] | None = None,
    cache: dict[Path, set[str]] | None = None,
) -> set[str]:
    source_root = root / SOURCE_ROOT
    all_modules = dict(modules or production_modules(root))
    import_cache = cache if cache is not None else {}
    users: set[str] = set()
    for path in all_modules.values():
        key = path.resolve()
        imports = import_cache.get(key)
        if imports is None:
            imports = imported_modules(path, source_root)
            import_cache[key] = imports
        if legacy in imports or any(
            name.startswith(legacy + ".") for name in imports
        ):
            if module_name(path, source_root) != legacy:
                users.add(path.relative_to(root).as_posix())
    return users


def _is_all_assignment(node: ast.AST) -> bool:
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return False
    target = node.targets[0]
    if not isinstance(target, ast.Name) or target.id != "__all__":
        return False
    value = node.value
    return isinstance(value, (ast.List, ast.Tuple)) and all(
        isinstance(item, ast.Constant) and isinstance(item.value, str)
        for item in value.elts
    )


def _is_simple_getattr(node: ast.AST) -> bool:
    if not isinstance(node, ast.FunctionDef) or node.name != "__getattr__":
        return False
    if len(node.body) != 1 or not isinstance(node.body[0], ast.Return):
        return False
    call = node.body[0].value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return False
    if call.func.id != "getattr" or len(call.args) != 2:
        return False
    impl, name = call.args
    return (
        isinstance(impl, ast.Name)
        and impl.id == "_impl"
        and isinstance(name, ast.Name)
        and name.id == "name"
    )


def _is_simple_dir(node: ast.AST) -> bool:
    if not isinstance(node, ast.FunctionDef) or node.name != "__dir__":
        return False
    if len(node.body) != 1 or not isinstance(node.body[0], ast.Return):
        return False
    call = node.body[0].value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return False
    if call.func.id != "sorted" or len(call.args) != 1:
        return False
    union = call.args[0]
    if not isinstance(union, ast.BinOp) or not isinstance(union.op, ast.BitOr):
        return False
    left, right = union.left, union.right
    return (
        isinstance(left, ast.Call)
        and isinstance(left.func, ast.Name)
        and left.func.id == "set"
        and len(left.args) == 1
        and isinstance(left.args[0], ast.Call)
        and isinstance(left.args[0].func, ast.Name)
        and left.args[0].func.id == "globals"
        and isinstance(right, ast.Call)
        and isinstance(right.func, ast.Name)
        and right.func.id == "set"
        and len(right.args) == 1
        and isinstance(right.args[0], ast.Call)
        and isinstance(right.args[0].func, ast.Name)
        and right.args[0].func.id == "dir"
        and len(right.args[0].args) == 1
        and isinstance(right.args[0].args[0], ast.Name)
        and right.args[0].args[0].id == "_impl"
    )


def facade_ast_issues(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    issues: list[str] = []
    for index, node in enumerate(tree.body):
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, ast.ImportFrom):
            continue
        if _is_all_assignment(node):
            continue
        if _is_simple_getattr(node) or _is_simple_dir(node):
            continue
        issues.append(
            f"{path}:{getattr(node, 'lineno', 0)} 不允许 {type(node).__name__}；"
            "兼容 Facade 只能包含导入、__all__、__getattr__ 和 __dir__")
    return issues


def audit_repository(
    root: Path = PROJECT_ROOT,
    manifest_path: Path = MANIFEST_PATH,
) -> list[str]:
    manifest = load_manifest(root, manifest_path)
    issues: list[str] = []
    current = version_tuple(manifest.current_version)
    reviewed = version_tuple(manifest.reviewed_at)
    if manifest.schema_version != 1:
        issues.append("compatibility manifest schema_version 必须为 1")
    if manifest.policy != "reexport-only":
        issues.append("compatibility policy 必须为 reexport-only")
    if manifest.review_cycle != "major":
        issues.append("compatibility review_cycle 必须为 major")
    if current[0] != reviewed[0]:
        issues.append(
            "已跨越大版本，必须重新审查兼容清单并更新 reviewed_at："
            f"reviewed={manifest.reviewed_at}, current={manifest.current_version}")

    modules = production_modules(root)
    import_cache: dict[Path, set[str]] = {}
    discovered = discover_compatibility_facades(root)
    registered = {spec.path for spec in manifest.facades}
    for path in sorted(discovered - registered):
        issues.append(f"发现未登记的 Compatibility Facade: {path}")
    for path in sorted(registered - discovered):
        issues.append(f"清单中的路径未声明兼容 Facade 标准 docstring: {path}")

    seen_legacy: dict[str, str] = {}
    seen_paths: dict[str, str] = {}
    legacy_modules = {spec.legacy for spec in manifest.facades}
    for spec in manifest.facades:
        facade_path = root / spec.path
        if spec.path in seen_paths:
            issues.append(f"重复 Facade path: {spec.path}")
        seen_paths[spec.path] = spec.legacy
        if spec.legacy in seen_legacy:
            issues.append(f"重复 legacy import: {spec.legacy}")
        seen_legacy[spec.legacy] = spec.path
        if not facade_path.is_file():
            issues.append(f"Facade 文件不存在: {spec.path}")
            continue
        actual_module = module_name(facade_path, root / SOURCE_ROOT)
        if actual_module != spec.legacy:
            issues.append(
                f"{spec.path} 的 legacy 应为 {actual_module}，而非 {spec.legacy}")
        try:
            deprecated = version_tuple(spec.deprecated_in)
            remove = version_tuple(spec.remove_in)
        except CompatibilityPolicyError as exc:
            issues.append(f"{spec.path}: {exc}")
            continue
        if deprecated > current:
            issues.append(
                f"{spec.path} 尚未到首次弃用版本 {spec.deprecated_in}")
        if remove <= deprecated:
            issues.append(f"{spec.path} 的删除版本必须晚于首次弃用版本")
        if current >= remove:
            issues.append(
                f"{spec.path} 已到计划删除版本 {spec.remove_in}，必须删除")
        for target in spec.new_imports:
            if target not in modules:
                issues.append(f"{spec.path} 指向不存在的新路径: {target}")
            if target in legacy_modules:
                issues.append(
                    f"{spec.path} 指向另一个兼容 Facade: {target}")
        imports = concrete_project_imports(
            facade_path, modules, root / SOURCE_ROOT)
        missing_targets = sorted(set(spec.new_imports) - imports)
        if missing_targets:
            issues.append(
                f"{spec.path} 未实际 re-export 声明的新路径: {missing_targets}")
        unexpected_targets = sorted(imports - set(spec.new_imports))
        if unexpected_targets:
            issues.append(
                f"{spec.path} 存在未声明的项目依赖: {unexpected_targets}")
        actual_users = internal_importers(
            spec.legacy,
            root,
            modules=modules,
            cache=import_cache,
        )
        declared_users = set(spec.internal_uses)
        if bool(actual_users) != spec.internal_use:
            issues.append(
                f"{spec.path} internal_use={spec.internal_use}，"
                f"实际为 {bool(actual_users)}")
        if actual_users != declared_users:
            added = sorted(actual_users - declared_users)
            removed = sorted(declared_users - actual_users)
            issues.append(
                f"{spec.path} 内部引用基线不匹配；新增={added}, 已消除={removed}")
        if declared_users - set(path.relative_to(root).as_posix()
                                for path in modules.values()):
            issues.append(f"{spec.path} internal_uses 包含不存在文件")
        issues.extend(facade_ast_issues(facade_path))
    return sorted(set(issues))


def facade_summary(
    root: Path = PROJECT_ROOT,
    manifest_path: Path = MANIFEST_PATH,
) -> dict[str, Any]:
    manifest = load_manifest(root, manifest_path)
    return {
        "current_version": manifest.current_version,
        "reviewed_at": manifest.reviewed_at,
        "facades": len(manifest.facades),
        "internal_use": sum(1 for item in manifest.facades if item.internal_use),
        "internal_edges": sum(len(item.internal_uses) for item in manifest.facades),
        "next_removal": min(
            (item.remove_in for item in manifest.facades),
            key=version_tuple,
            default="",
        ),
    }


def render_summary(summary: Mapping[str, Any]) -> str:
    return (
        f"Compatibility facades: {summary['facades']} "
        f"(internal-use {summary['internal_use']}, "
        f"edges {summary['internal_edges']})\n"
        f"Version: {summary['current_version']} "
        f"(reviewed {summary['reviewed_at']}, "
        f"next removal {summary['next_removal'] or 'none'})"
    )


__all__ = [
    "CompatibilityManifest",
    "CompatibilityPolicyError",
    "FacadeSpec",
    "MANIFEST_PATH",
    "PROJECT_ROOT",
    "SOURCE_ROOT",
    "audit_repository",
    "concrete_project_imports",
    "discover_compatibility_facades",
    "facade_ast_issues",
    "facade_summary",
    "imported_modules",
    "internal_importers",
    "load_manifest",
    "module_name",
    "production_modules",
    "project_version",
    "render_summary",
    "version_tuple",
]
