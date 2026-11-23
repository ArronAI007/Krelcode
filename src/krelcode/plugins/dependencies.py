"""Plugin dependency requirement parsing and deterministic graph resolution."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Iterable, Mapping

from .semver import VersionError, parse_constraint


@dataclass(frozen=True)
class PluginDependency:
    name: str
    constraint: str = "*"
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("plugin dependency name cannot be empty")
        try:
            parse_constraint(self.constraint)
        except VersionError:
            raise

    def matches(self, version: str) -> bool:
        return parse_constraint(self.constraint).matches(version)

    def describe(self) -> str:
        return f"{self.name}@{self.constraint}" if self.constraint != "*" else self.name


@dataclass(frozen=True)
class DependencyNode:
    name: str
    version: str
    required: tuple[PluginDependency, ...] = ()
    optional: tuple[PluginDependency, ...] = ()
    conflicts: tuple[PluginDependency, ...] = ()


@dataclass(frozen=True)
class DependencyDiagnostic:
    code: str
    plugin: str
    dependency: str
    message: str


@dataclass(frozen=True)
class DependencyResolution:
    order: tuple[str, ...]
    blocked: Mapping[str, str]
    diagnostics: tuple[DependencyDiagnostic, ...]
    dependents: Mapping[str, tuple[str, ...]]


def _append_unique(values: dict[str, list[str]], key: str, value: str) -> None:
    bucket = values.setdefault(key, [])
    if value not in bucket:
        bucket.append(value)


def resolve_dependencies(
    nodes: Mapping[str, DependencyNode],
    *,
    disabled: Iterable[str] = (),
) -> DependencyResolution:
    """Resolve installed plugins, blocking only affected dependency subgraphs.

    Missing optional dependencies are not diagnostics. Disabled, missing,
    incompatible and cyclic required dependencies fail closed. A plugin whose
    required dependency is blocked is recursively blocked as well.
    """

    disabled_set = set(disabled)
    blocked: dict[str, str] = {}
    diagnostics: list[DependencyDiagnostic] = []
    dependents: dict[str, list[str]] = {}
    required_edges: dict[str, tuple[str, ...]] = {}

    for name, node in nodes.items():
        edge_names: list[str] = []
        for requirement in node.required:
            _append_unique(dependents, requirement.name, name)
            target = nodes.get(requirement.name)
            if requirement.name in disabled_set:
                blocked.setdefault(name, f"依赖 {requirement.describe()} 已禁用")
                diagnostics.append(DependencyDiagnostic(
                    "disabled", name, requirement.name,
                    f"依赖 {requirement.describe()} 已禁用"))
                continue
            if target is None:
                blocked.setdefault(name, f"缺少依赖 {requirement.describe()}")
                diagnostics.append(DependencyDiagnostic(
                    "missing", name, requirement.name,
                    f"缺少依赖 {requirement.describe()}"))
                continue
            try:
                compatible = requirement.matches(target.version)
            except VersionError as exc:
                blocked.setdefault(name, f"依赖版本无效: {exc}")
                diagnostics.append(DependencyDiagnostic(
                    "invalid_version", name, requirement.name, str(exc)))
                continue
            if not compatible:
                blocked.setdefault(
                    name,
                    f"依赖 {requirement.name}@{target.version} 不满足 {requirement.constraint}",
                )
                diagnostics.append(DependencyDiagnostic(
                    "incompatible", name, requirement.name,
                    f"已安装 {requirement.name}@{target.version} 不满足 "
                    f"{requirement.constraint}"))
                continue
            edge_names.append(requirement.name)
        required_edges[name] = tuple(edge_names)

        for requirement in node.optional:
            _append_unique(dependents, requirement.name, name)
            target = nodes.get(requirement.name)
            if target is None or requirement.name in disabled_set:
                continue
            try:
                if not requirement.matches(target.version):
                    diagnostics.append(DependencyDiagnostic(
                        "optional_incompatible", name, requirement.name,
                        f"可选依赖 {requirement.name}@{target.version} 不满足 "
                        f"{requirement.constraint}，已忽略"))
            except VersionError:
                continue

        for conflict in node.conflicts:
            target = nodes.get(conflict.name)
            if target is None:
                continue
            try:
                conflicts = conflict.matches(target.version)
            except VersionError:
                continue
            if conflicts:
                message = (
                    f"与 {conflict.name}@{target.version} 冲突 "
                    f"({conflict.constraint})")
                blocked.setdefault(name, message)
                blocked.setdefault(conflict.name, f"与 {name} 冲突")
                diagnostics.extend((
                    DependencyDiagnostic("conflict", name, conflict.name, message),
                    DependencyDiagnostic(
                        "conflict", conflict.name, name,
                        f"与插件 {name} 的冲突声明不兼容"),
                ))

    # Detect cycles without recursion so a hostile 5000-node graph cannot turn
    # into a Python RecursionError. Residual Kahn nodes include both cycle
    # members and nodes that can only load after a cycle, all fail-closed.
    cycle_indegree = {
        name: 0 for name in nodes if name not in blocked
    }
    cycle_outgoing: dict[str, list[str]] = {name: [] for name in cycle_indegree}
    for name in cycle_indegree:
        for dependency in required_edges.get(name, ()):
            if dependency in cycle_indegree:
                cycle_outgoing[dependency].append(name)
                cycle_indegree[name] += 1
    cycle_ready = [name for name, degree in cycle_indegree.items() if degree == 0]
    heapq.heapify(cycle_ready)
    while cycle_ready:
        name = heapq.heappop(cycle_ready)
        for dependent in cycle_outgoing.get(name, ()):
            cycle_indegree[dependent] -= 1
            if cycle_indegree[dependent] == 0:
                heapq.heappush(cycle_ready, dependent)
    for name in sorted(cycle_indegree):
        if cycle_indegree[name] > 0 and name not in blocked:
            blocked[name] = "依赖循环或依赖了循环子图"
            diagnostics.append(DependencyDiagnostic(
                "cycle", name, "", blocked[name]))

    changed = True
    while changed:
        changed = False
        for name, node in nodes.items():
            if name in blocked:
                continue
            for requirement in node.required:
                if requirement.name in blocked:
                    blocked[name] = (
                        f"依赖 {requirement.name} 无法加载: {blocked[requirement.name]}")
                    diagnostics.append(DependencyDiagnostic(
                        "blocked_dependency", name, requirement.name, blocked[name]))
                    changed = True
                    break

    indegree = {name: 0 for name in nodes if name not in blocked}
    outgoing: dict[str, list[str]] = {name: [] for name in indegree}
    for name in indegree:
        for dependency in required_edges.get(name, ()):
            if dependency in indegree:
                outgoing[dependency].append(name)
                indegree[name] += 1
    ready = [name for name, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        name = heapq.heappop(ready)
        order.append(name)
        for dependent in sorted(outgoing.get(name, ())):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, dependent)

    # Any residual node is part of an unresolved cycle; keep fail-closed even
    # if a malformed graph slipped past the explicit cycle pass.
    for name in sorted(indegree):
        if indegree[name] > 0 and name not in blocked:
            blocked[name] = "依赖图无法拓扑排序"
            diagnostics.append(DependencyDiagnostic(
                "cycle", name, "", "依赖图无法拓扑排序"))

    return DependencyResolution(
        order=tuple(order),
        blocked=dict(sorted(blocked.items())),
        diagnostics=tuple(diagnostics),
        dependents={name: tuple(values) for name, values in dependents.items()},
    )
