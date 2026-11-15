"""REPL：常驻 reader + 状态机（Step 26 重构，支持运行中 steering 与 /stop）。

结构：
- 真实 TTY 由 daemon reader 线程持续读输入 → inbox 队列；测试经 input_fn 注入，零终端
- 主循环用 asyncio.wait 同时盯 inbox 与运行中的 agent 任务：
  · IDLE：处理 /命令；普通消息（含 @文件展开）→ 启动 loop.run 后台任务
  · RUNNING：普通文本 → SessionController.steer（模型在下一 step 看到）；
    /stop 取消并消毒历史；/exit 停止并退出；其他斜杠命令给提示
- 任务自然结束后，队列里未送达的插话会被丢弃并提示（语义可见，不静默吞）
- 最终回复由 StreamRenderer 流式打印（订阅事件总线）
"""

from __future__ import annotations

import asyncio
import codecs
import os
import re
import select
import shutil
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Callable, Optional

try:  # POSIX TTY；Windows 自动退回普通 input()
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    termios = None
    tty = None

from .. import __version__
from ..agent.loop import AgentLoop
from ..agent.query import QuerySession
from ..agent.runtime import AgentRuntime
from ..session.commands import (
    CommandRegistry,
    CommandResult,
    builtin_command_registry,
)
from ..session.controller import SessionController
from ..session.input import InputAction, InputEnvelope, InputKind, InputProcessor
from ..session.manager import resolve_resume_target
from ..session.state import SessionViewState
from .render import CYAN, DIM, RESET, StreamRenderer, auto_color

RUNNING_HINT = ("  （任务运行中：直接输入=插话，/thinking 展开思考，"
                "/tool-output <编号> 展开输出，/stop 中止，/exit 退出）")
STOPPED_NOTE = "  ⎿ 已中止（历史已修复，可继续对话）"

IDLE_PROMPT = "❯ "
RUNNING_PROMPT = "↳ "
MENU_ROWS = 8

_BUILTIN_COMMANDS = builtin_command_registry()
SLASH_COMMANDS = tuple(
    (spec.invocation, spec.description) for spec in _BUILTIN_COMMANDS.list())
RUNNING_SLASH_COMMANDS = tuple(
    (spec.invocation, spec.description)
    for spec in _BUILTIN_COMMANDS.list(running=True))


def match_slash_commands(text: str, *, running: bool = False,
                         limit: int = 8,
                         registry: Optional[CommandRegistry] = None
                         ) -> list[tuple[str, str]]:
    """返回与当前输入前缀匹配的 slash 命令；进入参数后不再弹菜单。"""
    if not text.startswith("/") or any(ch.isspace() for ch in text):
        return []
    catalog = registry or _BUILTIN_COMMANDS
    return [(spec.invocation, spec.description)
            for spec in catalog.list(query=text, running=running)[:limit]]


def _stdout_write(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _display_width(text: str) -> int:
    """终端显示宽度：CJK 宽字符按 2 列计算，ANSI 控制序列按 0 计算。"""
    plain = _ANSI_RE.sub("", text)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in plain)


def _truncate_display(text: str, width: int) -> str:
    if _display_width(text) <= width:
        return text
    if width <= 1:
        return "…"[:width]
    out = []
    used = 0
    for ch in text:
        char_width = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + char_width > width - 1:
            break
        out.append(ch)
        used += char_width
    return "".join(out) + "…"


def _pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _input_rule(color: bool = False) -> str:
    """pi 风格输入区上边界；留 1 列避免终端自动换行产生空行。"""
    columns = shutil.get_terminal_size((80, 24)).columns
    rule = "─" * max(1, columns - 1)
    return f"{DIM}{rule}{RESET}" if color else rule


