"""Pure terminal-panel model, layout, keyboard and mouse tests."""

from __future__ import annotations

from krelcode.session.state import SessionViewState
from krelcode.ui.tui.controller import PanelController
from krelcode.ui.tui.layout import display_width, render_panel_dock
from krelcode.ui.tui.model import Panel, PanelItem
from krelcode.ui.tui.mouse import parse_cursor_position, parse_sgr_mouse
from krelcode.ui.tui.panels import build_panels


def sample_panels() -> tuple[Panel, ...]:
    return (
        Panel("tasks", "Tasks", (
            PanelItem("one", "读取文件", status="✓"),
            PanelItem("two", "修改代码", status="▶"),
            PanelItem("three", "运行测试", status="○"),
        )),
        Panel("activity", "Activity", (
            PanelItem("tool", "run_command", "1/1 完成", "✓"),
        )),
        Panel("memory", "Memory", (
            PanelItem("note", "项目使用 pytest", status="•"),
        )),
    )


def test_panel_layout_has_tabs_items_hitboxes_and_cjk_width():
    layout = render_panel_dock(
        sample_panels(), active_id="tasks", selected_index=1, width=60)

    assert layout.height == 6
    assert "Tasks" in layout.lines[0] and "Memory" in layout.lines[0]
    assert "读取文件" in layout.lines[1]
    assert layout.hit_test(2, 0).item_id == ""
    assert layout.hit_test(2, 0).panel_id == "tasks"
    item_hit = layout.hit_test(2, 2)
    assert item_hit is not None and item_hit.item_id == "two"
    assert display_width("a中文") == 5
    assert all(display_width(line) <= 60 for line in layout.lines)


def test_panel_layout_truncates_to_narrow_terminal():
    for width in (5, 20):
        layout = render_panel_dock(
            sample_panels(), active_id="tasks", width=width)
        assert all(display_width(line) <= width for line in layout.lines)


def test_panel_controller_keyboard_navigation_does_not_edit_prompt():
    controller = PanelController(sample_panels())
    controller.render(60)

    assert controller.handle_key("up") is False       # input remains active
    assert controller.handle_key("ctrl-o") is True    # enter panel focus
    assert controller.focused
    assert controller.selected_index == 0
    controller.handle_key("down")
    assert controller.selected_index == 1
    controller.handle_key("down")
    controller.handle_key("down")
    assert controller.selected_index == 2
    controller.handle_key("right")
    assert controller.active_id == "activity"
    controller.handle_key("left")
    assert controller.active_id == "tasks"
    assert controller.handle_key("x") is True          # modal focus consumes text
    controller.handle_key("escape")
    assert not controller.focused


def test_panel_mouse_click_switches_tab_and_selects_item():
    controller = PanelController(sample_panels())
    controller.render(60)
    controller.set_cursor_origin(cursor_row=20, relative_cursor_row=4)
    # Dock top = 20 - 1 - 4 = 15 (zero-based); Memory tab is row 0.
    memory_tab = next(
        hit for hit in controller.layout.hitboxes
        if hit.panel_id == "memory" and hit.item_id == ""
    )
    click = f"\x1b[<0;{memory_tab.x1 + 1};{15 + 1}M"
    assert controller.handle_mouse(click)
    assert controller.active_id == "memory" and controller.focused

    controller.render(60)
    item_hit = next(
        hit for hit in controller.layout.hitboxes
        if hit.panel_id == "memory" and hit.item_id == "note"
    )
    assert controller.handle_mouse(
        f"\x1b[<0;{item_hit.x1 + 1};{15 + item_hit.y1 + 1}M")
    assert controller.selected_index == 0


def test_panel_mouse_click_can_focus_from_prompt_after_redraw():
    controller = PanelController(sample_panels())
    controller.render(60)
    controller.set_cursor_origin(cursor_row=20, relative_cursor_row=4)
    controller.render(60)  # a normal prompt refresh must not drop CPR origin

    memory_tab = next(
        hit for hit in controller.layout.hitboxes
        if hit.panel_id == "memory" and hit.item_id == ""
    )
    click = f"\x1b[<0;{memory_tab.x1 + 1};{16}M"
    assert controller.handle_mouse(click)
    assert controller.focused and controller.active_id == "memory"


