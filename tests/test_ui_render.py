"""ui/render 测试：流式渲染器（注入 write，无真实终端）与 diff 上色。"""

import pytest

from krelcode.core.events import (
    ContextCompressed, ErrorOccurred, LLMStreamDelta, PermissionDenied,
    RunFinished, ToolCallCompleted, ToolCallStarted,
)
from krelcode.core.messages import ToolCall
from krelcode.ui.render import GREEN, RED, RESET, StreamRenderer, colorize_diff


class Buffer:
    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)

    @property
    def text(self):
        return "".join(self.parts)


def make_renderer(color=False):
    buf = Buffer()
    return StreamRenderer(write=buf.write, color=color), buf


# -- 流式渲染 -------------------------------------------------------------------------

def test_streaming_deltas_rendered_incrementally():
    r, buf = make_renderer()
    r(LLMStreamDelta("Hel"))
    assert buf.text == "⏺ Hel"          # 首个 delta 带助手标记，立即输出
    r(LLMStreamDelta("lo"))
    assert buf.text == "⏺ Hello"
    r(RunFinished("Hello"))
    assert buf.text == "⏺ Hello\n"      # 结束补换行，且不重复打印全文


def test_model_trailing_newline_does_not_create_blank_line():
    r, buf = make_renderer()
    r(LLMStreamDelta("回答"))
    r(LLMStreamDelta("\n"))
    r(RunFinished("回答\n"))
    assert buf.text == "⏺ 回答\n"
    assert "\n\n" not in buf.text


def test_unstreamed_final_text_printed_once():
    r, buf = make_renderer()
    r(RunFinished("[krelcode] 判定为循环，已中止本轮。"))
    assert buf.text == "⏺ [krelcode] 判定为循环，已中止本轮。\n"


def test_unstreamed_final_text_with_newline_has_single_terminator():
    r, buf = make_renderer()
    r(RunFinished("已中止\n"))
    assert buf.text == "⏺ 已中止\n"
    assert "\n\n" not in buf.text


def test_tool_activity_interrupts_stream():
    r, buf = make_renderer()
    tc = ToolCall("c1", "get_time", {})
    r(LLMStreamDelta("让我想想"))
    r(ToolCallStarted(tc))
    r(ToolCallCompleted(tc, "12:00", False))
    r(LLMStreamDelta("现在是12点"))
    r(RunFinished("让我想现在是12点"))
    lines = buf.text.splitlines()
    assert lines[0] == "⏺ 让我想想"
    assert "⏺ get_time" in lines[1]
    assert "⎿ ✓ get_time → 12:00" in lines[2]
    assert lines[3] == "⏺ 现在是12点"


def test_error_and_denied_and_compressed_lines():
    r, buf = make_renderer()
    tc = ToolCall("c1", "rm", {})
    r(PermissionDenied(tc, "危险命令"))
    r(ContextCompressed(1000, 200))
    r(ErrorOccurred("boom", "pytest"))
    text = buf.text
    assert "⎿ ✗ rm 被拒绝: 危险命令" in text
    assert "⎿ 历史已压缩: 1000 → 200 tokens" in text
    assert "⎿ ✗ pytest: boom" in text


def test_long_tool_content_truncated():
    r, buf = make_renderer()
    tc = ToolCall("c1", "grep", {})
    r(ToolCallCompleted(tc, "y" * 5000, False))
    assert "工具输出已截断" in buf.text
    assert len(buf.text) < 3200


def test_multiline_tool_result_preserves_code_layout():
    r, buf = make_renderer()
    tc = ToolCall("c1", "read_file", {"path": "agent.py"})
    content = (
        "     1→def run():\n"
        "     2→    if ready:\n"
        "     3→        return 'ok'"
    )
    r(ToolCallCompleted(tc, content, False))
    assert buf.text == (
        "  ⎿ ✓ read_file\n"
        "         1→def run():\n"
        "         2→    if ready:\n"
        "         3→        return 'ok'\n"
    )


def test_multiline_tool_arguments_are_indented():
    r, buf = make_renderer()
    tc = ToolCall("c1", "run_command", {
        "command": "set -e\necho ready\npytest -q",
        "timeout": 60,
    })
    r(ToolCallStarted(tc))
    assert buf.text == (
        "  ⏺ run_command(\n"
        "    command:\n"
        "      set -e\n"
        "      echo ready\n"
        "      pytest -q\n"
        "    timeout=60\n"
        "  )\n"
    )


# -- diff 上色 ---------------------------------------------------------------------------

def test_edit_file_diff_colorized():
    r, buf = make_renderer(color=True)
    tc = ToolCall("c1", "edit_file", {})
    diff = "已编辑 f.py（替换 1 处）\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-a = 1\n+a = 2"
    r(ToolCallCompleted(tc, diff, False))
    assert GREEN in buf.text and RED in buf.text and RESET in buf.text
    assert "✓ edit_file: 已编辑 f.py（替换 1 处）" in buf.text


def test_edit_file_error_not_treated_as_diff():
    r, buf = make_renderer(color=True)
    tc = ToolCall("c1", "edit_file", {})
    r(ToolCallCompleted(tc, "-未找到 old_string", True))
    assert GREEN not in buf.text  # 错误信息不走 diff 上色


def test_colorize_diff_pure_function():
    diff = "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n ctx"
    colored = colorize_diff(diff, enabled=True)
    assert f"{RED}-old{RESET}" in colored
    assert f"{GREEN}+new{RESET}" in colored
    assert colorize_diff(diff, enabled=False) == diff
