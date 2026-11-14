"""REPL：常驻 reader + 状态机（Step 26 重构，支持运行中 steering 与 /stop）。

结构：
- 真实 TTY 由 daemon reader 线程持续读输入 → inbox 队列；测试经 input_fn 注入，零终端
- 主循环用 asyncio.wait 同时盯 inbox 与运行中的 agent 任务：
  · IDLE：处理 /命令；普通消息（含 @文件展开）→ 启动 loop.run 后台任务
  · RUNNING：普通文本 → loop.steering_queue（模型在下一 step 看到）；
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
from ..session.export import export_session
from ..session.manager import format_sessions, list_sessions
from .atfiles import expand_at_files
from .render import CYAN, DIM, RESET, StreamRenderer, auto_color

HELP_TEXT = """可用命令：
  /help          显示本帮助
  /history       查看当前对话历史
  /thinking      展开最近一次思考（保留 Markdown 多行）
  /tool-output   列出工具输出；/tool-output <编号|last> 展开灰色面板
  /tools         列出已注册工具
  /session       显示当前会话文件路径
  /sessions      列出工作区的全部会话
  /resume <n>    切换到第 n 个会话（/sessions 里的序号；latest=最新）
  /mcp           查看 MCP 服务器状态；/mcp reload 重读配置并热重启
  /agents        列出子代理（后台任务）状态
  /skills        列出 Skill；/skills reload 热重载；/skills show <name> 查看详情
  /skill <name> [参数]  用户主动加载并执行一个 Skill
  /undo          回滚上一轮对话的文件改动（检查点恢复；/undo list 查看栈）
  /cost          显示 token 用量（本轮 + 会话累计 + 估算成本）
  /model [p/m]   查看/热切换模型（"gpt-4o" 沿用当前 provider，
                 "anthropic/claude-sonnet-4-5" 跨 provider）
  /allow         查看命令审批白名单；/allow add <程序名> 添加、
                 /allow remove <程序名> 移除（持久化到工作区）
  /context       显示上下文统计；/context refresh 重新采集工作区上下文
  /export [路径] 把当前会话导出为 Markdown（缺省存 .krelcode/exports/）
  /compact       立即压缩对话历史（LLM 摘要，不必等预算超限）
  /stop          中止正在运行的任务（历史自动修复）
  /reset         清空对话历史
  /exit          退出（Ctrl-D / Ctrl-C 同样有效）