def test_sgr_mouse_and_cursor_position_parser_rejects_bad_values():
    event = parse_sgr_mouse(b"\x1b[<0;12;7M")
    assert event is not None
    assert (event.button, event.x, event.y, event.pressed) == (0, 12, 7, True)
    assert event.is_left_click
    assert not parse_sgr_mouse(b"\x1b[<0;0;7M")
    assert parse_cursor_position("\x1b[24;81R") == (24, 81)
    assert parse_cursor_position("\x1b[0;1R") is None


def test_panel_view_builder_surfaces_plan_activity_context_and_memory():
    state = SessionViewState()
    state.begin_turn("执行任务")
    state.plan_items = ("读取", "修改")
    state.plan_statuses = ("completed", "in_progress")
    state.current_step = 2
    state.active_tool_name = "edit_file"
    state.tool_calls_started = 3
    state.tool_calls_completed = 2

    panels = build_panels(
        state,
        permission_mode="default",
        queue_size=2,
        context_tokens=1234,
        context_budget=8000,
        fragment_count=4,
        stable_fragments=2,
        memory_entries=(),
        memory_tokens=12,
        quick_status="shell · 运行中 · pytest",
    )
    by_id = {panel.id: panel for panel in panels}

    assert by_id["tasks"].items[0].status == "✓"
    assert by_id["tasks"].items[1].status == "▶"
    activity = " ".join(item.label for item in by_id["activity"].items)
    assert "step · 2" in activity and "shell ·" in activity
    context = " ".join(item.label for item in by_id["context"].items)
    assert "1234/8000" in context and "fragments · 4" in context
    assert by_id["memory"].items[0].id == "memory-empty"


def test_prompt_writer_renders_panel_dock_above_input():
    from krelcode.ui.repl import IDLE_PROMPT, PromptWriter

    output = []
    prompt = PromptWriter(write=output.append, enabled=True, color=False)
    layout = render_panel_dock(sample_panels(), active_id="tasks", width=60)
    prompt.set_panels(layout.lines)
    prompt.show(IDLE_PROMPT)

    rendered = "".join(output)
    assert "[Tasks]" in rendered and "读取文件" in rendered
    assert rendered.index("[Tasks]") < rendered.index(IDLE_PROMPT)
    prompt.submitted(raw=True)


def test_line_editor_panel_focus_is_modal_and_enter_returns_to_prompt(monkeypatch):
    import os
    import pty
    import threading

    from krelcode.ui import repl as repl_module
    from krelcode.ui.repl import PromptWriter, TerminalLineEditor, TerminalSubmission

    controller = PanelController(sample_panels())
    prompt = PromptWriter(enabled=False)
    editor = TerminalLineEditor(
        prompt, lambda _text: [], panels=controller)

    master_fd, slave_fd = pty.openpty()
    stream = os.fdopen(slave_fd, "r", encoding="utf-8", buffering=1)
    monkeypatch.setattr(repl_module.sys, "stdin", stream)
    monkeypatch.setattr(
        TerminalLineEditor, "available", property(lambda _self: True))

    def drain():
        try:
            while os.read(master_fd, 4096):
                pass
        except OSError:
            pass

    threading.Thread(target=drain, daemon=True).start()
    # Ctrl-O focuses Tasks; Down selects item 2; x is consumed; Enter returns
    # to the editor without submitting; the second Enter submits empty text.
    threading.Timer(
        0.05, os.write,
        args=(master_fd, b"\x0fx\x1b[B\r\r"),
    ).start()
    try:
        assert editor.readline(lambda: "❯ ") == TerminalSubmission("")
        assert not controller.focused
        assert controller.selected_index == 1
    finally:
        stream.close()
        os.close(master_fd)


def test_panel_layout_scrolls_to_keyboard_selection():
    items = tuple(
        PanelItem(f"i{index}", f"task-{index}") for index in range(8)
    )
    panel = Panel("tasks", "Tasks", items)
    layout = render_panel_dock(
        (panel,), active_id="tasks", selected_index=7, focused=True, width=50)
    rendered = "\n".join(layout.lines)
    assert "task-7" in rendered
    assert "8/8" in rendered
