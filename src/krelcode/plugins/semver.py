"""Small, strict SemVer subset used by plugin dependency resolution.

The implementation intentionally does not depend on ``packaging``. It accepts
the common npm-style forms used by extension manifests:

* ``1.2.3`` (partial ``1`` and ``1.2`` are treated as version ranges)
* ``^1.2.3`` / ``~1.2``
* ``>=1.0,<2.0`` (``||`` separates alternatives)
* ``1.2.x`` / ``1.*`` / ``*``

Build metadata participates in validation but not precedence, matching SemVer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from typing import Iterable, Iterator, Optional


class VersionError(ValueError):
    """A version or constraint is malformed."""


_VERSION_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)"
    r"(?:\.(?P<minor>0|[1-9]\d*))?"
    r"(?:\.(?P<patch>0|[1-9]\d*))?"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_IDENTIFIER_RE = re.compile(r"^[0-9A-Za-z-]+$")


def _validate_identifiers(value: Optional[str], *, prerelease: bool) -> tuple[str, ...]:
    if not value:
        return ()
    identifiers = tuple(value.split("."))
    for identifier in identifiers:
        if not identifier or not _IDENTIFIER_RE.fullmatch(identifier):
            raise VersionError(f"非法版本标识符: {identifier!r}")
        if prerelease and identifier.isdigit() and len(identifier) > 1 and identifier[0] == "0":
            raise VersionError("预发布数字标识符不能有前导零")
    return identifiers


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int = 0
    patch: int = 0
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> "Version":
        text = str(value).strip()
        match = _VERSION_RE.fullmatch(text)
        if match is None:
            raise VersionError(f"非法版本: {value!r}")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor") or 0),
            patch=int(match.group("patch") or 0),
            prerelease=_validate_identifiers(
                match.group("pre"), prerelease=True),
            build=_validate_identifiers(match.group("build"), prerelease=False),
        )

    def _precedence(self) -> tuple[int, int, int, tuple[tuple[int, object], ...]]:
        pre: list[tuple[int, object]] = []
        if self.prerelease:
            for item in self.prerelease:
                pre.append((0, int(item)) if item.isdigit() else (1, item))
            return self.major, self.minor, self.patch, tuple(pre)
        # A final release sorts after every prerelease of the same core version.
        return self.major, self.minor, self.patch, ((2, ""),)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._precedence() < other._precedence()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return False
        return (
            self.major, self.minor, self.patch, self.prerelease
        ) == (
            other.major, other.minor, other.patch, other.prerelease
        )

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += "-" + ".".join(self.prerelease)
        if self.build:
            text += "+" + ".".join(self.build)
        return text


@dataclass(frozen=True)
class Comparator:
    operator: str
    version: Version

    def matches(self, candidate: Version) -> bool:
        if self.operator == "=":
            return candidate == self.version
        if self.operator == ">":
            return candidate > self.version
        if self.operator == ">=":
            return candidate >= self.version
        if self.operator == "<":
            return candidate < self.version
        if self.operator == "<=":
            return candidate <= self.version
        raise AssertionError(f"unknown comparator: {self.operator}")


@dataclass(frozen=True)
class Constraint:
    """A disjunction of comparator sets (AND inside each set)."""

    alternatives: tuple[tuple[Comparator, ...], ...]
    source: str = "*"

    def matches(self, version: Version | str) -> bool:
        candidate = Version.parse(version) if isinstance(version, str) else version
        return any(
            all(comparator.matches(candidate) for comparator in group)
            for group in self.alternatives
        )

    def __str__(self) -> str:
        return self.source


_RELATIONAL_RE = re.compile(r"^(>=|<=|>|<|=)?\s*(.+)$")


def _upper_for_wildcard(parts: list[str], *, caret: bool) -> Version:
    numbers = [int(part) for part in parts if part.isdigit()]
    while len(numbers) < 3:
        numbers.append(0)
    major, minor, patch = numbers[:3]
    if caret:
        if major > 0:
            return Version(major + 1, 0, 0)
        if minor > 0:
            return Version(0, minor + 1, 0)
        return Version(0, 0, patch + 1)
    return Version(major, minor + 1, 0)


def _partial_floor(value: str) -> tuple[Version, int]:
    core = value.split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if any(part.lower() == "x" or part == "*" for part in parts):
        numeric = []
        for part in parts:
            if part.lower() == "x" or part == "*":
                break
            numeric.append(part)
        parts = numeric or ["0"]
    if len(parts) > 3:
        raise VersionError(f"非法版本约束: {value!r}")
    base = Version.parse(".".join(parts))
    return base, len(parts)


def _comparators_for_token(token: str) -> tuple[Comparator, ...]:
    match = _RELATIONAL_RE.fullmatch(token)
    if match is None:
        raise VersionError(f"非法版本约束: {token!r}")
    operator = match.group(1) or ""
    value = match.group(2).strip()
    if not value:
        raise VersionError(f"非法版本约束: {token!r}")
    if value == "*" and not operator:
        return ()
    if value.startswith(("^", "~")) and not operator:
        sigil = value[0]
        floor, count = _partial_floor(value[1:])
        if sigil == "^":
            upper = _upper_for_wildcard(
                [str(floor.major), str(floor.minor), str(floor.patch)],
                caret=True,
            )
        else:
            upper = Version(floor.major, floor.minor + 1, 0)
        return (Comparator(">=", floor), Comparator("<", upper))
    if any(part.lower() == "x" or part == "*" for part in value.split(".")):
        if operator:
            raise VersionError("通配符不能与比较运算符混用")
        floor, count = _partial_floor(value)
        if count <= 1:
            upper = Version(floor.major + 1, 0, 0)
        else:
            upper = Version(floor.major, floor.minor + 1, 0)
        return (Comparator(">=", floor), Comparator("<", upper))
    if operator:
        return (Comparator(operator, Version.parse(value)),)
    # A bare partial version behaves as a range; a full version is exact.
    floor, count = _partial_floor(value)
    if count == 3:
        return (Comparator("=", Version.parse(value)),)
    if count == 2:
        upper = Version(floor.major, floor.minor + 1, 0)
    else:
        upper = Version(floor.major + 1, 0, 0)
    return (Comparator(">=", floor), Comparator("<", upper))


def parse_constraint(value: str | Constraint | None) -> Constraint:
    if isinstance(value, Constraint):
        return value
    source = str(value or "*").strip()
    if not source:
        source = "*"
    if len(source) > 512:
        raise VersionError("版本约束过长（>512）")
    if source.count("||") > 32 or source.count(",") > 64:
        raise VersionError("版本约束分支过多")
    alternatives: list[tuple[Comparator, ...]] = []
    for alternative in source.split("||"):
        alternative = alternative.strip()
        if not alternative:
            raise VersionError(f"非法版本约束: {source!r}")
        tokens = [token.strip() for token in alternative.split(",") if token.strip()]
        if not tokens:
            raise VersionError(f"非法版本约束: {source!r}")
        group: list[Comparator] = []
        for token in tokens:
            group.extend(_comparators_for_token(token))
        alternatives.append(tuple(group))
    return Constraint(tuple(alternatives), source=source)


def highest(versions: Iterable[str], constraint: str | Constraint = "*") -> Optional[Version]:
    parsed_constraint = parse_constraint(constraint)
    candidates = [Version.parse(value) for value in versions]
    matching = [value for value in candidates if parsed_constraint.matches(value)]
    return max(matching) if matching else None


def iter_prerelease_identifiers(version: Version) -> Iterator[str]:
    yield from version.prerelease