任务运行中：直接输入文字 = 插话（steering）；/thinking 展开思考；
/tool-output <编号> 展开工具输出；/stop 中止。
输入 / 会实时显示匹配命令，Tab 可补全唯一匹配；↑/↓ 调取本次会话的历史问题。
其他任何输入都会作为消息发给 agent；消息里可用 @路径 引用文件。"""

RUNNING_HINT = ("  （任务运行中：直接输入=插话，/thinking 展开思考，"
                "/tool-output <编号> 展开输出，/stop 中止，/exit 退出）")
STOPPED_NOTE = "  ⎿ 已中止（历史已修复，可继续对话）"

IDLE_PROMPT = "❯ "
RUNNING_PROMPT = "↳ "
MENU_ROWS = 8

SLASH_COMMANDS = (
    ("/help", "显示本帮助"),
    ("/history", "查看当前对话历史"),
    ("/thinking", "展开最近一次思考"),
    ("/tool-output", "查看或展开工具输出"),
    ("/tools", "列出已注册工具"),
    ("/session", "显示当前会话文件"),
    ("/sessions", "列出工作区会话"),
    ("/resume", "恢复指定会话"),
    ("/mcp", "查看或重载 MCP"),
    ("/agents", "查看子代理状态"),
    ("/skills", "查看或重载 Skills"),
    ("/skill", "调用指定 Skill"),
    ("/undo", "回滚上一轮文件改动"),
    ("/cost", "查看 token 用量与成本"),
    ("/model", "查看或切换模型"),
    ("/allow", "管理命令审批白名单"),
    ("/context", "查看或刷新上下文"),
    ("/export", "导出当前会话"),
    ("/compact", "立即压缩历史"),
    ("/stop", "中止正在运行的任务"),
    ("/reset", "清空对话历史"),
    ("/exit", "退出 Krelcode"),
)

RUNNING_SLASH_COMMANDS = tuple(
    item for item in SLASH_COMMANDS
    if item[0] in {"/thinking", "/tool-output", "/stop", "/exit"}
)


def match_slash_commands(text: str, *, running: bool = False,
                         limit: int = 8) -> list[tuple[str, str]]:
    """返回与当前输入前缀匹配的 slash 命令；进入参数后不再弹菜单。"""
    if not text.startswith("/") or any(ch.isspace() for ch in text):
        return []
    source = RUNNING_SLASH_COMMANDS if running else SLASH_COMMANDS
    matches = [item for item in source if item[0].startswith(text)]
    return matches[:limit]


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
) -> int:
    from ..app import restore_session  # 延迟导入避免 ui→app 循环依赖

    use_color = auto_color()
    prompt = PromptWriter(color=use_color)

    def _prompt_write(s: str) -> None:
        # 任何异步输出前先清掉提示符，输出后再由主循环决定是否重绘
        prompt.hide()
        _stdout_write(s)

    renderer = StreamRenderer(write=_prompt_write, redactor=redactor)
    input_history = InputHistory()
    unsubscribe = loop.bus.subscribe(renderer)
    workspace_root = loop.tool_context.workspace_root

    print_banner(loop, tool_names, session_path=session_path)

    running: Optional[asyncio.Task] = None
    state = {"running": False, "raw_input": False}
    inbox: asyncio.Queue = asyncio.Queue()
    input_ready = threading.Event()

    def current_prompt() -> str:
        text = RUNNING_PROMPT if state["running"] else IDLE_PROMPT
        return f"{CYAN}{text}{RESET}" if use_color else text

    def suggest(text: str) -> list[tuple[str, str]]:
        return match_slash_commands(text, running=state["running"])

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
                await inbox.put(None)
                return
            except asyncio.CancelledError:
                raise
            await inbox.put(line)

    def start_stdin_reader() -> None:
        """真实终端使用 daemon 线程，避免退出时被阻塞的 input() 拖住。"""
        event_loop = asyncio.get_running_loop()

        def push(line) -> None:
            try:
                event_loop.call_soon_threadsafe(inbox.put_nowait, line)
            except RuntimeError:
                pass  # 事件循环已经结束，daemon 线程可自行退出

        def read_stdin() -> None:
            while True:
                input_ready.wait()
                try:
                    state["raw_input"] = editor.available
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

    def drain_steering_notes() -> None:
        """任务结束后丢弃未送达的插话（可见地），避免悄悄留到下一轮。"""
        while not loop.steering_queue.empty():
            try:
                leftover = loop.steering_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            print(f"  ⎿ 任务已结束，未送达的插话已丢弃: {str(leftover)[:60]}")

    restore_prompt()
    reader_task = asyncio.create_task(reader()) if input_fn is not None else None
    if reader_task is None:
        start_stdin_reader()
    inbox_get = asyncio.create_task(inbox.get())
    try:
        while True:
            wait_set = {inbox_get}
            if running is not None:
                wait_set.add(running)
            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            # 1) 运行中的任务结束（自然完成/异常/被取消）
            if running is not None and running in done:
                prompt.hide()
                state["running"] = False
                try:
                    running.result()
                except asyncio.CancelledError:
                    renderer.end_stream()
                    print(STOPPED_NOTE)
                except Exception as e:
                    renderer.end_stream()
                    print(f"错误：{type(e).__name__}: {e}")
                running = None
                drain_steering_notes()
                restore_prompt()

            if inbox_get not in done:
                continue
            item = inbox_get.result()
            prompt.submitted(raw=state["raw_input"])
            inbox_get = asyncio.create_task(inbox.get())

            # 2) EOF：停掉运行中任务并退出
            if item is None:
                prompt.hide()
                if running is not None:
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    renderer.end_stream()
                return 0

            user_input = item.strip()
            input_history.add(user_input)

            # 3) RUNNING 状态：steering / /thinking / /stop / /exit
            if running is not None:
                prompt.hide()
                if user_input == "/stop":
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    state["running"] = False
                    running = None
                    renderer.end_stream()
                    print(STOPPED_NOTE)
                    drain_steering_notes()
                    restore_prompt()
                    continue
                if user_input == "/exit":
                    running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
                    renderer.end_stream()
                    return 0
                if user_input == "/thinking":
                    if not renderer.expand_thinking():
                        print("  （思考内容尚未生成）")
                    restore_prompt()
                    continue
                if user_input == "/tool-output" or user_input.startswith("/tool-output "):
                    parts = user_input.split(maxsplit=1)
                    if len(parts) == 1:
                        if not renderer.list_tool_outputs():
                            print("  （暂无可展开的工具输出）")
                    elif not renderer.expand_tool_output(parts[1].strip()):
                        print("  （未找到工具输出；输入 /tool-output 查看编号）")
                    restore_prompt()
                    continue
                if user_input.startswith("/"):
                    print(RUNNING_HINT)
                    restore_prompt()
                    continue
                if user_input:
                    loop.steering_queue.put_nowait(user_input)
                    preview = user_input if len(user_input) <= 40 else user_input[:40] + "…"
                    print(f"  ⎿ 插话已排队（模型下一步看到）: {preview}")
                restore_prompt()
                continue

            # 4) IDLE 状态：命令与消息
            if not user_input:
                restore_prompt()
                continue
            if user_input == "/exit":
                prompt.hide()
                return 0
            if user_input == "/stop":
                prompt.hide()
                print("  （当前没有运行中的任务）")
                restore_prompt()
                continue
            prompt.hide()
            handled = await _handle_command(
                user_input, loop, renderer,
                session_path=session_path, sessions_dir=sessions_dir,
                mcp_supervisor=mcp_supervisor, mcp_reload=mcp_reload,
                subagents=subagents, checkpoints=checkpoints, usage=usage,
                restore_session=restore_session, workspace_root=workspace_root,
                model_switch=model_switch, skills=skills,
                skills_reload=skills_reload,
            )
            if handled is not None and not isinstance(handled, str):
                restore_prompt()
                continue
            run_input = handled if isinstance(handled, str) else user_input
            # /skill 已加载完整指令，不能再把正文里的 @作用域 误当文件引用；
            # 普通消息仍执行 @ 文本/图片展开。
            if isinstance(handled, str):
                expanded, at_notes, at_images = run_input, [], []
            else:
                expanded, at_notes, at_images = expand_at_files(
                    run_input, workspace_root)
            for note_text in at_notes:
                print(f"  [@] {note_text}")
            state["running"] = True
            if prompt.enabled:
                status = "  ⋯ 工作中 · 输入文字可插话 · /stop 中止"
                print(f"{DIM}{status}{RESET}" if use_color else status)
            running = asyncio.create_task(loop.run(expanded, images=at_images or None))
            restore_prompt()
    finally:
        if reader_task is not None:
            reader_task.cancel()
        inbox_get.cancel()
        prompt.finish()
        unsubscribe()


async def _handle_command(user_input, loop, renderer, *, session_path, sessions_dir,
                          mcp_supervisor, mcp_reload, subagents, checkpoints,
                          usage, restore_session, workspace_root, model_switch=None,
                          skills=None, skills_reload=None):
    """处理 IDLE 态斜杠命令；非命令返回 None，待运行 prompt 返回字符串。"""
    if not user_input.startswith("/"):
        return None

    if user_input == "/help":
        print(HELP_TEXT)
    elif user_input == "/tools":
        for name in loop.registry.names():
            tool = loop.registry.get(name)
            print(f"  {name}: {tool.description}")
    elif user_input == "/session":
        print(f"  当前会话文件: {session_path or '（未启用，--no-session）'}")
    elif user_input == "/sessions":
        if sessions_dir is None:
            print("  （会话目录未知）")
        else:
            print(format_sessions(list_sessions(sessions_dir)))
    elif user_input.startswith("/resume"):
        parts = user_input.split(maxsplit=1)
        target = parts[1].strip() if len(parts) > 1 else "latest"
        path = _resolve_resume_target(target, sessions_dir)
        if path is None:
            print(f"  （找不到会话: {target}；用 /sessions 查看列表）")
        else:
            restore_session(loop, path)
            renderer.clear_tool_outputs()
    elif user_input == "/mcp" or user_input.startswith("/mcp "):
        if mcp_supervisor is None:
            print("  （MCP 未启用）")
        else:
            parts = user_input.split(maxsplit=1)
            sub = parts[1].strip() if len(parts) > 1 else ""
            if sub == "reload":
                restarted = await mcp_reload() if mcp_reload else []
                note = f"，重启: {', '.join(restarted)}" if restarted else ""
                print(f"  [mcp] 配置已同步{note}")
            for item in mcp_supervisor.status():
                state = "在线" if item["alive"] else "已断开"
                print(f"  {item['name']}（{item['server']}，{item['transport']}）"
                      f"{state}，{item['tools']} 个工具")
            if not mcp_supervisor.status():
                print("  （没有配置 MCP 服务器；在 krelcode.toml 的 [mcp_servers.*] 里声明）")
    elif user_input == "/agents":
        if subagents is None:
            print("  （子代理未启用）")
        else:
            lines = subagents.status_lines()
            print("\n".join(f"  {l}" for l in lines) if lines else "  （暂无子代理）")
    elif user_input == "/skills" or user_input.startswith("/skills "):
        if skills is None or not getattr(skills, "enabled", False):
            print("  （Skills 未启用：移除 --no-skills 或在配置中设置 skills.enabled=true）")
        else:
            parts = user_input.split(maxsplit=2)
            sub = parts[1].strip() if len(parts) > 1 else ""
            if sub == "reload":
                report = skills_reload() if skills_reload else skills.reload()
                print(f"  [skills] 已重载 {report.count} 个 Skill")
                for issue in report.issues:
                    print(f"  [skills] 跳过无效 Skill: {issue.path}: {issue.message}")
            elif sub == "show":
                if len(parts) < 3 or not parts[2].strip():
                    print("  用法: /skills show <name>")
                else:
                    _print_skill_details(skills, parts[2].strip())
            else:
                query = sub
                items = skills.list(query=query)
                if not items:
                    print("  （没有匹配的 Skill）" if query else "  （未发现 Skill）")
                for skill in items:
                    flags = []
                    if not skill.user_invocable:
                        flags.append("仅模型")
                    if skill.disable_model_invocation:
                        flags.append("仅用户")
                    flag_text = f" [{', '.join(flags)}]" if flags else ""
                    hint = f" · {skill.argument_hint}" if skill.argument_hint else ""
                    print(f"  {skill.name}: {skill.description}{hint}{flag_text}")
                if items:
                    print("  使用 /skills show <name> 查看详情，/skill <name> [参数] 调用")
                for issue in skills.issues:
                    print(f"  [skills] 跳过无效 Skill: {issue.path}: {issue.message}")
    elif user_input.startswith("/skill "):
        if skills is None or not getattr(skills, "enabled", False):
            print("  （Skills 未启用：移除 --no-skills 或在配置中设置 skills.enabled=true）")
        else:
            parts = user_input.split(maxsplit=2)
            if len(parts) < 2 or not parts[1].strip():
                print("  用法: /skill <name> [参数]")
            else:
                name = parts[1].strip()
                arguments = parts[2] if len(parts) > 2 else ""
                try:
                    prompt = skills.user_prompt(name, arguments)
                except Exception as e:
                    print(f"  [skill] {e}")
                else:
                    print(f"  [skill] 已加载并执行: {name}")
                    return prompt
    elif user_input == "/undo" or user_input.startswith("/undo "):
        if checkpoints is None:
            print("  （检查点未启用：--no-checkpoint）")
        else:
            parts = user_input.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            if arg == "list":
                lines = checkpoints.list_groups()
                print("\n".join(f"  {l}" for l in lines) if lines else "  （无可回滚的检查点）")
            else:
                result = checkpoints.undo_last()
                if result is None:
                    print("  （无可回滚的检查点）")
                else:
                    parts_desc = []
                    if result.restored:
                        parts_desc.append(f"还原 {len(result.restored)} 个: {', '.join(result.restored)}")
                    if result.deleted:
                        parts_desc.append(f"删除新建 {len(result.deleted)} 个: {', '.join(result.deleted)}")
                    label = f"（{result.label}）" if result.label else ""
                    print(f"  [undo] 已回滚上一轮{label}: {'；'.join(parts_desc) if parts_desc else '无文件变化'}")
    elif user_input == "/model" or user_input.startswith("/model "):
        if model_switch is None:
            print("  （模型切换不可用）")
        else:
            parts = user_input.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else None
            print(f"  [model] {model_switch(target)}")
    elif user_input == "/allow" or user_input.startswith("/allow "):
        rules = getattr(loop.policy, "rules", None)
        if rules is None:
            print("  （安全策略未启用（--no-sandbox），无审批白名单）")
        else:
            parts = user_input.split()
            if len(parts) == 1:
                listed = rules.as_list()
                print("  已允许的程序: " + (", ".join(listed) if listed else "（空）"))
                print(f"  规则文件: {rules.path or '（仅内存）'}")
            elif len(parts) == 3 and parts[1] == "add":
                added = rules.add_program(parts[2])
                print(f"  [allow] {'已添加' if added else '已存在，无需重复添加'}: {parts[2]}")
            elif len(parts) == 3 and parts[1] == "remove":
                removed = rules.remove_program(parts[2])
                print(f"  [allow] {'已移除' if removed else '规则不存在'}: {parts[2]}")
            else:
                print("  用法: /allow | /allow add <程序名> | /allow remove <程序名>")
    elif user_input == "/cost":
        print(f"  [cost] {usage.summary()}" if usage else "  （用量统计不可用）")
    elif user_input == "/context" or user_input.startswith("/context "):
        from ..agent.tokens import estimate_history_tokens
        parts = user_input.split(maxsplit=1)
        sub = parts[1].strip() if len(parts) > 1 else ""
        if sub == "refresh":
            from ..agent.context import build_workspace_context
            from ..agent.prompts import build_system_prompt
            ctx_text = await build_workspace_context(workspace_root)
            if loop.history and loop.history[0].role == "system":
                base_prompt = build_system_prompt(ctx_text)
                loop.skill_base_prompt = base_prompt
                loop.history[0].content = (
                    skills.apply_to_prompt(base_prompt) if skills else base_prompt)
                print("  [context] 工作区上下文已刷新（目录树/git/规则文件）")
            else:
                print("  [context] 历史中无 system 消息，未刷新")
        else:
            total = estimate_history_tokens(loop.history)
            budget = (loop.context_manager.max_tokens
                      if loop.context_manager else "未启用压缩")
            tool_msgs = sum(1 for m in loop.history if m.role == "tool")
            print(f"  [context] 历史 {len(loop.history)} 条消息（含 {tool_msgs} 条工具结果）"
                  f" · 估算 {total} tokens · 预算 {budget}")
    elif user_input == "/compact":
        if loop.context_manager is None:
            print("  （上下文压缩未启用：--no-compress）")
        else:
            from ..agent.tokens import estimate_history_tokens
            before = estimate_history_tokens(loop.history)
            done = await loop.context_manager.maybe_compress(loop.history, force=True)
            after = estimate_history_tokens(loop.history)
            if done:
                print(f"  [compact] 已压缩: {before} → {after} tokens")
            else:
                print(f"  [compact] 历史太短，无需压缩（当前约 {before} tokens）")
    elif user_input == "/export" or user_input.startswith("/export "):
        parts = user_input.split(maxsplit=1)
        try:
            if len(parts) > 1:
                out = export_session(loop.history, Path(parts[1].strip()))
            else:
                out = export_session(
                    loop.history,
                    export_dir=workspace_root / ".krelcode" / "exports")
            print(f"  [export] 已导出: {out}")
        except OSError as e:
            print(f"  [export] 导出失败: {e}")
    elif user_input == "/thinking":
        if not renderer.expand_thinking():
            print("  （暂无思考内容）")
    elif user_input == "/tool-output" or user_input.startswith("/tool-output "):
        parts = user_input.split(maxsplit=1)
        if len(parts) == 1:
            if not renderer.list_tool_outputs():
                print("  （暂无可展开的工具输出）")
        elif not renderer.expand_tool_output(parts[1].strip()):
            print("  （未找到工具输出；输入 /tool-output 查看编号）")
    elif user_input == "/history":
        for m in loop.history:
            content = m.content or ""
            extra = f" tool_calls={[tc.name for tc in m.tool_calls]}" if m.tool_calls else ""
            print(f"  [{m.role}]{extra} {content}")
    elif user_input == "/reset":
        loop.reset()
        renderer.clear_tool_outputs()
        print("（历史已清空）")
    else:
        print(f"  （未知命令: {user_input.split()[0]}，/help 查看可用命令）")
    return False


def _print_skill_details(skills, name: str) -> None:
    """在 REPL 中展示 Skill 元数据、资源和完整说明。"""
    try:
        skill = skills.get(name)
        if skill is None:
            raise ValueError(f"未知 Skill: {name}")
        print(f"  [skill] {skill.name}")
        print(f"  描述: {skill.description}")
        print(f"  来源: {skill.source}")
        print(f"  位置: {skill.location}")
        if skill.argument_hint:
            print(f"  参数: {skill.argument_hint}")
        if skill.allowed_tools:
            print(f"  建议工具: {', '.join(skill.allowed_tools)}")
        if skill.version:
            print(f"  版本: {skill.version}")
        if skill.license:
            print(f"  许可证: {skill.license}")
        resources = skills.list_resources(name)
        if resources:
            print("  资源:")
            for resource in resources[:50]:
                print(f"    {resource.kind:4s} {resource.size:>8d}  {resource.path}")
            if len(resources) > 50:
                print(f"    … 另有 {len(resources) - 50} 个资源")
        print("\n" + skills.load(name, for_model=False))
    except Exception as e:
        print(f"  [skill] {e}")


def _resolve_resume_target(target: str, sessions_dir: Optional[Path]) -> Optional[Path]:
    """latest / 序号 / 文件路径 → 会话文件。"""
    if sessions_dir is None:
        return Path(target) if Path(target).is_file() else None
    if target == "latest":
        infos = list_sessions(sessions_dir)
        return infos[0].path if infos else None
    if target.isdigit():
        infos = list_sessions(sessions_dir)
        idx = int(target)
        if 1 <= idx <= len(infos):
            return infos[idx - 1].path
        return None
    path = Path(target)
    return path if path.is_file() else None