class PromptWriter:
    """上下边界包住输入行；TTY 用 ANSI 移动光标，非 TTY 不输出。"""

    CLEAR_LINE = "\r\x1b[2K"
    CURSOR_UP = "\x1b[1A"
    CURSOR_DOWN = "\x1b[1B"

    def __init__(self, write: Optional[Callable[[str], None]] = None,
                 enabled: Optional[bool] = None,
                 color: Optional[bool] = None) -> None:
        self._write = write or _stdout_write
        self.enabled = sys.stdout.isatty() if enabled is None else enabled
        self._color = auto_color() if color is None else color
        self._visible = False
        self._last = ""
        self._prompt_width = 0
        self._suggestions: tuple[str, ...] = ()
        self._rule = ""
        self._submitted_raw: Optional[bool] = None

    def show(self, text: str) -> None:
        if not self.enabled:
            return
        rule = _input_rule(self._color)
        # 输入框下方固定预留候选区；后续菜单只原地覆盖，不向历史追加行。
        block = f"{rule}\n{text}\n{rule}"
        for _ in range(MENU_ROWS):
            block += "\n" + self.CLEAR_LINE
        block += (self.CURSOR_UP * (MENU_ROWS + 1) + "\r"
                  + f"\x1b[{_display_width(text)}C")
        if self._visible and block == self._last:
            return
        if self._visible:
            self.hide()
        self._write(block)
        self._visible = True
        self._last = block
        self._prompt_width = _display_width(text)
        self._suggestions = ()
        self._rule = rule
        self._submitted_raw = None

    def update(self, text: str, suggestions: Optional[list[str]] = None) -> None:
        """重绘输入行及其下方候选菜单，光标最终回到输入行末尾。"""
        if not self.enabled or not self._visible:
            return
        self._clear_suggestions()
        self._write(self.CLEAR_LINE + text)
        self._prompt_width = _display_width(text)
        # 同时重绘下边界，防止终端残留上一版输入行形成“第二行提示符”。
        self._write(self.CURSOR_DOWN + "\r" + self.CLEAR_LINE + self._rule)
        self._write(self.CURSOR_UP + "\r" + f"\x1b[{self._prompt_width}C")
        self._render_suggestions(suggestions or [])

    def _render_suggestions(self, suggestions: list[str]) -> None:
        items = tuple(suggestions)
        self._suggestions = items
        if not items:
            return
        # 候选菜单在固定区域内原地覆盖，行数始终不变。
        self._write(self.CURSOR_DOWN * 2 + "\r")
        for i in range(MENU_ROWS):
            self._write(self.CLEAR_LINE)
            if i < len(items):
                self._write(items[i])
            if i + 1 < MENU_ROWS:
                self._write("\n")
        self._write(self.CURSOR_UP * (MENU_ROWS + 1) + "\r")
        self._write(f"\x1b[{self._prompt_width}C")

    def _clear_suggestions(self) -> None:
        if not self._suggestions:
            return
        self._write(self.CURSOR_DOWN * 2 + "\r")
        for i in range(MENU_ROWS):
            self._write(self.CLEAR_LINE)
            if i + 1 < MENU_ROWS:
                self._write("\n")
        self._write(self.CURSOR_UP * (MENU_ROWS + 1) + "\r")
        self._write(f"\x1b[{self._prompt_width}C")
        self._suggestions = ()

    def submitted(self, *, raw: bool = True) -> None:
        # Enter 后越过下边界，后续输出不覆盖它。
        was_visible = self.enabled and self._visible
        if was_visible:
            self._clear_suggestions()
            # 下移到输出区后必须回到第 0 列；否则命令输出的首行会从输入
            # 末尾所在列开始，表现为第一个工具/结果缩进异常。
            self._write(self.CURSOR_DOWN * (2 if raw else 1) + "\r")
        self._visible = False
        self._last = ""
        self._submitted_raw = raw if was_visible else None

    def hide(self) -> None:
        if not self.enabled or not self._visible:
            return
        self._clear_suggestions()
        # 当前光标位于中间输入行；清掉输入行与下边界，再回到输入行落笔。
        self._write(self.CLEAR_LINE)
        self._write(self.CURSOR_DOWN)
        self._write(self.CLEAR_LINE)
        self._write(self.CURSOR_UP + "\r")
        self._visible = False
        self._last = ""

    def finish(self) -> None:
        """退出前把光标移到下边界之后并补换行，避免 shell 显示 PROMPT_EOL_MARK。"""
        if not self.enabled:
            return
        if self._visible:
            self.hide()
        elif self._submitted_raw:
            # 原生编辑器提交时下移 2 行；回到下边界再换行，避免留下空行。
            self._write(self.CURSOR_UP)
        self._write("\r\n")
        self._submitted_raw = None


