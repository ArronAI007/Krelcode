"""Step 53 测试：终端 UI（启动横幅、提示符、助手侧标记、无多余换行）。"""

import os
import pty
import threading
from types import SimpleNamespace

from krelcode.core.events import (
    LLMStreamDelta, LLMThinkingDelta, RunFinished, RunStarted,
)
from krelcode.ui import repl
from krelcode.session.input import InputAction, InputKind
from krelcode.ui.editor import EditBuffer, HistorySearch
from krelcode.ui.queue import PromptQueue
from krelcode.ui.repl import (
    IDLE_PROMPT, RUNNING_PROMPT, InputHistory, PromptWriter, ReplControl,
    TerminalLineEditor, TerminalSubmission, manage_prompt_queue, print_banner,
)
from krelcode.ui.repl import match_slash_commands
from krelcode.ui.render import (
    ASSISTANT_MARK, RESULT_MARK, THINKING_COLLAPSED, THINKING_MARK,
    StreamRenderer,
)


class Buf:
    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)

    @property
    def text(self):
        return "".join(self.parts)


def make_renderer(color=False):
    buf = Buf()
    return StreamRenderer(write=buf.write, color=color), buf


# -- 提示符 -----------------------------------------------------------------------------------

def test_prompt_writer_show_hide_submitted():
    buf = Buf()
    p = PromptWriter(write=buf.write, enabled=True, color=False)

    p.show(IDLE_PROMPT)
    lines = buf.text.split("\n")
    assert lines[0] and set(lines[0]) == {"─"}
    assert lines[1] == IDLE_PROMPT
    assert lines[2].startswith(lines[0])      # 下边界与上边界等宽
    assert p.CURSOR_UP in buf.text             # 光标被移回中间输入行
    p.show(IDLE_PROMPT)                     # 幂等：不重复打印
    assert buf.text.count(IDLE_PROMPT) == 1

    before = buf.text
    p.submitted(raw=True)                   # 原生编辑器：光标越过下边界
    submitted = buf.text[len(before):]
    assert submitted.startswith("\r" + p.CURSOR_UP)
    assert submitted.endswith(p.CURSOR_DOWN * 3 + p.CLEAR_BELOW)
    assert not p._visible
    before = buf.text
    p.hide()                                # 提交后无需再清行
    assert buf.text == before

    p.show(RUNNING_PROMPT)                  # 可重绘，且可切换提示符
    assert f"\n{RUNNING_PROMPT}\n" in buf.text

    before = buf.text
    p.hide()                                # 未提交时清输入行与下边界，并回到输入行
    assert buf.text.startswith(before)
    assert buf.text.endswith("\r" + p.CURSOR_UP + p.CLEAR_BELOW)


def test_prompt_writer_disabled_is_silent():
    buf = Buf()
    p = PromptWriter(write=buf.write, enabled=False)
    p.show(IDLE_PROMPT)
    p.hide()
    p.submitted()
    assert buf.text == ""


def test_input_rule_spans_terminal_without_auto_wrap(monkeypatch):
    import krelcode.ui.repl as repl

    monkeypatch.setattr(
        repl.shutil, "get_terminal_size",
        lambda _fallback=None: os.terminal_size((40, 24)),
    )
    buf = Buf()
    p = PromptWriter(write=buf.write, enabled=True, color=False)
    p.show(IDLE_PROMPT)
    rule = buf.text.splitlines()[0]
    assert len(rule) == 39                   # 留 1 列，避免自动换行后多空行


def test_slash_commands_filter_by_prefix():
    all_commands = match_slash_commands("/")
    assert all_commands[0][0] == "/help"
    assert ("/mcp", "查看或重载 MCP") in all_commands or len(all_commands) == 9
    assert match_slash_commands("/mo") == [
        ("/model", "查看或切换模型"),
    ]
    assert match_slash_commands("/thi") == [
        ("/thinking", "展开最近一次思考"),
    ]
    assert match_slash_commands("/model gpt") == []       # 进入参数后关闭菜单
    assert match_slash_commands("/nope") == []


def test_running_suggests_thinking_stop_and_exit():
    assert match_slash_commands("/", running=True) == [
        ("/thinking", "展开最近一次思考"),
        ("/plan", "查看当前执行计划"),
        ("/tool-output", "查看或展开工具输出"),
        ("/queue", "查看或管理下一条消息队列"),
        ("/agents", "查看和控制子代理"),
        ("/jobs", "查看或控制后台 Shell 任务"),
        ("/permissions", "切换权限模式并管理会话/工作区规则"),
        ("/stop", "中止正在运行的任务"),
        ("/exit", "退出 Krelcode"),
    ]


