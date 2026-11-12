"""tools/builtin 文件工具单元测试：tmp 工作区里真实读写改，不碰 LLM。"""

import asyncio
from pathlib import Path

import pytest

from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.edit_file import EditFileTool
from krelcode.tools.builtin.read_file import ReadFileTool
from krelcode.tools.builtin.write_file import WriteFileTool


@pytest.fixture
def ws(tmp_path):
    return ToolContext(workspace_root=tmp_path)


def run(coro):
    return asyncio.run(coro)


# -- read_file -------------------------------------------------------------------

def test_read_with_line_numbers(ws, tmp_path):
    (tmp_path / "a.txt").write_text("first\nsecond\nthird\n", encoding="utf-8")
    r = run(ReadFileTool().execute({"path": "a.txt"}, ws))
    assert not r.is_error
    assert r.content.splitlines() == [
        "     1→first", "     2→second", "     3→third",
    ]


def test_read_offset_limit_and_resume_note(ws, tmp_path):
    (tmp_path / "b.txt").write_text("\n".join(f"L{i}" for i in range(1, 11)), encoding="utf-8")
    r = run(ReadFileTool().execute({"path": "b.txt", "offset": 3, "limit": 4}, ws))
    lines = r.content.splitlines()
    assert lines[:4] == ["     3→L3", "     4→L4", "     5→L5", "     6→L6"]
    assert any("offset=7" in ln for ln in lines)  # 续读提示


def test_read_missing_file_and_directory(ws, tmp_path):
    r = run(ReadFileTool().execute({"path": "nope.txt"}, ws))
    assert r.is_error and "不存在" in r.content
    (tmp_path / "d").mkdir()
    r2 = run(ReadFileTool().execute({"path": "d"}, ws))
    assert r2.is_error and "目录" in r2.content


def test_read_long_line_truncated(ws, tmp_path):
    (tmp_path / "long.txt").write_text("x" * 5000 + "\n", encoding="utf-8")
    r = run(ReadFileTool().execute({"path": "long.txt"}, ws))
    assert "行截断" in r.content and len(r.content) < 5000


def test_read_empty_file(ws, tmp_path):
    (tmp_path / "e.txt").write_text("", encoding="utf-8")
    r = run(ReadFileTool().execute({"path": "e.txt"}, ws))
    assert not r.is_error and "空文件" in r.content


def test_read_binary_rejected(ws, tmp_path):
    (tmp_path / "bin.dat").write_bytes(b"\x00\xff\xfe")
    r = run(ReadFileTool().execute({"path": "bin.dat"}, ws))
    assert r.is_error and "UTF-8" in r.content


# -- 越界防护（三个工具共用） --------------------------------------------------------

@pytest.mark.parametrize("tool,args", [
    (ReadFileTool(), {"path": "../outside.txt"}),
    (WriteFileTool(), {"path": "../evil.txt", "content": "x"}),
    (EditFileTool(), {"path": "../outside.txt", "old_string": "a", "new_string": "b"}),
])
def test_paths_outside_workspace_rejected(tool, args, ws):
    r = run(tool.execute(args, ws))
    assert r.is_error and "越出工作区" in r.content


def test_absolute_path_outside_workspace_rejected(ws):
    r = run(WriteFileTool().execute({"path": "/etc/evil", "content": "x"}, ws))
    assert r.is_error and "越出工作区" in r.content


def test_symlink_escape_rejected(ws, tmp_path):
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("s", encoding="utf-8")
    try:
        link = tmp_path / "link.txt"
        link.symlink_to(outside)
        r = run(ReadFileTool().execute({"path": "link.txt"}, ws))
        assert r.is_error and "越出工作区" in r.content
    finally:
        outside.unlink(missing_ok=True)


# -- write_file --------------------------------------------------------------------

def test_write_creates_dirs_and_overwrites(ws, tmp_path):
    r1 = run(WriteFileTool().execute(
        {"path": "deep/nested/c.py", "content": "print('hi')\n"}, ws))
    assert not r1.is_error and "创建" in r1.content
    assert (tmp_path / "deep/nested/c.py").read_text(encoding="utf-8") == "print('hi')\n"

    r2 = run(WriteFileTool().execute({"path": "deep/nested/c.py", "content": "x = 1\n"}, ws))
    assert "覆盖" in r2.content
    assert (tmp_path / "deep/nested/c.py").read_text(encoding="utf-8") == "x = 1\n"


def test_write_requires_string_content(ws):
    r = run(WriteFileTool().execute({"path": "f.txt", "content": 42}, ws))
    assert r.is_error and "字符串" in r.content


def test_write_unicode_preserved(ws, tmp_path):
    run(WriteFileTool().execute({"path": "u.txt", "content": "中文🚀 café\n"}, ws))
    assert (tmp_path / "u.txt").read_text(encoding="utf-8") == "中文🚀 café\n"


# -- edit_file -----------------------------------------------------------------------

def test_edit_unique_replacement_with_diff(ws, tmp_path):
    (tmp_path / "f.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    r = run(EditFileTool().execute(
        {"path": "f.py", "old_string": "b = 2", "new_string": "b = 3"}, ws))
    assert not r.is_error and "替换 1 处" in r.content
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == "a = 1\nb = 3\n"
    assert "-b = 2" in r.data["diff"] and "+b = 3" in r.data["diff"]


