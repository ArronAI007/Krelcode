"""Step 4 工具单元测试：run_command / grep / glob（tmp 工作区，真实子进程）。"""

import asyncio
import os
import time
from pathlib import Path

import pytest

from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.glob import GlobTool
from krelcode.tools.builtin.grep import GrepTool
from krelcode.tools.builtin.run_command import RunCommandTool
from krelcode.tools.fsutils import truncate_middle


@pytest.fixture
def ws(tmp_path):
    return ToolContext(workspace_root=tmp_path)


def run(coro):
    return asyncio.run(coro)


# -- run_command -------------------------------------------------------------------

def test_run_command_success_merged_output(ws):
    r = run(RunCommandTool().execute({"command": "echo hello && echo err >&2"}, ws))
    assert not r.is_error
    assert r.data["exit_code"] == 0
    assert "hello" in r.content and "err" in r.content  # stdout+stderr 合并


def test_run_command_nonzero_exit_is_error(ws):
    r = run(RunCommandTool().execute({"command": "exit 3"}, ws))
    assert r.is_error and r.data["exit_code"] == 3
    assert r.content.startswith("exit_code: 3")


def test_run_command_cwd_in_workspace(ws, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("x", encoding="utf-8")
    r = run(RunCommandTool().execute({"command": "pwd && ls", "cwd": "sub"}, ws))
    assert not r.is_error and "f.txt" in r.content
    assert str((tmp_path / "sub").resolve()) in r.content


def test_run_command_cwd_outside_workspace_rejected(ws):
    r = run(RunCommandTool().execute({"command": "ls", "cwd": ".."}, ws))
    assert r.is_error and "越出工作区" in r.content


def test_run_command_timeout_kills(ws):
    start = time.monotonic()
    r = run(RunCommandTool().execute({"command": "sleep 30", "timeout": 1}, ws))
    elapsed = time.monotonic() - start
    assert r.is_error and r.data["timed_out"] and "超时" in r.content
    assert elapsed < 10  # 确实被及时终止


def test_run_command_timeout_kills_whole_group(ws):
    # 子 shell 再派生 sleep：进程组击杀应连带清理（无孤儿阻塞）
    start = time.monotonic()
    r = run(RunCommandTool().execute(
        {"command": "sh -c 'sleep 30 & sleep 30'", "timeout": 1}, ws))
    assert r.is_error and r.data["timed_out"]
    assert time.monotonic() - start < 10


def test_run_command_output_truncated(ws):
    r = run(RunCommandTool().execute(
        {"command": "python3 -c \"print('y'*100000)\""}, ws))
    assert not r.is_error
    assert "中间截断" in r.content and len(r.content) < 40_000


def test_run_command_empty_command(ws):
    r = run(RunCommandTool().execute({"command": "  "}, ws))
    assert r.is_error and "非空" in r.content


def test_truncate_middle_helper():
    assert truncate_middle("abc", 10) == "abc"
    t = truncate_middle("a" * 100 + "M" * 100 + "z" * 100, 60)
    assert "中间截断" in t and t.startswith("aaa") and t.endswith("zzz")


# -- grep ----------------------------------------------------------------------------

@pytest.fixture
def repo(ws, tmp_path):
    (tmp_path / "main.py").write_text(
        "def foo():\n    return 42\n\n\ndef bar():\n    return foo()\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# foo docs\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("foo=secret\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("foo\n", encoding="utf-8")
    (tmp_path / "data.bin").write_bytes(b"foo\x00bar")
    return ws


def test_grep_basic(repo):
    r = run(GrepTool().execute({"pattern": "foo"}, repo))
    assert not r.is_error
    assert "main.py:1: def foo():" in r.content
    assert "README.md:1: # foo docs" in r.content
    # 剪枝目录不参与搜索
    assert ".git" not in r.content and "node_modules" not in r.content
    assert r.data["count"] >= 2


def test_grep_include_filter_and_ignore_case(repo):
    r = run(GrepTool().execute({"pattern": "FOO", "include": "*.py", "ignore_case": True}, repo))
    assert "main.py" in r.content and "README.md" not in r.content

    r2 = run(GrepTool().execute({"pattern": "FOO", "include": "*.py"}, repo))
    assert "未找到匹配" in r2.content  # 大小写敏感时无匹配


def test_grep_regex_and_subdir(repo, tmp_path):
    r = run(GrepTool().execute({"pattern": r"return \d+"}, repo))
    assert "main.py:2:     return 42" in r.content

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "x.py").write_text("needle here\n", encoding="utf-8")
    r2 = run(GrepTool().execute({"pattern": "needle", "path": "pkg"}, repo))
    assert "pkg/x.py:1" in r2.content


def test_grep_invalid_regex(repo):
    r = run(GrepTool().execute({"pattern": "([a-z"}, repo))
    assert r.is_error and "非法正则" in r.content


def test_grep_max_results_truncation(repo, tmp_path):
    (tmp_path / "many.py").write_text("\n".join("hit" for _ in range(50)), encoding="utf-8")
    r = run(GrepTool().execute({"pattern": "hit", "max_results": 10}, repo))
    assert r.data["truncated"] and r.data["count"] == 10
    assert "上限" in r.content


def test_grep_binary_skipped(repo):
    r = run(GrepTool().execute({"pattern": "foo", "include": "*.bin"}, repo))
    assert "未找到匹配" in r.content  # NUL 防呆：不报二进制内容


def test_grep_path_outside_workspace(repo):
    r = run(GrepTool().execute({"pattern": "x", "path": ".."}, repo))
    assert r.is_error and "越出工作区" in r.content


# -- glob ------------------------------------------------------------------------------

def test_glob_recursive_sorted_by_mtime(ws, tmp_path):
    old = tmp_path / "old.py"; old.write_text("1", encoding="utf-8")
    os.utime(old, (1000, 1000))
    new = tmp_path / "new.py"; new.write_text("2", encoding="utf-8")
    os.utime(new, (2000, 2000))
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.py").write_text("3", encoding="utf-8")
    os.utime(tmp_path / "sub" / "deep.py", (1500, 1500))

    r = run(GlobTool().execute({"pattern": "**/*.py"}, ws))
    assert not r.is_error
    assert r.content.splitlines()[:3] == ["new.py", "sub/deep.py", "old.py"]


def test_glob_skips_pruned_and_hidden(ws, tmp_path):
    (tmp_path / "ok.py").write_text("", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("", encoding="utf-8")
    r = run(GlobTool().execute({"pattern": "**/*.py"}, ws))
    assert r.content.splitlines() == ["ok.py"]


def test_glob_no_match_and_absolute_pattern(ws, tmp_path):
    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    r = run(GlobTool().execute({"pattern": "**/*.rs"}, ws))
    assert "未找到匹配" in r.content
    r2 = run(GlobTool().execute({"pattern": "/etc/passwd"}, ws))
    assert r2.is_error and "相对模式" in r2.content


def test_glob_subdir_base(ws, tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("", encoding="utf-8")
    r = run(GlobTool().execute({"pattern": "test_*.py", "path": "tests"}, ws))
    assert r.content.splitlines() == ["tests/test_x.py"]
