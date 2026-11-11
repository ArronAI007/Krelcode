"""Step 27 测试：find_refs（ast 符号引用）与 check_syntax（批量语法检查）。"""

import asyncio
from pathlib import Path

import pytest

from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.check_syntax import CheckSyntaxTool
from krelcode.tools.builtin.find_refs import FindRefsTool


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "mod.py").write_text(
        'class Foo:\n'
        '    def bar(self):\n'
        '        return 1\n'
        '\n'
        'def make_foo():\n'
        '    return Foo()\n',
        encoding="utf-8")
    (pkg / "user.py").write_text(
        'from pkg.mod import Foo\n'
        '\n'
        '# Foo 出现在注释里，find_refs 不应命中\n'
        'S = "Foo 出现在字符串里，也不应命中"\n'
        '\n'
        'def use():\n'
        '    f = Foo()\n'
        '    return f.bar()\n',
        encoding="utf-8")
    # 干扰项：语法损坏文件 / 剪枝目录 / 隐藏文件
    (tmp_path / "broken.py").write_text("def (oops:\n", encoding="utf-8")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "dep.py").write_text("Foo = 1\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("Foo = 2\n", encoding="utf-8")
    return tmp_path


# -- find_refs ------------------------------------------------------------------------

def test_find_refs_class_symbol(repo):
    ctx = ToolContext(workspace_root=repo)
    r = run(FindRefsTool().execute({"symbol": "Foo"}, ctx))
    assert not r.is_error
    assert "pkg/mod.py:1: [def] class Foo:" in r.content
    assert "pkg/user.py:1: [import] from pkg.mod import Foo" in r.content
    assert "pkg/user.py:7: [ref] f = Foo()" in r.content
    assert "pkg/mod.py:6: [ref] return Foo()" in r.content


def test_find_refs_ignores_strings_and_comments(repo):
    """grep 会命中注释/字符串里的 Foo；ast 级查找不会。"""
    ctx = ToolContext(workspace_root=repo)
    r = run(FindRefsTool().execute({"symbol": "Foo"}, ctx))
    for line in r.content.splitlines():
        if line.startswith("pkg/user.py:"):
            lineno = int(line.split(":")[1])
            assert lineno not in (3, 4), f"注释/字符串行不应命中: {line}"


def test_find_refs_method_def_and_attribute_use(repo):
    ctx = ToolContext(workspace_root=repo)
    r = run(FindRefsTool().execute({"symbol": "bar"}, ctx))
    assert "pkg/mod.py:2: [def] def bar(self):" in r.content
    assert "pkg/user.py:8: [ref] return f.bar()" in r.content


def test_find_refs_skips_pruned_hidden_and_broken(repo):
    ctx = ToolContext(workspace_root=repo)
    r = run(FindRefsTool().execute({"symbol": "Foo"}, ctx))
    assert "node_modules" not in r.content
    assert ".hidden.py" not in r.content
    assert "broken.py" not in r.content        # 语法损坏文件静默跳过


def test_find_refs_not_found_and_bad_input(repo):
    ctx = ToolContext(workspace_root=repo)
    r = run(FindRefsTool().execute({"symbol": "NoSuchSymbol"}, ctx))
    assert not r.is_error and "未找到符号" in r.content and r.data["count"] == 0
    assert run(FindRefsTool().execute({"symbol": "  "}, ctx)).is_error
    assert run(FindRefsTool().execute({"symbol": "Foo", "path": ".."}, ctx)).is_error


def test_find_refs_subdir_and_max_results(repo):
    ctx = ToolContext(workspace_root=repo)
    r = run(FindRefsTool().execute({"symbol": "Foo", "path": "pkg"}, ctx))
    assert "pkg/mod.py" in r.content and "broken.py" not in r.content

    r2 = run(FindRefsTool().execute({"symbol": "Foo", "max_results": 2}, ctx))
    assert r2.data["truncated"] is True and r2.data["count"] == 2
    assert "上限" in r2.content


# -- check_syntax -----------------------------------------------------------------------

def test_check_syntax_reports_errors_with_location(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("def f(\n", encoding="utf-8")
    ctx = ToolContext(workspace_root=tmp_path)
    r = run(CheckSyntaxTool().execute({}, ctx))
    assert r.is_error
    assert "bad.py:" in r.content and "语法问题" in r.content
    assert r.data["errors"] >= 1


def test_check_syntax_clean_dir(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import os\n", encoding="utf-8")
    ctx = ToolContext(workspace_root=tmp_path)
    r = run(CheckSyntaxTool().execute({}, ctx))
    assert not r.is_error
    assert "2 个 Python 文件无语法错误" in r.content


def test_check_syntax_single_file_and_non_python(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("x === 1\n", encoding="utf-8")
    ctx = ToolContext(workspace_root=tmp_path)
    r = run(CheckSyntaxTool().execute({"path": "bad.py"}, ctx))
    assert r.is_error and "bad.py:1:" in r.content

    txt = tmp_path / "notes.txt"
    txt.write_text("hello", encoding="utf-8")
    r2 = run(CheckSyntaxTool().execute({"path": "notes.txt"}, ctx))
    assert "未找到 Python 文件" in r2.content


def test_check_syntax_prunes_and_rejects_outside(tmp_path):
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "bad.py").write_text("def (\n", encoding="utf-8")
    (tmp_path / "good.py").write_text("x = 1\n", encoding="utf-8")
    ctx = ToolContext(workspace_root=tmp_path)
    r = run(CheckSyntaxTool().execute({}, ctx))
    assert not r.is_error                       # node_modules 里的坏文件不扫
    assert run(CheckSyntaxTool().execute({"path": ".."}, ctx)).is_error