def test_prompt_writer_renders_slash_menu_below_input():
    buf = Buf()
    p = PromptWriter(write=buf.write, enabled=True, color=False)
    p.show(IDLE_PROMPT)
    before = len(buf.parts)
    p.update(IDLE_PROMPT + "/", ["  /help       显示本帮助",
                                  "  /history    查看当前对话历史"])
    update_output = "".join(buf.parts[before:])
    assert update_output.count(f"{IDLE_PROMPT}/") == 1  # 不产生第二行重复提示符
    assert update_output.count(p._rule) == 2            # 上下边界随输入同步重绘
    assert p.CURSOR_UP in update_output                  # 光标移回输入行
    assert "  /help       显示本帮助" in buf.text
    assert "  /history    查看当前对话历史" in buf.text
    assert len(p._suggestions) == 2
    p.update(IDLE_PROMPT + "/mo", ["  /model      查看或切换模型"])
    assert p._suggestions == ("  /model      查看或切换模型",)
    before = len(buf.parts)
    p.update(IDLE_PROMPT, [])                # 撤销 `/`：候选区立即清空
    cancelled_output = "".join(buf.parts[before:])
    assert p._suggestions == ()
    assert "/model" not in cancelled_output and "/help" not in cancelled_output
    assert cancelled_output.count(IDLE_PROMPT) == 1
    p.submitted(raw=True)
    assert p._suggestions == ()


def test_prompt_writer_finish_leaves_clean_shell_line():
    buf = Buf()
    p = PromptWriter(write=buf.write, enabled=True, color=False)
    p.show(IDLE_PROMPT)
    p.submitted(raw=True)
    before = buf.text
    p.finish()
    assert buf.text == before                    # 已提交后光标位于下边界之后

    p.show(IDLE_PROMPT)
    before = buf.text
    p.finish()
    assert buf.text.startswith(before)
    assert buf.text.endswith(p.CLEAR_BELOW + "\r\n")


def test_input_history_navigation_restores_draft():
    history = InputHistory()
    history.add("第一个问题")
    history.add("第一个问题")                  # 连续重复不重复记录
    history.add("/help")                      # 斜杠命令不进入问题历史
    history.add("第二个问题")
    assert len(history) == 2

    assert history.previous("未提交草稿") == "第二个问题"
    assert history.previous("") == "第一个问题"
    assert history.previous("") == "第一个问题"   # 到最旧后停住
    assert history.next("") == "第二个问题"
    assert history.next("") == "未提交草稿"       # 越过最新后恢复草稿
    assert history.next("继续编辑") == "继续编辑"  # 已在草稿区时 Down 不跳回历史


def test_line_editor_up_down_keys_navigate_history():
    buf = Buf()
    prompt = PromptWriter(write=buf.write, enabled=True, color=False)
    history = InputHistory()
    history.add("历史问题一")
    history.add("历史问题二")
    editor = TerminalLineEditor(
        prompt, match_slash_commands, color=False, history=history)
    assert editor._history is history             # 空历史也不能被替换成新对象

    buffer = list("当前草稿")
    assert editor._navigate_history(b"[A", buffer) is True
    assert "".join(buffer) == "历史问题二"
    assert editor._navigate_history(b"[A", buffer) is True
    assert "".join(buffer) == "历史问题一"
    assert editor._navigate_history(b"[B", buffer) is True
    assert "".join(buffer) == "历史问题二"
    assert editor._navigate_history(b"[B", buffer) is True
    assert "".join(buffer) == "当前草稿"
    assert editor._navigate_history(b"[C", buffer) is False

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"[A")
        assert editor._read_escape_sequence(read_fd) == b"[A"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_line_editor_escape_and_alt_enter_controls():
    buffer = list("下一条")
    assert TerminalLineEditor._apply_escape_sequence(b"", buffer) is ReplControl.INTERRUPT
    queued = TerminalLineEditor._apply_escape_sequence(b"\r", buffer)
    assert queued == TerminalSubmission("下一条", queue_next=True)
    assert TerminalLineEditor._apply_escape_sequence(b"[A", buffer) is None


