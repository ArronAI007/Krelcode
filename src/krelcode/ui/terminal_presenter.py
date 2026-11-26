"""Terminal presentation adapter for the conversation host.

The presenter owns visual state, prompt rendering, panels, status lines, and
review displays.  Conversation flow and session ownership remain in
``ConversationHost`` so terminal rendering can be replaced independently.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import shlex
import shutil
import sys
import threading
import time
from typing import Awaitable, Callable, Optional, cast

from ..agent.turn.loop import AgentLoop
from ..core.tokens import estimate_history_tokens
from ..core.events import (
    AgentEvent,
    MemoryCandidateObserved,
    MemoryWritten,
    RunStarted,
)
from ..core.redact import SecretRedactor
from ..session.controller import SessionController
from ..session.input import InputAction, InputProcessor
from ..session.memory import MemoryStore
from ..session.memory_candidates import AutoMemoryCoordinator
from ..session.memory_models import MemoryStoreSnapshot
from ..session.repl_session import ReplSessionRunner
from ..session.state import SessionViewState
from .approval import ApprovalRequest
from .alt_screen import AltScreenPrompt, AltScreenSurface
from .diff_review import (
    DiffPager,
    DiffReview,
    build_tool_call_review,
    collect_git_review,
)
from .editor import InputHistory
from .render import CYAN, RESET, StreamRenderer, auto_color
from .repl_primitives import (
    IDLE_PROMPT,
    RUNNING_PROMPT,
    PanelController,
    PromptWriter,
    TerminalLineEditor,
    _stdout_write,
)
from .repl_surface import match_slash_commands, print_banner
from .status_line import StatusLineRenderer, read_git_branch
from .tui.mouse import MouseEvent
from .tui.panels import build_panels


class TerminalPresenter:
    """Render one interactive conversation into a terminal."""

    def __init__(
        self,
        loop: AgentLoop,
        *,
        tool_names: Optional[list[str]] = None,
        session_path: Optional[str] = None,
        input_fn: Optional[Callable[[str], str | Awaitable[str]]] = None,
        redactor: Optional[SecretRedactor] = None,
        history_path: Optional[pathlib.Path] = None,
        persist_history: Optional[bool] = None,
        tui_mouse: bool = False,
        ui_mode: str = "native",
    ) -> None:
        self.loop = loop
        self.tool_names = tool_names
        self.session_path = session_path
        self.input_fn = input_fn
        self.redactor = redactor
        self.tui_mouse = tui_mouse
        self.workspace_root = loop.tool_context.workspace_root
        self.use_color = auto_color()
        self.ui_mode = str(ui_mode or "native").lower()
        if self.ui_mode not in {"native", "fullscreen"}:
            raise ValueError(f"未知 UI 模式: {ui_mode}")
        if self.ui_mode == "fullscreen" and not sys.stdout.isatty():
            self.ui_mode = "native"
        self.alt_screen: Optional[AltScreenSurface] = (
            AltScreenSurface(color=self.use_color)
            if self.ui_mode == "fullscreen" else None
        )
        self.prompt: PromptWriter = (
            AltScreenPrompt(self.alt_screen, color=self.use_color)
            if self.alt_screen is not None
            else PromptWriter(color=self.use_color)
        )
        self._output_row_sequence = 0
        self._tool_click_anchors: dict[int, int] = {}
        self._thinking_click_anchor: Optional[int] = None
        self.renderer = StreamRenderer(
            write=(
                self.alt_screen.write_transcript
                if self.alt_screen is not None
                else self._prompt_write
            ),
            redactor=redactor,
            on_tool_line=(
                self.alt_screen.mark_tool
                if self.alt_screen is not None
                else self._register_tool_click_anchor
            ),
            on_thinking_line=(
                self.alt_screen.mark_thinking
                if self.alt_screen is not None
                else self._register_thinking_click_anchor
            ),
            tool_lines_clickable=(
                self.alt_screen is not None or self.tui_mouse),
        )
        self.input_ready = threading.Event()

        explicit_history_path = history_path is not None
        persistent_history = (
            (input_fn is None or explicit_history_path)
            if persist_history is None else persist_history
        )
        if history_path is None:
            configured_history = os.environ.get("KRELCODE_HISTORY_FILE")
            history_path = (
                pathlib.Path(configured_history).expanduser()
                if configured_history
                else pathlib.Path.home() / ".krelcode" / "history.jsonl"
            )
        self.input_history = InputHistory(
            path=history_path if persistent_history else None,
            redact=redactor.redact if redactor is not None else None,
        )

        self.controller: Optional[SessionController] = None
        self.state: Optional[SessionViewState] = None
        self.runner: Optional[ReplSessionRunner] = None
        self.processor: Optional[InputProcessor] = None
        self.editor: Optional[TerminalLineEditor] = None
        self.panel_controller = PanelController()
        self.quick_status = ""
        self.review_pager: Optional[DiffPager] = None
        self.approval_review: Optional[DiffReview] = None
        self.git_review: Optional[DiffReview] = None
        self.memory_cache: Optional[MemoryStoreSnapshot] = None
        self.memory_pending_candidates = 0
        self.memory_mode = ""
        self.status_cache = ""
        self.last_status_draw = 0.0
        configured_status_renderer = (
            loop.services.extensions.status_line_renderer)
        self.status_renderer: StatusLineRenderer = (
            configured_status_renderer
            if isinstance(configured_status_renderer, StatusLineRenderer)
            else StatusLineRenderer())
        self.status_branch = read_git_branch(self.workspace_root)

    def _prompt_write(self, text: str) -> None:
        # Preserve the current draft while an asynchronous event writes output.
        self.prompt.suspend()
        _stdout_write(text)
        self._output_row_sequence += text.count("\n")

    @property
    def prompt_text(self) -> str:
        return self.styled_prompt(self.state.is_running if self.state else False)

    def styled_prompt(self, running: bool) -> str:
        text = RUNNING_PROMPT if running else IDLE_PROMPT
        return f"{CYAN}{text}{RESET}" if self.use_color else text

    def bind(
        self,
        controller: SessionController,
        state: SessionViewState,
        runner: ReplSessionRunner,
        processor: InputProcessor,
        memory_store: MemoryStore,
    ) -> None:
        self.controller = controller
        self.state = state
        self.runner = runner
        self.processor = processor
        self.refresh_memory_cache()
        self.editor = TerminalLineEditor(
            self.prompt,
            self.suggest,
            color=self.use_color,
            history=self.input_history,
            panels=self.panel_controller,
            on_diff_pager_change=lambda: self.refresh_panels(
                queue_size=len(runner.queue)),
            mouse_reporting=(
                self.alt_screen is not None or self.tui_mouse),
            on_mouse_click=self.handle_transcript_mouse,
            on_viewport_key=(
                self.alt_screen.handle_viewport_key
                if self.alt_screen is not None else None
            ),
        )

    def _register_tool_click_anchor(self, index: int) -> None:
        self._tool_click_anchors[index] = self._output_row_sequence

    def _register_thinking_click_anchor(self) -> None:
        self._thinking_click_anchor = self._output_row_sequence

    def handle_transcript_mouse(self, event: MouseEvent) -> bool:
        alt_screen = cast(
            Optional[AltScreenSurface],
            getattr(self, "alt_screen", None),
        )
        if alt_screen is not None:
            return alt_screen.handle_mouse(event)
        if not event.is_left_click:
            return False
        origin = self.panel_controller.cursor_origin_row
        if origin is None:
            return False
        clicked_row = event.y - 1
        if clicked_row >= origin:
            return False
        distance = origin - clicked_row
        thinking_anchor = getattr(self, "_thinking_click_anchor", None)
        if thinking_anchor is not None:
            expected = self._output_row_sequence - thinking_anchor + 1
            if distance == expected:
                self._thinking_click_anchor = None
                handled = self.renderer.expand_thinking()
                if handled:
                    self.prompt.resume_or_show(self.prompt_text)
                return handled
        for index, anchor in reversed(tuple(self._tool_click_anchors.items())):
            expected = self._output_row_sequence - anchor + 1
            if distance == expected:
                self._tool_click_anchors.clear()
                handled = self.renderer.expand_tool_output(str(index))
                if handled:
                    self.prompt.resume_or_show(self.prompt_text)
                return handled
        return False

    def start_alt_screen(self) -> None:
        if self.alt_screen is None:
            return
        self.alt_screen.set_click_handler(self._handle_alt_click)
        self.alt_screen.start()

    def stop_alt_screen(self) -> None:
        if self.alt_screen is not None:
            self.alt_screen.stop()

    def _handle_alt_click(self, kind: str, value: str) -> bool:
        if kind == "thinking":
            return self.renderer.expand_thinking()
        if kind == "tool":
            return self.renderer.expand_tool_output(value)
        return False

    def show_banner(self) -> None:
        print_banner(
            self.loop, self.tool_names, session_path=self.session_path)

    def suggest(self, text: str) -> list[tuple[str, str]]:
        if self.state is None or self.processor is None:
            return []
        return match_slash_commands(
            text,
            running=self.state.is_running,
            registry=self.processor.command_registry,
        )

    def refresh_memory_cache(self) -> None:
        memory_store = self.loop.services.session.memory
        if memory_store is not None:
            concrete_memory = cast(MemoryStore, memory_store)
            self.memory_cache = concrete_memory.dashboard_snapshot(limit=8)
        auto_memory = cast(
            Optional[AutoMemoryCoordinator],
            self.loop.services.extensions.auto_memory,
        )
        if auto_memory is None:
            self.memory_pending_candidates = 0
            self.memory_mode = ""
        else:
            self.memory_pending_candidates = auto_memory.queue.pending_count()
            self.memory_mode = auto_memory.mode.value

    def refresh_panels(self, *, queue_size: int = 0) -> None:
        if (self.state is None or self.processor is None
                or self.controller is None or self.runner is None):
            return
        snapshot = self.loop.runtime_state.prompt.prompt_snapshot
        fragments = getattr(snapshot, "fragments", ())
        cache = getattr(snapshot, "cache", None)
        context_manager = self.loop.services.context.manager
        terminal = shutil.get_terminal_size((80, 24))
        columns = max(1, terminal.columns - 1)
        if self.review_pager is not None and self.review_pager.active:
            overlay = self.review_pager.render(
                columns, max(8, terminal.lines - 8), color=self.use_color)
            self.panel_controller.set_dock_visible(True)
            self.prompt.set_panels(overlay)
            return
        memory_cache = self.memory_cache
        panels = build_panels(
            self.state,
            permission_mode=self.controller.permission_manager.mode.value,
            queue_size=queue_size,
            context_tokens=estimate_history_tokens(self.loop.history),
            context_budget=getattr(context_manager, "max_tokens", None),
            fragment_count=len(fragments),
            stable_fragments=len(getattr(cache, "stable_fragments", ())),
            memory_entries=memory_cache.entries if memory_cache else (),
            memory_total=(
                memory_cache.total_entries if memory_cache is not None else 0),
            memory_tokens=memory_cache.tokens if memory_cache else 0,
            memory_pending=self.memory_pending_candidates,
            memory_mode=self.memory_mode,
            quick_status=self.quick_status,
            diff_review=self.approval_review or self.git_review,
        )
        self.panel_controller.set_panels(panels)
        layout = self.panel_controller.render(columns)
        dock_visible = (
            self.state.is_running
            or bool(self.state.plan_items)
            or self.panel_controller.focused
            or bool(self.quick_status)
            or (self.approval_review is not None)
            or (self.git_review is not None)
        )
        self.panel_controller.set_dock_visible(dock_visible)
        self.prompt.set_panels(layout.lines if dock_visible else ())

    def render_status(self, view: SessionViewState) -> str:
        """Build a status line without touching disk on every redraw."""
        if self.controller is None:
            return ""
        progress = view.status_text()
        if self.tui_mouse:
            progress = (
                f"{progress} · Shift+拖拽复制"
                if progress else "Shift+拖拽复制"
            )
        if self.review_pager is not None and self.review_pager.active:
            mode = "待审批" if self.review_pager.can_decide else "只读"
            progress = (
                f"diff {mode} · {self.review_pager.review.stats} · "
                "j/k 滚动 · y/n 决定 · q 返回")
        elif not progress and self.controller.plan_manager.pending:
            progress = "计划待确认 · /plan approve 执行"
        tokens = estimate_history_tokens(self.loop.history)
        manager = self.loop.services.context.manager
        budget = getattr(manager, "max_tokens", None)
        context = f"ctx {tokens}/{budget}" if budget else f"ctx {tokens}"
        provider = str(getattr(self.loop.services.core.llm, "name", "") or "")
        model = str(getattr(self.loop.services.core.llm, "model", "") or "")
        session = (
            pathlib.Path(self.session_path).stem if self.session_path else "")
        return self.status_renderer.render(
            provider=provider,
            model=model,
            permission=(
                f"权限 {self.controller.permission_manager.mode.value}"),
            session=session,
            cwd=str(self.workspace_root),
            status=progress,
            tokens=str(tokens),
            context=context,
            branch=self.status_branch,
        )

    def refresh_status(
        self,
        current: Optional[SessionViewState] = None,
        *,
        force: bool = False,
    ) -> None:
        if self.state is None or self.controller is None or self.runner is None:
            return
        self.refresh_panels(queue_size=len(self.runner.queue))
        now = time.monotonic()
        if not force and now - self.last_status_draw < 0.15:
            return
        view = current or self.state
        status = self.render_status(view)
        if status != self.status_cache:
            self.status_cache = status
            self.prompt.set_status(status)
        if view.is_running:
            self.prompt.retarget_prompt(
                self.styled_prompt(False), self.styled_prompt(True))
            # Stream output owns the cursor while tokens are arriving.  The
            # next status update after RunFinished restores the dock and input.
            if not self.renderer.output_active:
                self.prompt.resume_or_show(self.prompt_text)
        else:
            self.prompt.retarget_prompt(
                self.styled_prompt(True), self.styled_prompt(False))
        self.last_status_draw = now

    def restore_prompt(self) -> None:
        if self.state is None or self.runner is None:
            return
        self.refresh_panels(queue_size=len(self.runner.queue))
        self.status_cache = self.render_status(self.state)
        self.prompt.set_status(self.status_cache)
        self.prompt.resume_or_show(self.prompt_text)
        self.last_status_draw = time.monotonic()
        self.input_ready.set()

    def notice(self, message: str) -> None:
        self.prompt.suspend()
        _stdout_write(f"  {message}\n")
        if self.state is not None and self.state.is_running:
            self.refresh_status(self.state, force=True)

    def redact(self, value: object) -> str:
        text = str(value)
        return self.redactor.redact(text) if self.redactor is not None else text

    def show_attachment_notes(self, action: InputAction) -> None:
        for note_text in action.notes:
            print(f"  [@] {note_text}")

    def mark_submitted(self) -> None:
        if self.state is not None:
            self.prompt.submitted(raw=self.state.raw_input)

    def hide(self) -> None:
        self.prompt.hide()

    def finish(self) -> None:
        self.prompt.finish()

    def on_session_event(self, event: AgentEvent) -> None:
        self.renderer(event)
        if isinstance(event, RunStarted):
            self._thinking_click_anchor = None
        if isinstance(event, (MemoryCandidateObserved, MemoryWritten)):
            self.refresh_memory_cache()
            refresh = getattr(self, "refresh_status", None)
            if refresh is not None:
                refresh(self.state)

    def end_stream(self) -> None:
        self.renderer.end_stream()

    def clear_review(self) -> None:
        if self.review_pager is not None:
            self.review_pager.active = False
        self.review_pager = None
        self.approval_review = None
        if self.editor is not None:
            self.editor.set_diff_pager(None)

    def activate_review(self, review: DiffReview, *, can_decide: bool) -> DiffPager:
        pager = DiffPager(review, can_decide=can_decide, active=True)
        self.review_pager = pager
        if self.editor is not None:
            self.editor.set_diff_pager(pager)
        if self.runner is None:
            return pager
        self.refresh_panels(queue_size=len(self.runner.queue))
        return pager

    def show_approval(self, request: ApprovalRequest) -> None:
        tool_call = request.tool_call
        command = tool_call.arguments.get("command")
        detail = command if command is not None else tool_call.arguments
        self.prompt.suspend()
        _stdout_write(f"  [审批] {tool_call.name}: {self.redact(detail)}\n")
        _stdout_write(f"  原因: {self.redact(request.reason)}\n")

        review = build_tool_call_review(
            tool_call, self.workspace_root, redactor=self.redactor)
        self.approval_review = review
        if review.has_changes:
            _stdout_write(f"  差异审阅: {self.redact(review.stats)}\n")
            for line in review.summary_lines(limit=4):
                _stdout_write(f"    {self.redact(line)}\n")
            for line in review.preview_lines(limit=7):
                _stdout_write(f"    {self.redact(line)}\n")
            self.activate_review(review, can_decide=True)
            _stdout_write(
                "  j/k 滚动 · PgUp/PgDn 翻页 · [/] 切换文件 · "
                "y 允许 · n 拒绝 · q 返回\n")
            return

        if review.notice:
            _stdout_write(f"  {self.redact(review.notice)}\n")
        _stdout_write(
            "  允许执行? [y]是 / [n]否 / [a]始终允许此命令（直接 Enter=否）\n")

    async def handle_diff_command(self, arguments: str) -> bool:
        """Open or navigate the current approval/Git diff review."""
        if self.runner is None:
            return True
        try:
            parts = shlex.split(arguments) if arguments.strip() else []
        except ValueError as exc:
            print(f"  [diff] 参数解析失败: {exc}")
            return True
        operation = parts[0].lower() if parts else ""

        if self.review_pager is None or not self.review_pager.active:
            if operation == "close":
                self.clear_review()
                return True
            if self.runner.pending is not None and self.approval_review is not None:
                self.activate_review(self.approval_review, can_decide=True)
            else:
                staged = operation == "staged"
                if operation not in {"", "staged", "refresh"}:
                    print("  用法: /diff [staged|refresh|next|prev|file <n>|close]")
                    return True
                review = await asyncio.to_thread(
                    collect_git_review,
                    self.workspace_root,
                    staged=staged,
                    redactor=self.redactor,
                )
                self.git_review = review
                if not review.files:
                    print(f"  [diff] {review.notice or '没有可审阅差异'}")
                    return True
                self.activate_review(review, can_decide=False)

        pager = self.review_pager
        if pager is None:
            return True
        if operation in {"close", "q"}:
            self.clear_review()
            self.refresh_panels(queue_size=len(self.runner.queue))
            return True
        if operation == "next":
            pager.switch_file(1)
        elif operation == "prev":
            pager.switch_file(-1)
        elif operation == "file":
            if len(parts) != 2:
                print("  用法: /diff file <n>")
                return True
            try:
                index = max(1, int(parts[1])) - 1
            except ValueError:
                print("  用法: /diff file <n>")
                return True
            pager.select_file(index)
        elif operation == "top":
            pager.go_start()
        elif operation == "end":
            pager.go_end()
        elif operation == "numbers":
            pager.toggle_line_numbers()
        elif operation in {"staged", "refresh"} and self.runner.pending is not None:
            pass
        elif operation in {"", "staged", "refresh"}:
            if operation != "":
                staged = operation == "staged"
                review = await asyncio.to_thread(
                    collect_git_review,
                    self.workspace_root,
                    staged=staged,
                    redactor=self.redactor,
                )
                self.git_review = review
                if not review.files:
                    print(f"  [diff] {review.notice or '没有可审阅差异'}")
                    return True
                self.activate_review(review, can_decide=False)
                pager = self.review_pager
        else:
            print("  用法: /diff [staged|refresh|next|prev|file <n>|close]")
            return True

        if pager is not None:
            self.refresh_panels(queue_size=len(self.runner.queue))
            print(f"  [diff] {pager.review.stats}")
            for line in pager.review.summary_lines(limit=4):
                print(f"    {line}")
        return True


__all__ = ["TerminalPresenter"]