class InputHistory:
    """当前 REPL 会话的用户问题历史；只记录普通输入，不记录斜杠命令。"""

    def __init__(self, limit: int = 200) -> None:
        self._entries: list[str] = []
        self._limit = max(1, limit)
        self._index: Optional[int] = None
        self._draft = ""

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, text: str) -> None:
        text = text.strip()
        if not text or text.startswith("/"):
            return
        if self._entries and self._entries[-1] == text:
            return
        self._entries.append(text)
        if len(self._entries) > self._limit:
            del self._entries[:len(self._entries) - self._limit]
        self.reset_navigation()

    def reset_navigation(self) -> None:
        self._index = None
        self._draft = ""

    def previous(self, current: str) -> str:
        if not self._entries:
            return current
        if self._index is None:
            self._draft = current
            self._index = len(self._entries) - 1
        elif self._index > 0:
            self._index -= 1
        return self._entries[self._index]

    def next(self, current: str) -> str:
        if self._index is None:
            return current
        if self._index + 1 < len(self._entries):
            self._index += 1
            return self._entries[self._index]
        draft = self._draft
        self.reset_navigation()
        return draft


class TerminalLineEditor:
    """零依赖字符级编辑器：回显、slash 候选和上下键历史导航。"""

    def __init__(self, prompt: PromptWriter,
                 suggest: Callable[[str], list[tuple[str, str]]],
                 color: bool = False,
                 history: Optional[InputHistory] = None) -> None:
        self._prompt = prompt
        self._suggest = suggest
        self._color = color
        self._history = history if history is not None else InputHistory()

    @property
    def available(self) -> bool:
        return bool(termios and tty and sys.stdin.isatty())

    def readline(self, prompt_text: Callable[[], str]) -> str:
        if not self.available:
            return input("")
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        # 保留输出处理（\n 正常换行），但不让 Ctrl-C 绕过 finally 恢复终端。
        editor_settings = termios.tcgetattr(fd)
        editor_settings[3] &= ~termios.ISIG
        termios.tcsetattr(fd, termios.TCSADRAIN, editor_settings)
        buffer: list[str] = []
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._history.reset_navigation()
        try:
            while True:
                data = os.read(fd, 1)
                if not data:
                    raise EOFError
                if data == b"\x1b":
                    sequence = self._read_escape_sequence(fd)
                    self._navigate_history(sequence, buffer)
                    self._refresh(prompt_text(), buffer)
                    continue
                char = decoder.decode(data)
                if not char:
                    continue
                if char in ("\r", "\n"):
                    return "".join(buffer)
                if char in ("\x7f", "\x08"):
                    if buffer:
                        buffer.pop()
                elif char == "\x04":             # Ctrl-D
                    raise EOFError
                elif char == "\x03":             # Ctrl-C（通常由 ISIG 先行处理）
                    raise KeyboardInterrupt
                elif char == "\t":
                    self._complete(buffer)
                elif char >= " " and char != "\x7f":
                    buffer.append(char)
                self._refresh(prompt_text(), buffer)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    @staticmethod
    def _read_escape_sequence(fd: int) -> bytes:
        """读取 ESC 后紧跟的方向键序列；其他终端转义自然忽略。"""
        sequence = []
        while len(sequence) < 8 and select.select([fd], [], [], 0.01)[0]:
            sequence.append(os.read(fd, 1))
        return b"".join(sequence)

    def _navigate_history(self, sequence: bytes, buffer: list[str]) -> bool:
        """应用 ↑/↓；返回是否识别为历史导航键。"""
        if sequence in (b"[A", b"OA"):
            buffer[:] = list(self._history.previous("".join(buffer)))
            return True
        if sequence in (b"[B", b"OB"):
            buffer[:] = list(self._history.next("".join(buffer)))
            return True
        return False

    def _complete(self, buffer: list[str]) -> None:
        matches = [name for name, _ in self._suggest("".join(buffer))]
        if not matches:
            return
        if len(matches) == 1:
            buffer[:] = list(matches[0] + " ")
            return
        common = os.path.commonprefix(matches)
        if len(common) > len(buffer):
            buffer[:] = list(common)

    def _refresh(self, prompt_text: str, buffer: list[str]) -> None:
        text = prompt_text + "".join(buffer)
        matches = self._suggest("".join(buffer))
        columns = shutil.get_terminal_size((80, 24)).columns
        # 候选命令从输入正文的起始列开始，而不是依赖固定的两空格。
        # 这样首项与后续项、以及输入行里的 `/` 始终共享同一条左对齐线。
        indent = " " * _display_width(prompt_text)
        lines = []
        for name, description in matches:
            label = f"{name:<11}"
            line = _truncate_display(
                f"{indent}{label}{description}", max(1, columns - 1))
            if self._color:
                prefix = f"{indent}{CYAN}{name:<11}{RESET}"
                line = prefix + line[len(f"{indent}{label}"):]
            lines.append(line)
        self._prompt.update(text, lines)