def test_line_editor_cursor_edit_and_multiline_escape_sequences():
    buffer = EditBuffer("abc def", cursor=0)
    assert TerminalLineEditor._apply_escape_sequence(b"[C", buffer) is None
    assert TerminalLineEditor._apply_escape_sequence(b"[C", buffer) is None
    assert buffer.cursor == 2
    TerminalLineEditor._apply_escape_sequence(b"[D", buffer)
    assert buffer.cursor == 1
    TerminalLineEditor._apply_escape_sequence(b"[3~", buffer)
    assert buffer.text == "ac def"
    TerminalLineEditor._apply_escape_sequence(b"[F", buffer)
    assert buffer.cursor == len(buffer.text)
    TerminalLineEditor._apply_escape_sequence(b"b", buffer)
    assert buffer.cursor == len("ac ")
    TerminalLineEditor._apply_escape_sequence(b"[H", buffer)
    assert buffer.cursor == 0
    TerminalLineEditor._apply_escape_sequence(b"[13;2u", buffer)
    assert buffer.text.startswith("\nac def")


def test_bracketed_paste_reads_once_and_removes_terminal_controls():
    editor = TerminalLineEditor(PromptWriter(enabled=False), match_slash_commands)
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, "第一行\r\n\x1b[31mred\x00".encode() + b"\x1b[201~")
        os.close(write_fd)
        paste = editor._read_bracketed_paste(read_fd)
    finally:
        os.close(read_fd)
    assert paste == "第一行\n[31mred"


def test_line_editor_readline_handles_keys_and_bracketed_paste(monkeypatch):
    master_fd, slave_fd = pty.openpty()
    stream = os.fdopen(slave_fd, "r", encoding="utf-8", buffering=1)
    monkeypatch.setattr(repl.sys, "stdin", stream)
    monkeypatch.setattr(
        TerminalLineEditor, "available", property(lambda _self: True))

    def drain_terminal_output():
        try:
            while os.read(master_fd, 4096):
                pass
        except OSError:
            pass

    drainer = threading.Thread(target=drain_terminal_output, daemon=True)
    drainer.start()
    writer = threading.Timer(
        0.05,
        os.write,
        args=(master_fd, b"ab\x1b[DZ\x1b[200~x\r\ny\x1b[201~\x1b[DX\r"),
    )
    writer.start()
    try:
        prompt = PromptWriter(enabled=False)
        editor = TerminalLineEditor(prompt, match_slash_commands, color=False)
        assert editor.readline(lambda: IDLE_PROMPT) == TerminalSubmission("aZx\nXyb")
    finally:
        writer.cancel()
        stream.close()
        os.close(master_fd)


def test_multiline_refresh_tracks_cursor_row_and_cjk_column():
    buf = Buf()
    p = PromptWriter(write=buf.write, enabled=True, color=False)
    p.show(IDLE_PROMPT)
    editor = TerminalLineEditor(p, match_slash_commands, color=False)
    buffer = EditBuffer("first\n中文x", cursor=len("first\n中"))
    editor._refresh(IDLE_PROMPT, buffer)
    assert "❯ first\n  中文x" in buf.text
    assert p._last[-2:] == (1, 2)
    assert p._suggestions == ()


def test_history_search_updates_buffer_and_renders_status():
    buf = Buf()
    p = PromptWriter(write=buf.write, enabled=True, color=False)
    p.show(IDLE_PROMPT)
    history = InputHistory()
    history.add("fix parser bug")
    history.add("fix cursor bug")
    editor = TerminalLineEditor(
        p, match_slash_commands, color=False, history=history)
    buffer = EditBuffer("draft", cursor=5)
    search = HistorySearch.begin(buffer)
    search.query = "fix"
    assert editor._run_history_search(search, buffer) is True
    assert buffer.text == "fix cursor bug"
    editor._refresh(IDLE_PROMPT, buffer, search=search)
    assert "(reverse-i-search) `fix`: fix cursor bug" in buf.text


def test_prompt_queue_command_views_deletes_moves_and_clears():
    queue = PromptQueue[InputAction]()
    for text in ("one", "two", "three"):
        queue.append(InputAction(kind=InputKind.PROMPT, text=text, raw_text=text))
    output = []
    manage_prompt_queue("", queue, write=output.append)
    assert output == [
        "下一条消息队列（3）:",
        "  #1 one",
        "  #2 two",
        "  #3 three",
    ]
    manage_prompt_queue("delete 2", queue, write=output.append)
    manage_prompt_queue("move 2 1", queue, write=output.append)
    assert [entry.text for entry in queue.entries] == ["three", "one"]
    manage_prompt_queue("clear", queue, write=output.append)
    assert not queue


def test_line_editor_filters_menu_and_tab_completes():
    buf = Buf()
    p = PromptWriter(write=buf.write, enabled=True, color=False)
    p.show(IDLE_PROMPT)
    editor = TerminalLineEditor(p, match_slash_commands, color=False)
    editor._refresh(IDLE_PROMPT, list("/m"))
    assert len(p._suggestions) == 2
    assert any("/mcp" in line for line in p._suggestions)
    assert any("/model" in line for line in p._suggestions)

    chars = list("/mod")
    editor._complete(chars)
    assert "".join(chars) == "/model "


