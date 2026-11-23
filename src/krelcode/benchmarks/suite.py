"""Named live-benchmark suites and deterministic sharding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


DEFAULT_SUITE = "standard"


class BenchmarkSuiteError(ValueError):
    """A named suite or shard selector is invalid."""


@dataclass(frozen=True)
class BenchmarkSuite:
    name: str
    description: str
    case_ids: tuple[str, ...] = ()
    default_trials: int = 1

    def select(self, available: Iterable[str]) -> tuple[str, ...]:
        order = tuple(dict.fromkeys(str(item) for item in available))
        if not self.case_ids:
            return order
        missing = tuple(item for item in self.case_ids if item not in order)
        if missing:
            raise BenchmarkSuiteError(
                "suite 引用了不存在的 case: " + ", ".join(missing))
        selected = set(self.case_ids)
        return tuple(item for item in order if item in selected)


BENCHMARK_SUITES: dict[str, BenchmarkSuite] = {
    "smoke": BenchmarkSuite(
        "smoke",
        "快速验证编辑、读取、安全边界与失败恢复",
        case_ids=(
            "read-scope",
            "edit-verify",
            "permission-denied",
            "failed-test-recovery",
            "second-turn-cache-hit",
        ),
    ),
    "standard": BenchmarkSuite(
        "standard",
        "完整 16-case 行为契约，每个 case 默认 1 trial",
    ),
    "nightly": BenchmarkSuite(
        "nightly",
        "完整 suite × 3 trials，适合 nightly 稳定性回归",
        default_trials=3,
    ),
    "extended": BenchmarkSuite(
        "extended",
        "完整 suite × 5 trials，适合发布前大规模稳定性评估",
        default_trials=5,
    ),
}


def resolve_suite(name: str | None) -> BenchmarkSuite:
    key = str(name or DEFAULT_SUITE).strip().lower()
    try:
        return BENCHMARK_SUITES[key]
    except KeyError as exc:
        raise BenchmarkSuiteError(
            "未知 benchmark suite: " + key
            + "（可选: " + ", ".join(sorted(BENCHMARK_SUITES)) + ")") from exc


def parse_shard(value: str | None) -> tuple[int, int]:
    """Parse one-based ``index/count`` shard syntax."""
    if not value:
        return 1, 1
    raw = str(value).strip()
    parts = raw.split("/")
    if len(parts) != 2:
        raise BenchmarkSuiteError("shard 必须使用 <index>/<count>，例如 1/3")
    try:
        index, count = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise BenchmarkSuiteError("shard index/count 必须是整数") from exc
    if count < 1 or index < 1 or index > count:
        raise BenchmarkSuiteError("shard 必须满足 1 <= index <= count")
    return index, count


def shard_cases(case_ids: Sequence[str], shard: str | None = None) -> tuple[str, ...]:
    index, count = parse_shard(shard)
    ordered = tuple(case_ids)
    return tuple(
        case_id for position, case_id in enumerate(ordered)
        if position % count == index - 1
    )


__all__ = [
    "BENCHMARK_SUITES",
    "BenchmarkSuite",
    "BenchmarkSuiteError",
    "DEFAULT_SUITE",
    "parse_shard",
    "resolve_suite",
    "shard_cases",
]