def print_banner(loop: AgentLoop, tool_names: Optional[list],
                 session_path: Optional[str] = None,
                 write: Callable[[str], None] = print,
                 color: Optional[bool] = None) -> None:
    """启动信息卡片：模型、工作区、工具与会话，保持主流 coding agent 风格。"""
    if color is None:
        color = auto_color()
    provider = getattr(loop.llm, "name", "?")
    model = getattr(loop.llm, "model", "?")
    workspace = loop.tool_context.workspace_root
    columns = shutil.get_terminal_size((80, 24)).columns
    inner = max(36, min(columns - 4, 96))
    title = f"✻ Krelcode v{__version__}"
    title_rendered = f"{CYAN}{title}{RESET}" if color else title
    fill = max(0, inner - _display_width(title) - 3)

    write(f"  ╭─ {title_rendered} " + "─" * fill + "╮")

    def row(label: str, value: str, path: bool = False) -> None:
        available = inner - 14
        if path and _display_width(value) > available:
            value = "…" + value[-(available - 1):]
        else:
            value = _truncate_display(value, available)
        label_text = f"{label:<10}"
        rendered_label = f"{DIM}{label_text}{RESET}" if color else label_text
        write(f"  │ {_pad_display(rendered_label + value, inner - 2)} │")

    row("provider", f"{provider} · {model}")
    row("workspace", str(workspace), path=True)
    row("tools", f"{len(tool_names or [])} 个 · /tools 查看")
    if session_path:
        row("session", session_path, path=True)
    write("  ╰" + "─" * inner + "╯")

    accent = (lambda s: f"{CYAN}{s}{RESET}") if color else (lambda s: s)
    write("")
    write(f"  {accent('/help')} 查看命令 · ↑/↓ 历史问题 · "
          f"{accent('@路径')} 引用文件 · {accent('/exit')} 退出")
    write("")