def test_slash_menu_uses_prompt_width_as_shared_left_gutter():
    buf = Buf()
    p = PromptWriter(write=buf.write, enabled=True, color=False)
    prompt = IDLE_PROMPT
    p.show(prompt)
    editor = TerminalLineEditor(p, match_slash_commands, color=False)
    editor._refresh(prompt, list("/"))

    gutter = " " * repl._display_width(prompt)
    assert len(gutter) == 2       # ❯ 占 1 列 + 1 个空格
    assert len(p._suggestions) > 1
    assert all(line.startswith(gutter + "/") for line in p._suggestions)
    assert all(line.index("/") == len(gutter) for line in p._suggestions)


def test_prompt_submission_resets_output_column_to_zero():
    buf = Buf()
    p = PromptWriter(write=buf.write, enabled=True, color=False)
    p.show(IDLE_PROMPT)
    p.update(IDLE_PROMPT + "/tools", [])
    before = len(buf.parts)
    p.submitted(raw=True)
    submitted_output = "".join(buf.parts[before:])
    assert submitted_output.startswith("\r" + p.CURSOR_UP)
    assert submitted_output.endswith(p.CURSOR_DOWN * 3 + p.CLEAR_BELOW)


# -- 启动横幅 ---------------------------------------------------------------------------------

def test_banner_contains_key_info(tmp_path):
    loop = SimpleNamespace(
        llm=SimpleNamespace(name="openai", model="deepseek-flash"),
        tool_context=SimpleNamespace(workspace_root=tmp_path),
    )
    lines = []
    print_banner(loop, ["get_time", "run_command"], session_path="/tmp/s.jsonl",
                 write=lines.append, color=False)
    text = "\n".join(lines)
    assert "╭─" in text and "╰" in text
    assert "Krelcode v" in text
    assert "provider" in text and "openai" in text and "deepseek-flash" in text
    assert "workspace" in text and tmp_path.name in text
    assert "tools" in text and "2" in text
    assert "session" in text and "/tmp/s.jsonl" in text
    assert "/help" in text and "/exit" in text


# -- 助手侧标记 -------------------------------------------------------------------------------

def test_renderer_uses_assistant_side_markers():
    r, buf = make_renderer()
    r(RunStarted("问题"))
    r(LLMThinkingDelta("先想一下"))
    r(LLMStreamDelta("答案"))
    r(RunFinished("答案"))
    text = buf.text
    assert text.startswith(THINKING_COLLAPSED)
    assert f"{THINKING_MARK}Thinking…" in text
    assert "正文已折叠" in text
    assert "下方为工具活动" in text
    assert "先想一下" not in text                   # thinking 默认折叠为一行
    assert f"{ASSISTANT_MARK}答案" in text
    # 不再出现旧式用户侧标签
    assert "[thinking]" not in text and "agent>" not in text


def test_tool_lines_use_tree_markers():
    from krelcode.core.events import ToolCallCompleted, ToolCallStarted
    from krelcode.core.messages import ToolCall

    r, buf = make_renderer()
    tc = ToolCall("c1", "run_command", {"command": "pwd"})
    r(ToolCallStarted(tc))
    r(ToolCallCompleted(tc, "exit_code: 0\n/tmp", False))
    text = buf.text
    assert f"  {ASSISTANT_MARK}run_command(" in text
    assert f"  {RESULT_MARK}✓ run_command #1" in text
    assert "输出已折叠（/tool-output 1 展开）" in text
    assert "exit_code: 0" not in text
    assert r.expand_tool_output("1")
    assert "│ exit_code: 0\n" in buf.text and "│ /tmp" in buf.text


# -- 回答结束不产生多余空行 ---------------------------------------------------------------------

def test_run_finished_no_blank_line():
    r, buf = make_renderer()
    r(LLMStreamDelta("回答"))
    r(RunFinished("回答"))
    assert buf.text == "⏺ 回答\n"
    assert "\n\n" not in buf.text


def test_answer_returns_directly_to_bounded_input_prompt():
    buf = Buf()
    r = StreamRenderer(write=buf.write, color=False)
    p = PromptWriter(write=buf.write, enabled=True, color=False)
    r(LLMStreamDelta("回答\n"))
    r(RunFinished("回答\n"))
    p.show(IDLE_PROMPT)
    lines = buf.text.split("\n")
    assert lines[0] == "⏺ 回答"
    assert lines[1] and set(lines[1]) == {"─"}
    assert lines[2] == IDLE_PROMPT
    assert lines[3].startswith(lines[1])     # 下边界