def test_edit_multiple_matches_rejected_unless_replace_all(ws, tmp_path):
    (tmp_path / "f.txt").write_text("dup\ndup\n", encoding="utf-8")
    r = run(EditFileTool().execute(
        {"path": "f.txt", "old_string": "dup", "new_string": "x"}, ws))
    assert r.is_error and "2 次" in r.content and "replace_all" in r.content
    # 文件未被改动
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "dup\ndup\n"

    r2 = run(EditFileTool().execute(
        {"path": "f.txt", "old_string": "dup", "new_string": "x", "replace_all": True}, ws))
    assert not r2.is_error and r2.data["replacements"] == 2
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "x\nx\n"


def test_edit_not_found_and_noop_and_empty(ws, tmp_path):
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    r = run(EditFileTool().execute(
        {"path": "f.txt", "old_string": "bye", "new_string": "x"}, ws))
    assert r.is_error and "未找到" in r.content

    r2 = run(EditFileTool().execute(
        {"path": "f.txt", "old_string": "hello", "new_string": "hello"}, ws))
    assert r2.is_error and "相同" in r2.content

    r3 = run(EditFileTool().execute(
        {"path": "f.txt", "old_string": "", "new_string": "x"}, ws))
    assert r3.is_error and "write_file" in r3.content


def test_edit_missing_file(ws):
    r = run(EditFileTool().execute(
        {"path": "ghost.txt", "old_string": "a", "new_string": "b"}, ws))
    assert r.is_error and "不存在" in r.content


# -- edit_file 失败辅助提示（Step 20b）-----------------------------------------------------------

def test_edit_multiple_matches_error_lists_line_numbers(ws, tmp_path):
    (tmp_path / "f.py").write_text("dup = 1\nmid = 2\ndup = 1\n", encoding="utf-8")
    r = run(EditFileTool().execute(
        {"path": "f.py", "old_string": "dup = 1", "new_string": "x = 1"}, ws))
    assert r.is_error
    assert "第 1、3 行" in r.content        # 精确行号帮助 LLM 扩大上下文
    assert "replace_all" in r.content


def test_edit_not_found_shows_close_line_hint(ws, tmp_path):
    (tmp_path / "f.py").write_text(
        "def hello(name):\n    return name\n", encoding="utf-8")
    r = run(EditFileTool().execute(
        {"path": "f.py", "old_string": "def hello(nam):", "new_string": "x"}, ws))
    assert r.is_error
    assert "最接近的行" in r.content and "L1" in r.content
    assert "def hello(name):" in r.content


def test_edit_not_found_without_close_match_has_no_hint(ws, tmp_path):
    (tmp_path / "f.py").write_text("alpha\nbeta\n", encoding="utf-8")
    r = run(EditFileTool().execute(
        {"path": "f.py", "old_string": "zzzz completely different", "new_string": "x"}, ws))
    assert r.is_error and "未找到" in r.content
    assert "最接近的行" not in r.content


# -- 敏感文件读保护/脱敏（Step 45）------------------------------------------------------------

def test_read_env_file_redacts_values(ws, tmp_path):
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=sk-openai-123\n"
        "DEEPSEEK_API_KEY = \"sk-deep-456\"\n"
        "export ANTHROPIC_API_KEY=sk-ant-789\n"
        "# keep comments\n"
        "EMPTY=\n",
        encoding="utf-8",
    )
    r = run(ReadFileTool().execute({"path": ".env"}, ws))
    assert not r.is_error
    assert r.data["redacted"] is True
    # 键名与结构保留，值被隐藏
    assert "OPENAI_API_KEY=***" in r.content
    assert "DEEPSEEK_API_KEY = ***" in r.content
    assert "export ANTHROPIC_API_KEY=***" in r.content
    assert "# keep comments" in r.content
    # 任何真实值都不得出现
    for secret in ("sk-openai-123", "sk-deep-456", "sk-ant-789"):
        assert secret not in r.content


def test_read_secret_file_blocked(ws, tmp_path):
    for name in ("credentials.json", "id_rsa", "api.key"):
        (tmp_path / name).write_text("secret-content\n", encoding="utf-8")
        r = run(ReadFileTool().execute({"path": name}, ws))
        assert r.is_error
        assert "拒绝读取敏感文件" in r.content


def test_read_krelcode_toml_redacts_api_key_only(ws, tmp_path):
    (tmp_path / "krelcode.toml").write_text(
        "default_provider = \"openai\"\n\n"
        "[providers.openai]\n"
        "model = \"deepseek-chat\"\n"
        "api_key = \"sk-toml-secret\"\n"
        "api_key_env = \"DEEPSEEK_API_KEY\"\n",
        encoding="utf-8",
    )
    r = run(ReadFileTool().execute({"path": "krelcode.toml"}, ws))
    assert not r.is_error
    assert r.data["redacted"] is True
    assert "sk-toml-secret" not in r.content
    assert 'api_key = "***"' in r.content
    # 非密钥配置保持可见
    assert 'model = "deepseek-chat"' in r.content
    assert 'api_key_env = "DEEPSEEK_API_KEY"' in r.content