async def run_repl(
    loop: AgentLoop,
    tool_names: Optional[list] = None,
    session_path: Optional[str] = None,
    sessions_dir: Optional[Path] = None,
    mcp_supervisor=None,
    mcp_reload=None,
    subagents=None,
    checkpoints=None,
    usage=None,
    model_switch=None,
    skills=None,
    skills_reload=None,
    input_fn=None,
    redactor=None,
    runtime: Optional[AgentRuntime] = None,
    input_processor: Optional[InputProcessor] = None,
) -> int:
    runtime = runtime or AgentRuntime(
        loop,
        subagents=subagents,
        mcp_supervisor=mcp_supervisor,
        mcp_reload=mcp_reload,
    )
    loop = runtime.loop
    use_color = auto_color()
    prompt = PromptWriter(color=use_color)

    def _prompt_write(s: str) -> None:
        # 任何异步输出前先清掉提示符，输出后再由主循环决定是否重绘
        prompt.hide()
        _stdout_write(s)

    renderer = StreamRenderer(write=_prompt_write, redactor=redactor)
    input_history = InputHistory()
    workspace_root = loop.tool_context.workspace_root
    processor = input_processor or InputProcessor(workspace_root)
    controller = SessionController(
        runtime,
        session_path=session_path,
        sessions_dir=sessions_dir,
        mcp_supervisor=mcp_supervisor,
        mcp_reload=mcp_reload,
        subagents=subagents,
        checkpoints=checkpoints,
        usage=usage,
        model_switch=model_switch,
        skills=skills,
        skills_reload=skills_reload,
        workspace_root=workspace_root,
        command_registry=processor.command_registry,
    )

    print_banner(loop, tool_names, session_path=session_path)

    query: Optional[QuerySession] = None
    render_task: Optional[asyncio.Task] = None
    state = SessionViewState()
    raw_inbox: asyncio.Queue = asyncio.Queue()
    action_queue: asyncio.Queue = asyncio.Queue()
    input_ready = threading.Event()

    def current_prompt() -> str:
        text = RUNNING_PROMPT if state.is_running else IDLE_PROMPT
        return f"{CYAN}{text}{RESET}" if use_color else text

    def suggest(text: str) -> list[tuple[str, str]]:
        return match_slash_commands(
            text, running=state.is_running, registry=processor.command_registry)

    editor = TerminalLineEditor(
        prompt, suggest, color=use_color, history=input_history)

    def restore_prompt() -> None:
        prompt.show(current_prompt())
        input_ready.set()

    async def reader() -> None:
        assert input_fn is not None
        while True:
            try:
                line = await input_fn("")
            except (EOFError, KeyboardInterrupt):
                await raw_inbox.put(None)
                return
            except asyncio.CancelledError:
                raise
            await raw_inbox.put(line)

    def start_stdin_reader() -> None:
        """真实终端使用 daemon 线程，避免退出时被阻塞的 input() 拖住。"""
        event_loop = asyncio.get_running_loop()

        def push(line) -> None:
            try:
                event_loop.call_soon_threadsafe(raw_inbox.put_nowait, line)
            except RuntimeError:
                pass  # 事件循环已经结束，daemon 线程可自行退出

        def read_stdin() -> None:
            while True:
                input_ready.wait()
                try:
                    state.raw_input = editor.available
                    line = editor.readline(current_prompt)
                except (EOFError, KeyboardInterrupt, OSError):
                    line = None
                input_ready.clear()
                push(line)
                if line is None:
                    return

        threading.Thread(
            target=read_stdin, name="krelcode-stdin", daemon=True,
        ).start()

    async def process_inputs() -> None:
        """后台把原始输入转换为 InputAction，前台只消费标准动作。"""
        while True:
            raw = await raw_inbox.get()
            if raw is None:
                await action_queue.put(None)
                return
            try:
                action = await processor.process(InputEnvelope(raw))
            except Exception as exc:
                print(f"  （输入处理失败: {type(exc).__name__}: {exc}）")
                action = InputAction(
                    kind=InputKind.EMPTY, raw_text=str(raw), source="terminal")
            await action_queue.put(action)

    def show_attachment_notes(action: InputAction) -> None:
        for note_text in action.notes:
            print(f"  [@] {note_text}")

    async def consume_query_events(session: QuerySession) -> None:
        async for item in session.events:
            renderer(item.event)

    def launch_prompt(
        action: InputAction,
        *,
        tool_allowlist: Optional[list[str]] = None,
    ) -> tuple[QuerySession, asyncio.Task]:
        show_attachment_notes(action)
        session = controller.start_query(
            action.text,
            images=list(action.images) or None,
            tool_allowlist=tool_allowlist,
        )
        state.begin_turn(turn_id=session.turn_id)
        if prompt.enabled:
            status = "  ⋯ 工作中 · 输入文字可插话 · /stop 中止"
            print(f"{DIM}{status}{RESET}" if use_color else status)
        consumer = asyncio.create_task(consume_query_events(session))
        return session, consumer

    def drain_steering_notes() -> None:
        """任务结束后丢弃未送达的插话（可见地），避免悄悄留到下一轮。"""
        for leftover in controller.drain_steering():
            text = getattr(leftover, "text", leftover)
            print(f"  ⎿ 任务已结束，未送达的插话已丢弃: {str(text)[:60]}")

    async def handle_command(action: InputAction):
        return await _handle_command(
            action.text, loop, renderer,
            session_path=session_path, sessions_dir=sessions_dir,
            mcp_supervisor=mcp_supervisor, mcp_reload=mcp_reload,
            subagents=subagents, checkpoints=checkpoints, usage=usage,
            restore_session=None, workspace_root=workspace_root,
            model_switch=model_switch, skills=skills,
            skills_reload=skills_reload, controller=controller,
            command_registry=processor.command_registry,
        )

    restore_prompt()
    reader_task = asyncio.create_task(reader()) if input_fn is not None else None
    if reader_task is None:
        start_stdin_reader()
    processor_task = asyncio.create_task(process_inputs())
    action_get = asyncio.create_task(action_queue.get())
    try:
        while True:
            wait_set = {action_get}
            if query is not None:
                wait_set.add(query.task)
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            # 1) 运行中的任务结束（自然完成/异常/被取消）
            if query is not None and query.task in done:
                prompt.hide()
                try:
                    query.task.result()
                except asyncio.CancelledError:
                    renderer.end_stream()
                    print(STOPPED_NOTE)
                except Exception as e:
                    renderer.end_stream()
                    print(f"错误：{type(e).__name__}: {e}")
                if render_task is not None:
                    await asyncio.gather(render_task, return_exceptions=True)
                state.finish_turn()
                query = None
                render_task = None
                drain_steering_notes()
                restore_prompt()

            if action_get not in done:
                continue
            action = action_get.result()
            prompt.submitted(raw=state.raw_input)
            action_get = asyncio.create_task(action_queue.get())

            # 2) EOF：停掉运行中任务并退出
            if action is None:
                prompt.hide()
                if query is not None:
                    state.request_stop()
                    await controller.stop()
                    if render_task is not None:
                        await asyncio.gather(render_task, return_exceptions=True)
                    state.finish_turn()
                    renderer.end_stream()
                return 0

            input_history.add(action.raw_text)

            if action.kind is InputKind.EMPTY:
                restore_prompt()
                continue

            # 3) RUNNING：控制命令、展示命令、steering
            if query is not None:
                prompt.hide()
                if action.kind is InputKind.CONTROL:
                    if action.command == "/stop":
                        state.request_stop()
                        await controller.stop()
                        if render_task is not None:
                            await asyncio.gather(render_task, return_exceptions=True)
                        state.finish_turn()
                        query = None
                        render_task = None
                        renderer.end_stream()
                        print(STOPPED_NOTE)
                        drain_steering_notes()
                        restore_prompt()
                        continue
                    if action.command == "/exit":
                        state.request_stop()
                        await controller.stop()
                        if render_task is not None:
                            await asyncio.gather(render_task, return_exceptions=True)
                        state.finish_turn()
                        renderer.end_stream()
                        return 0
                if action.kind is InputKind.UI_COMMAND:
                    await handle_command(action)
                    restore_prompt()
                    continue
                if action.kind is InputKind.COMMAND:
                    print(RUNNING_HINT)
                    restore_prompt()
                    continue
                if action.kind is InputKind.PROMPT:
                    show_attachment_notes(action)
                    preview = controller.steer(
                        action.text, images=action.images or None)
                    print(f"  ⎿ 插话已排队（模型下一步看到）: {preview}")
                restore_prompt()
                continue

            # 4) IDLE：控制命令、普通消息、业务/展示命令
            if action.kind is InputKind.CONTROL:
                prompt.hide()
                if action.command == "/exit":
                    return 0
                print("  （当前没有运行中的任务）")
                restore_prompt()
                continue

            if action.kind is InputKind.PROMPT:
                prompt.hide()
                query, render_task = launch_prompt(action)
                restore_prompt()
                continue

            prompt.hide()
            handled = await handle_command(action)
            if isinstance(handled, CommandResult) and handled.run_prompt is not None:
                # /skill 已加载完整指令，不再把 Skill 正文中的 @ 当文件引用。
                skill_prompt = InputAction(
                    kind=InputKind.PROMPT,
                    text=handled.run_prompt,
                    raw_text=action.raw_text,
                    source=action.source,
                )
                query, render_task = launch_prompt(
                    skill_prompt,
                    tool_allowlist=(
                        list(handled.tool_allowlist)
                        if handled.tool_allowlist is not None else None
                    ),
                )
            restore_prompt()
    finally:
        if reader_task is not None:
            reader_task.cancel()
        processor_task.cancel()
        action_get.cancel()
        if query is not None:
            await query.cancel()
        if render_task is not None:
            await asyncio.gather(render_task, return_exceptions=True)
        state.finish_turn()
        prompt.finish()


