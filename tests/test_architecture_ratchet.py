"""Architecture budget ratchet: limits may shrink, never grow."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).parent.parent


def load_script() -> ModuleType:
    path = ROOT / "scripts" / "check_architecture_ratchet.py"
    spec = importlib.util.spec_from_file_location("architecture_ratchet", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ratchet = load_script()


def baseline(limits: dict[str, int], default: int = 800) -> dict:
    return {
        "file_size": {
            "max_unlisted_lines": default,
            "limits": limits,
        },
    }


def test_existing_limit_may_shrink_or_disappear():
    previous = baseline({"a.py": 1200, "b.py": 900})
    current = baseline({"a.py": 1000})
    assert ratchet.ratchet_violations(previous, current) == []


def test_existing_limit_cannot_increase():
    violations = ratchet.ratchet_violations(
        baseline({"a.py": 1200}),
        baseline({"a.py": 1201}),
    )
    assert violations == ["a.py 上限从 1200 提高到 1201"]


def test_default_limit_cannot_increase():
    violations = ratchet.ratchet_violations(
        baseline({}, default=800),
        baseline({}, default=1000),
    )
    assert violations == [
        "file_size.max_unlisted_lines 从 800 提高到 1000",
    ]


def test_new_listed_file_cannot_bypass_default_limit():
    violations = ratchet.ratchet_violations(
        baseline({}),
        baseline({"new.py": 801}),
    )
    assert violations == [
        "新基线文件 new.py 的上限 801 超过默认值 800",
    ]


def test_new_listed_file_within_default_is_allowed():
    assert ratchet.ratchet_violations(
        baseline({}),
        baseline({"new.py": 800}),
    ) == []


def test_invalid_toml_is_reported():
    with pytest.raises(ratchet.RatchetError, match="无法解析"):
        ratchet.load_baseline_bytes(b"[broken", "memory")
