"""Step 22a 测试：@文件引用展开（纯函数，tmp 工作区）。"""

from pathlib import Path

import pytest

from krelcode.ui.atfiles import expand_at_files


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "a.txt").write_text("AAA 内容", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("print('b')\n", encoding="utf-8")
    (tmp_path / "bin.dat").write_bytes(b"\x00\xff\xfe")
    return tmp_path


def test_expand_existing_file(ws):
    text, notes, images = expand_at_files("看看 @a.txt 吧", ws)
    assert '<attached_file path="a.txt">' in text
    assert "AAA 内容" in text
    assert "看看" in text and "吧" in text          # 前后文保留
    assert any("已注入" in n for n in notes)
    assert images == []                            # 文本文件不产生图片附件


def test_expand_nested_path(ws):
    text, _, _images = expand_at_files("@sub/b.py", ws)
    assert 'path="sub/b.py"' in text and "print('b')" in text


def test_missing_file_kept_verbatim(ws):
    text, notes, images = expand_at_files("看 @ghost.txt", ws)
    assert "@ghost.txt" in text and "attached_file" not in text
    assert any("不存在" in n for n in notes)


def test_outside_workspace_kept_verbatim(ws):
    text, notes, images = expand_at_files("看 @../secret.txt", ws)
    assert "attached_file" not in text
    assert any("越出工作区" in n for n in notes)


def test_binary_file_kept_verbatim(ws):
    text, notes, images = expand_at_files("@bin.dat", ws)
    assert "attached_file" not in text
    assert any("非 UTF-8" in n for n in notes)


def test_truncation_with_marker(ws):
    text, notes, images = expand_at_files("@a.txt", ws, max_file_chars=3)
    assert "截断" in text and "AAA" in text
    assert "AAA 内容" not in text


def test_max_files_cap(ws):
    text, notes, images = expand_at_files("@a.txt @sub/b.py @a.txt", ws, max_files=1)
    assert text.count("<attached_file") == 1
    assert any("上限" in n for n in notes)


def test_email_not_triggered(ws):
    text, notes, images = expand_at_files("联系 user@example.com 谢谢", ws)
    assert text == "联系 user@example.com 谢谢"
    assert notes == []


def test_bare_at_and_punctuation_not_triggered(ws):
    text, _, _images = expand_at_files("@ 单独 at 符号 @！", ws)
    assert "<attached_file" not in text