async def _handle_command(
    user_input,
    loop,
    renderer,
    *,
    session_path,
    sessions_dir,
    mcp_supervisor,
    mcp_reload,
    subagents,
    checkpoints,
    usage,
    restore_session,
    workspace_root,
    model_switch=None,
    skills=None,
    skills_reload=None,
    controller: Optional[SessionController] = None,
    command_registry: Optional[CommandRegistry] = None,
):
    """兼容入口：终端展示命令留在 UI，业务命令委托 SessionController。"""
    if not user_input.startswith("/"):
        return None

    # 这两类命令操作的是当前终端 renderer 的展示缓存，不属于会话业务状态。
    if user_input == "/thinking":
        if not renderer.expand_thinking():
            print("  （暂无思考内容）")
        return False
    if user_input == "/tool-output" or user_input.startswith("/tool-output "):
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1:
            if not renderer.list_tool_outputs():
                print("  （暂无可展开的工具输出）")
        elif not renderer.expand_tool_output(parts[1].strip()):
            print("  （未找到工具输出；输入 /tool-output 查看编号）")
        return False

    if controller is None:
        controller = SessionController(
            loop,
            session_path=session_path,
            sessions_dir=sessions_dir,
            mcp_supervisor=mcp_supervisor,
            mcp_reload=mcp_reload,
            subagents=subagents,
            checkpoints=checkpoints,
            usage=usage,
            model_switch=model_switch,
            skills=skills,
            skills_reload=skills_reload,
            workspace_root=workspace_root,
            command_registry=command_registry,
        )
    result = await controller.execute_command(user_input)
    for line in result.output:
        print(line)
    if result.clear_tool_outputs:
        renderer.clear_tool_outputs()
    if result.run_prompt is not None:
        return result
    return False


def _resolve_resume_target(target: str, sessions_dir: Optional[Path]) -> Optional[Path]:
    """兼容旧测试/嵌入方的导入路径。"""
    return resolve_resume_target(target, sessions_dir)
