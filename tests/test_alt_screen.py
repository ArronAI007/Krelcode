"""Alt Screen terminal backend tests."""

from __future__ import annotations

import os
import io
import sys

from krelcode.ui.alt_screen import AltScreenPrompt, AltScreenSurface
from krelcode.core.events import (
    LLMStreamDelta,
    LLMThinkingDelta,
    RunFinished,
    RunStarted,
)
from krelcode.ui.render import StreamRenderer
from krelcode.ui.tui.mouse import MouseEvent


def test_alt_screen_owns_scroll_copy_click_and_restore(monkeypatch):
    output: list[str] = []
    copied: list[str] = []
    clicked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda _fallback: os.terminal_size((20, 6)))
    surface = AltScreenSurface(
        write=output.append,
        clipboard=lambda text: copied.append(text) or True,
        color=False,
    )
    surface.set_click_handler(
        lambda kind, value: clicked.append((kind, value)) or True)
    surface.start()
    try:
        surface.write_transcript(
            "line0\nline1\nline2\nline3\nline4\n"
            "line5\nline6\nline7\nline8\nline9\n"
        )
        assert surface.follow_end is True
        assert surface.scroll_top == 4

        surface.write_transcript("tool result\n")
        surface.mark_tool(10)
        assert surface.handle_mouse(MouseEvent(0, 1, 6, True))
        assert surface.handle_mouse(MouseEvent(0, 1, 6, False))
        assert clicked == [("tool", "10")]
        assert surface.handle_mouse(MouseEvent(0, 1, 6, True))
        assert surface.handle_mouse(MouseEvent(0, 1, 6, False))
        assert clicked == [("tool", "10")]

        surface.handle_viewport_key("pageup")
        assert surface.scroll_top < len(surface.transcript) - 6
        surface.handle_mouse(MouseEvent(65, 1, 1, True))

        surface.handle_viewport_key("home")
        assert surface.scroll_top == 0
        assert surface.handle_mouse(MouseEvent(0, 1, 1, True))
        assert surface.handle_mouse(MouseEvent(32, 4, 2, True))
        assert surface.handle_mouse(MouseEvent(0, 4, 2, False))
        assert copied == ["line0\nline"]
    finally:
        surface.stop(print_final=False)

    rendered = "".join(output)
    assert "\x1b[?1049h" in rendered
    assert "\x1b[?1049l" in rendered
    assert "\x1b[?1002h" in rendered
    assert "\x1b[?1002l" in rendered


def test_alt_screen_prompt_renders_fixed_input_and_status(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda _fallback: os.terminal_size((30, 8)))
    surface = AltScreenSurface(write=output.append, color=False)
    surface.start()
    try:
        prompt = AltScreenPrompt(surface, color=False)
        prompt.set_panels(["─ Context ─"])
        prompt.set_status("permission · default")
        prompt.update("❯ hello", ["/help  显示帮助"], cursor_row=0, cursor_column=7)

        rendered = "".join(output)
        assert "─ Context ─" in rendered
        assert "❯ hello" in rendered
        assert "/help" in rendered
        assert "permission · default" in rendered
        assert surface.input_visible is True

        prompt.hide()
        assert surface.input_visible is False
    finally:
        surface.stop(print_final=False)


def test_alt_screen_proxies_print_without_recursing(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda _fallback: os.terminal_size((30, 8)))
    surface = AltScreenSurface(color=False)
    surface.start()
    try:
        print("proxied output")
        assert surface.transcript[-1] == "proxied output"
    finally:
        surface.stop(print_final=False)
    rendered = stream.getvalue()
    assert "\x1b[?1049h" in rendered
    assert "\x1b[?1049l" in rendered


def test_alt_screen_keeps_one_bottom_input_and_renders_markdown(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda _fallback: os.terminal_size((50, 12)))
    surface = AltScreenSurface(write=output.append, color=False)
    surface.start()
    try:
        prompt = AltScreenPrompt(surface, color=False)
        prompt.show("❯ 你好")
        prompt.set_status("permission · default")
        prompt.submitted(raw=True)

        renderer = StreamRenderer(write=surface.write_transcript, color=False)
        answer = "当前工作区：\n\n- `quick.py`\n- **README.md**\n"
        renderer(RunStarted("你好"))
        renderer(LLMStreamDelta(answer))
        renderer(RunFinished(answer))

        assert surface.transcript[:2] == ["❯ 你好", "⏺ 当前工作区："]
        rendered = "\n".join(surface.transcript)
        assert "• quick.py" in rendered
        assert "• README.md" in rendered
        assert "`quick.py`" not in rendered
        assert "**README.md**" not in rendered
        assert surface.status == "permission · default"
        assert surface.input_visible is False
    finally:
        surface.stop(print_final=False)


def test_thinking_block_expands_and_collapses_in_place(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda _fallback: os.terminal_size((60, 14)))
    surface = AltScreenSurface(write=output.append, color=False)
    surface.start()
    surface.set_click_handler(
        lambda kind, value: (
            surface.toggle_thinking(int(value))
            if kind == "thinking" else False
        ))
    try:
        surface.begin_thinking_turn()
        surface.append_thinking_delta("# 分析\n\n- **检查文件**")
        surface.write_transcript("✻ 模型思考完成 · 点击展开\n")
        surface.mark_thinking()

        collapsed = "\n".join(surface._content_lines())
        assert "▶ ✻ Thinking · 点击展开" in collapsed
        assert "检查文件" not in collapsed

        surface.handle_mouse(MouseEvent(0, 1, 1, True))
        surface.handle_mouse(MouseEvent(0, 1, 1, False))
        expanded = "\n".join(surface._content_lines())
        assert expanded.count("Thinking") == 1
        assert "▼ ✻ Thinking · 点击折叠" in expanded
        assert "  ▌ 分析" in expanded
        assert "  • 检查文件" in expanded

        surface.handle_mouse(MouseEvent(0, 1, 1, True))
        surface.handle_mouse(MouseEvent(0, 1, 1, False))
        collapsed_again = "\n".join(surface._content_lines())
        assert "▶ ✻ Thinking · 点击展开" in collapsed_again
        assert "检查文件" not in collapsed_again
    finally:
        surface.stop(print_final=False)


def test_renderer_slash_thinking_toggles_alt_screen_block(monkeypatch):
    output: list[str] = []
    monkeypatch.setattr(
        "shutil.get_terminal_size", lambda _fallback: os.terminal_size((60, 14)))
    surface = AltScreenSurface(write=output.append, color=False)
    surface.start()
    try:
        surface.begin_thinking_turn()
        renderer = StreamRenderer(
            write=surface.write_transcript,
            color=False,
            on_thinking_line=surface.mark_thinking,
            on_thinking_delta=surface.append_thinking_delta,
            tool_lines_clickable=True,
        )
        renderer.set_thinking_expander(surface.toggle_latest_thinking)
        renderer(RunStarted("q"))
        renderer(LLMThinkingDelta("需要先分析文件"))
        renderer(LLMStreamDelta("回答"))

        assert renderer.expand_thinking()
        expanded = "\n".join(surface._content_lines())
        assert "▼ ✻ Thinking · 点击折叠" in expanded
        assert "需要先分析文件" in expanded

        assert renderer.expand_thinking()
        collapsed = "\n".join(surface._content_lines())
        assert "▶ ✻ Thinking · 点击展开" in collapsed
        assert "需要先分析文件" not in collapsed
    finally:
        surface.stop(print_final=False)
