"""命令注册、过滤与解析。

支持按来源原子替换，因此 Skill/MCP 热重载只需要重建对应来源，不需要修改
REPL 的静态菜单或 InputProcessor 的判断表。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .types import CommandSource, CommandSpec

SOURCE_PRIORITY = {
    CommandSource.BUILTIN: 100,
    CommandSource.SKILL: 50,
    CommandSource.MCP: 40,
}


@dataclass(frozen=True)
class CommandConflict:
    token: str
    kept: CommandSpec
    rejected: CommandSpec


def normalize_token(token: str) -> str:
    return token.strip().lstrip("/").lower()


def _validate_spec(spec: CommandSpec,
                   expected_source: Optional[CommandSource] = None) -> None:
    if expected_source is not None and spec.source is not expected_source:
        raise ValueError(
            f"命令 {spec.name!r} 来源为 {spec.source.value}，"
            f"不能注册到 {expected_source.value}")
    for token in (spec.name, *spec.aliases):
        normalized = normalize_token(token)
        if not normalized or any(ch.isspace() for ch in normalized):
            raise ValueError(f"非法命令名: {token!r}")


class CommandRegistry:
    """线程内只做同步快照更新；执行仍在 REPL 的事件循环中。"""

    def __init__(self, specs: Iterable[CommandSpec] = ()) -> None:
        self._source_specs: dict[CommandSource, tuple[CommandSpec, ...]] = {}
        self._specs: tuple[CommandSpec, ...] = ()
        self._lookup: dict[str, CommandSpec] = {}
        self._conflicts: tuple[CommandConflict, ...] = ()
        grouped: dict[CommandSource, list[CommandSpec]] = {}
        for spec in specs:
            _validate_spec(spec)
            grouped.setdefault(spec.source, []).append(spec)
        for source, items in grouped.items():
            self._source_specs[source] = tuple(items)
        self._rebuild()

    @property
    def specs(self) -> tuple[CommandSpec, ...]:
        return self._specs

    @property
    def conflicts(self) -> tuple[CommandConflict, ...]:
        return self._conflicts

    def replace_source(self, source: CommandSource,
                       specs: Iterable[CommandSpec]) -> None:
        items = tuple(specs)
        for spec in items:
            _validate_spec(spec, source)
        self._source_specs[source] = items
        self._rebuild()

    def resolve(self, token: str) -> Optional[CommandSpec]:
        return self._lookup.get(normalize_token(token))

    def list(
        self,
        *,
        query: str = "",
        running: bool = False,
        include_hidden: bool = False,
        include_disabled: bool = False,
    ) -> list[CommandSpec]:
        prefix = normalize_token(query)
        result: list[CommandSpec] = []
        for spec in self._specs:
            if not include_hidden and spec.is_hidden:
                continue
            if not include_disabled and not spec.is_enabled:
                continue
            if running and not spec.available_during_run:
                continue
            if prefix and not spec.name.startswith(prefix):
                continue
            result.append(spec)
        return result

    def _rebuild(self) -> None:
        lookup: dict[str, CommandSpec] = {}
        accepted: list[CommandSpec] = []
        conflicts: list[CommandConflict] = []
        sources = sorted(
            self._source_specs,
            key=lambda source: (-SOURCE_PRIORITY.get(source, 0), source.value),
        )
        for source in sources:
            for spec in self._source_specs[source]:
                names = (spec.name, *spec.aliases)
                duplicate = next(
                    (normalize_token(name) for name in names
                     if normalize_token(name) in lookup),
                    None,
                )
                if duplicate is not None:
                    conflicts.append(CommandConflict(
                        token=duplicate,
                        kept=lookup[duplicate],
                        rejected=spec,
                    ))
                    continue
                accepted.append(spec)
                for name in names:
                    lookup[normalize_token(name)] = spec
        self._specs = tuple(accepted)
        self._lookup = lookup
        self._conflicts = tuple(conflicts)
