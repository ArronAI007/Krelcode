"""repo_map 工具测试：ast/正则符号提取、剪枝、预算截断。"""

import asyncio
from pathlib import Path

import pytest

from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.repo_map import RepoMapTool, extract_symbols

PY_SRC = '''import os


class Foo:
    """doc"""

    def method(self, x, y=1):
        return x + y

    async def amethod(self, *args, **kwargs):
        pass


def top_level(a, b):
    return a


async def async_top(x):
    return x
'''

JS_SRC = """export class Bar {
  constructor() {}
}

function helper(a, b) {
  return a;
}

const arrowFn = (x) => x * 2;
export async function fetchData(url) {}
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(PY_SRC, encoding="utf-8")
    (tmp_path / "app.js").write_text(JS_SRC, encoding="utf-8")
    (tmp_path / "broken.py").write_text("def (invalid syntax", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not code", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("def hidden():\n    pass\n", encoding="utf-8")
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def test_python_symbols_with_nesting_and_signatures(repo):
    symbols = extract_symbols(repo / "pkg" / "mod.py")
    rendered = [(indent.strip(), lineno, text) for indent, lineno, text in symbols]
    assert ("", 4, "class Foo") in rendered
    assert ("", 7, "def method(self, x, y)") in rendered        # 默认值不进签名
    assert ("", 10, "async def amethod(self, *args, **kwargs)") in rendered
    assert ("", 14, "def top_level(a, b)") in rendered
    assert ("", 18, "async def async_top(x)") in rendered
    # 嵌套方法带缩进
    method_entry = [s for s in symbols if "method(self" in s[2] and "amethod" not in s[2]][0]
    assert method_entry[0] == "  "


def test_js_symbols(repo):
    symbols = extract_symbols(repo / "app.js")
    texts = [text for _, _, text in symbols]
    assert "class Bar" in texts
    assert "function helper" in texts
    assert "const arrowFn" in texts
    assert "function fetchData" in texts


def test_broken_python_skipped(repo):
    assert extract_symbols(repo / "broken.py") == []


def test_map_output_layout_and_pruning(repo):
    ctx = ToolContext(workspace_root=repo)
    r = run(RepoMapTool().execute({}, ctx))
    assert not r.is_error
    assert "pkg/mod.py:" in r.content
    assert "app.js:" in r.content
    assert "L4 class Foo" in r.content
    # 剪枝与非源码不进地图
    assert "node_modules" not in r.content
    assert "notes.txt" not in r.content
    assert "broken.py" not in r.content
    assert r.data["files"] == 2


def test_map_subdir_path(repo):
    ctx = ToolContext(workspace_root=repo)
    r = run(RepoMapTool().execute({"path": "pkg"}, ctx))
    assert "pkg/mod.py:" in r.content and "app.js" not in r.content


def test_map_budget_truncation(repo):
    # 生成大量文件撑爆预算
    for i in range(200):
        (repo / f"gen{i:03d}.py").write_text(
            "\n".join(f"def f{i}_{j}(a, b): pass" for j in range(30)), encoding="utf-8")
    ctx = ToolContext(workspace_root=repo)
    r = run(RepoMapTool().execute({}, ctx))
    assert r.data["truncated"] is True
    assert "代码地图截断" in r.content
    assert len(r.content) < 25_000


def test_map_empty_workspace(tmp_path):
    ctx = ToolContext(workspace_root=tmp_path)
    r = run(RepoMapTool().execute({}, ctx))
    assert "未发现" in r.content


def test_map_outside_workspace_rejected(repo):
    ctx = ToolContext(workspace_root=repo)
    r = run(RepoMapTool().execute({"path": ".."}, ctx))
    assert r.is_error and "越出工作区" in r.content
