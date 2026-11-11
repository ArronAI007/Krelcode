"""Step 22b 测试：会话 Markdown 导出。"""

from pathlib import Path

from krelcode.core.messages import Message, ToolCall
from krelcode.session.export import export_session, render_markdown


def sample_history():
    return [
        Message.system("SYS PROMPT"),
        Message.user("写个函数"),
        Message.assistant(tool_calls=[ToolCall("c1", "write_file", {"path": "f.py", "content": "x"})]),
        Message.tool_result("c1", "已创建 f.py"),
        Message.assistant("写好了。"),
    ]


def test_render_markdown_structure():
    md = render_markdown(sample_history(), title="测试会话")
    assert md.startswith("# 测试会话")
    assert "## System" in md and "SYS PROMPT" in md
    assert "## 👤 User" in md and "写个函数" in md
    assert "## 🤖 Assistant" in md and "写好了。" in md
    assert "🔧 `write_file`" in md
    assert "Tool Result (c1)" in md and "已创建 f.py" in md


def test_render_markdown_truncates_long_parts():
    long_content = "y" * 5000
    history = [
        Message.assistant(tool_calls=[ToolCall("c", "t", {"big": "z" * 5000})]),
        Message.tool_result("c", long_content),
    ]
    md = render_markdown(history)
    assert "…[截断]" in md
    assert "…" in md
    assert len(md) < 4000


def test_export_to_explicit_path(tmp_path):
    out = tmp_path / "dir" / "s.md"
    path = export_session(sample_history(), out)
    assert path == out and out.is_file()
    assert "# Krelcode 会话" in out.read_text(encoding="utf-8")


def test_export_default_dir(tmp_path):
    export_dir = tmp_path / ".krelcode" / "exports"
    path = export_session(sample_history(), export_dir=export_dir)
    assert path.parent == export_dir
    assert path.name.startswith("session-") and path.name.endswith(".md")
    assert "写好了。" in path.read_text(encoding="utf-8")
