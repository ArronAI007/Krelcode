#!/usr/bin/env python3
"""Reject architecture budget increases against a base revision.

The architecture tests enforce current file budgets.  This script adds the
missing temporal guarantee: an already recorded budget may shrink or disappear,
but it may never be raised.  New oversized files likewise need an explicit
review through the architecture policy rather than silently entering a baseline.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import tomllib
from typing import Any, Mapping


BASELINE_PATH = "architecture/baseline.toml"


class RatchetError(ValueError):
    """Architecture limits would regress."""


def load_baseline_bytes(data: bytes, source: str) -> dict[str, Any]:
    try:
        value = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RatchetError(f"无法解析 {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise RatchetError(f"{source} 必须是 TOML table")
    return value


def _file_budget(value: Mapping[str, Any]) -> tuple[int, dict[str, int]]:
    file_size = value.get("file_size", {})
    if not isinstance(file_size, dict):
        raise RatchetError("file_size 必须是 TOML table")
    default = int(file_size.get("max_unlisted_lines", 800))
    raw_limits = file_size.get("limits", {})
    if not isinstance(raw_limits, dict):
        raise RatchetError("file_size.limits 必须是 TOML table")
    limits: dict[str, int] = {}
    for path, limit in raw_limits.items():
        if not isinstance(path, str):
            raise RatchetError("file_size.limits 的 key 必须是字符串")
        limits[path] = int(limit)
    return default, limits


def ratchet_violations(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> list[str]:
    """Return human-readable limit increases with no override mechanism."""
    previous_default, previous_limits = _file_budget(previous)
    current_default, current_limits = _file_budget(current)
    violations: list[str] = []
    if current_default > previous_default:
        violations.append(
            "file_size.max_unlisted_lines 从 "
            f"{previous_default} 提高到 {current_default}")
    for path, limit in current_limits.items():
        old_limit = previous_limits.get(path)
        if old_limit is not None and limit > old_limit:
            violations.append(
                f"{path} 上限从 {old_limit} 提高到 {limit}")
        elif old_limit is None and limit > current_default:
            violations.append(
                f"新基线文件 {path} 的上限 {limit} 超过默认值 {current_default}")
    return violations


def _git_output(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], stderr=subprocess.PIPE)


def load_git_baseline(ref: str) -> dict[str, Any]:
    try:
        data = _git_output("show", f"{ref}:{BASELINE_PATH}")
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RatchetError(f"无法读取基线 {ref}:{BASELINE_PATH}: {message}") from exc
    return load_baseline_bytes(data, f"{ref}:{BASELINE_PATH}")


def check_repository(base_ref: str, root: Path | None = None) -> None:
    project_root = root or Path(__file__).resolve().parent.parent
    current_path = project_root / BASELINE_PATH
    current = load_baseline_bytes(current_path.read_bytes(), str(current_path))
    previous = load_git_baseline(base_ref)
    violations = ratchet_violations(previous, current)
    if violations:
        raise RatchetError("\n".join(violations))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-ref",
        default="HEAD^",
        help="Git revision containing the previously accepted baseline",
    )
    args = parser.parse_args(argv)
    try:
        check_repository(args.base_ref)
    except (RatchetError, subprocess.CalledProcessError) as exc:
        print(f"[architecture-ratchet] {exc}")
        return 1
    print("[architecture-ratchet] no architecture budget increase")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
