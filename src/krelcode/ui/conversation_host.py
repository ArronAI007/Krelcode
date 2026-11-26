"""Interactive conversation host: foreground state and action routing.

The host owns the terminal foreground loop, input conversion, session runner,
approval/user-input precedence, queued prompts, and quick-shell lifecycle.
Rendering is delegated to :class:`TerminalPresenter`; query ownership remains
in :class:`ReplSessionRunner`.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import replace
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol, Sequence, cast

from ..agent.turn.runtime import AgentRuntime
from ..agent.turn.loop import AgentLoop
from ..core.events import AgentEvent, ShellJobFinished
from ..core.redact import SecretRedactor
from ..session.commands import CommandResult
from ..session.commands.ports import (
    MCPReloadPort,
    MCPSupervisorPort,
    ModelSwitchPort,
    PluginManagerPort,
    PluginReloadPort,
    SkillManagerPort,
    SkillReloadPort,
    SubagentManagerCommandPort,
    UsageTrackerPort,
)
from ..session.controller import SessionController
from ..session.input import (
    InputAction,
    InputDelivery,
    InputEnvelope,
    InputKind,
    InputProcessor,
)
from ..session.interactions import UserInputBroker
from ..session.memory import (
    MemoryStore,
    memory_lines,
    sync_memory_fragment,
)
from ..session.memory_models import SCOPE_LABELS, parse_memory_note
from ..session.repl_session import ReplSessionRunner, SessionPrompt
from ..session.runtime_ports import CheckpointPort
from ..session.state import SessionViewState
from .approval import ApprovalBroker
from .command_dispatch import _handle_command
from .repl_primitives import RUNNING_HINT, ReplControl, TerminalSubmission
from .repl_surface import (
    _format_job_notification,
    _queue_label,
    handle_plan_command,
    manage_prompt_queue,
)
from .terminal_presenter import TerminalPresenter

InputCallback = Callable[[str], str | Awaitable[str]]
RawInput = str | TerminalSubmission | ReplControl | None
QueuedAction = InputAction | ReplControl | None


class ShellShortcutResult(Protocol):
    @property
    def content(self) -> str: ...

    @property
    def is_error(self) -> bool: ...


class ConversationHost:
    """Coordinate one interactive session without owning process bootstrap."""

    def __init__(
        self,
        loop: AgentLoop,
        tool_names: Optional[list[str]] = None,
        session_path: Optional[str] = None,
        sessions_dir: Optional[Path] = None,
        mcp_supervisor: Optional[MCPSupervisorPort] = None,
        mcp_reload: Optional[MCPReloadPort] = None,
        subagents: Optional[SubagentManagerCommandPort] = None,
        checkpoints: Optional[CheckpointPort] = None,
        usage: Optional[UsageTrackerPort] = None,
        model_switch: Optional[ModelSwitchPort] = None,
        skills: Optional[SkillManagerPort] = None,
        skills_reload: Optional[SkillReloadPort] = None,
        plugins: Optional[PluginManagerPort] = None,
        plugins_reload: Optional[PluginReloadPort] = None,
        input_fn: Optional[InputCallback] = None,
        redactor: Optional[SecretRedactor] = None,
        runtime: Optional[AgentRuntime] = None,
        input_processor: Optional[InputProcessor] = None,
        approval_broker: Optional[ApprovalBroker] = None,
        input_broker: Optional[UserInputBroker] = None,
        history_path: Optional[Path] = None,
        persist_history: Optional[bool] = None,
        tui_mouse: bool = False,
        ui_mode: str = "native",
    ) -> None:
        if (input_broker is not None
                and loop.services.core.user_input_port is None):
            loop.user_input_port = input_broker
        self.runtime = runtime or AgentRuntime(
            loop,
            subagents=subagents,
            mcp_supervisor=mcp_supervisor,
            mcp_reload=mcp_reload,
        )
        self.loop = self.runtime.loop
        self.tool_names = tool_names
        self.session_path = session_path
        self.sessions_dir = sessions_dir
        self.mcp_supervisor = mcp_supervisor
        self.mcp_reload = mcp_reload
        self.subagents = subagents
        self.checkpoints = checkpoints
        self.usage = usage
        self.model_switch = model_switch
        self.skills = skills
        self.skills_reload = skills_reload
        self.plugins = plugins
        self.plugins_reload = plugins_reload
        self.input_fn = input_fn
        self.redactor = redactor
        self.tui_mouse = tui_mouse
        self.approval_broker = approval_broker
        self.input_broker = input_broker
        self.processor = input_processor or InputProcessor(
            self.loop.tool_context.workspace_root)
        self.controller = SessionController(
            self.runtime,
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
            plugins=plugins,
            plugins_reload=plugins_reload,
            workspace_root=self.loop.tool_context.workspace_root,
            command_registry=self.processor.command_registry,
        )
        configured_memory = self.loop.services.session.memory
        if configured_memory is None:
            self.memory_store = MemoryStore(
                self.loop.tool_context.workspace_root, redactor=redactor)
            self.loop.memory_store = self.memory_store
        else:
            self.memory_store = cast(MemoryStore, configured_memory)
            self.memory_store.redactor = redactor
        sync_memory_fragment(self.loop, self.memory_store)
        self.state = SessionViewState()
        self.presenter = TerminalPresenter(
            self.loop,
            tool_names=tool_names,
            session_path=session_path,
            input_fn=input_fn,
            redactor=redactor,
            history_path=history_path,
            persist_history=persist_history,
            tui_mouse=tui_mouse,
            ui_mode=ui_mode,
        )
        self.runner = ReplSessionRunner(
            self.controller,
            self.state,
            on_event=self.presenter.on_session_event,
            on_status=self.presenter.refresh_status,
            on_approval=self.presenter.show_approval,
            on_notice=self.presenter.notice,
            on_stream_end=self.presenter.end_stream,
            approval_broker=approval_broker,
            input_broker=input_broker,
            redactor=redactor,
        )
        self.presenter.bind(
            self.controller,
            self.state,
            self.runner,
            self.processor,
            self.memory_store,
        )

    async def run(self) -> int:
        loop = self.loop
        state = self.state
        controller = self.controller
        runner = self.runner
        processor = self.processor
        presenter = self.presenter
        prompt = presenter.prompt
        renderer = presenter.renderer
        editor = presenter.editor
        input_history = presenter.input_history
        input_ready = presenter.input_ready
        workspace_root = loop.tool_context.workspace_root
        memory_store = self.memory_store
        refresh_status = presenter.refresh_status
        restore_prompt = presenter.restore_prompt
        notice = presenter.notice
        clear_review = presenter.clear_review
        session_path = self.session_path
        sessions_dir = self.sessions_dir
        mcp_supervisor = self.mcp_supervisor
        mcp_reload = self.mcp_reload
        subagents = self.subagents
        checkpoints = self.checkpoints
        usage = self.usage
        model_switch = self.model_switch
        skills = self.skills
        skills_reload = self.skills_reload
        approval_broker = self.approval_broker
        input_broker = self.input_broker
        presenter.start_alt_screen()
        presenter.show_banner()
        raw_inbox: asyncio.Queue[RawInput] = asyncio.Queue()
        action_queue: asyncio.Queue[QueuedAction] = asyncio.Queue()
        job_notifications: asyncio.Queue[ShellJobFinished] = asyncio.Queue()

        def on_job_finished(event: AgentEvent) -> None:
            if isinstance(event, ShellJobFinished):
                job_notifications.put_nowait(event)

        unsubscribe_job_notifications = loop.bus.subscribe(on_job_finished)
        quick_task: Optional[asyncio.Task[ShellShortcutResult]] = None
        quick_unsubscribe: Optional[Callable[[], None]] = None

        async def reader() -> None:
            assert self.input_fn is not None
            while True:
                try:
                    pending = self.input_fn("")
                    line = await pending if inspect.isawaitable(pending) else pending
                except KeyboardInterrupt:
                    await raw_inbox.put(ReplControl.INTERRUPT)
                    continue
                except EOFError:
                    await raw_inbox.put(None)
                    return
                except asyncio.CancelledError:
                    raise
                await raw_inbox.put(line)

        def start_stdin_reader() -> None:
            """真实终端使用 daemon 线程，避免退出时被阻塞的 input() 拖住。"""
            event_loop = asyncio.get_running_loop()

            def push(line: RawInput) -> None:
                try:
                    event_loop.call_soon_threadsafe(raw_inbox.put_nowait, line)
                except RuntimeError:
                    pass  # 事件循环已经结束，daemon 线程可自行退出

            def read_stdin() -> None:
                terminal_editor = editor
                assert terminal_editor is not None
                while True:
                    input_ready.wait()
                    try:
                        state.raw_input = terminal_editor.available
                        line = terminal_editor.readline(
                            lambda: self.presenter.prompt_text)
                    except KeyboardInterrupt:
                        line = ReplControl.INTERRUPT
                    except (EOFError, OSError):
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
                if isinstance(raw, ReplControl):
                    await action_queue.put(raw)
                    continue
                queue_next = False
                if isinstance(raw, TerminalSubmission):
                    queue_next = raw.queue_next
                    raw = raw.text
                try:
                    action = await processor.process(InputEnvelope(raw))
                except Exception as exc:
                    print(f"  （输入处理失败: {type(exc).__name__}: {exc}）")
                    action = InputAction(
                        kind=InputKind.EMPTY, raw_text=str(raw), source="terminal")
                if queue_next:
                    action = replace(action, delivery=InputDelivery.QUEUE)
                await action_queue.put(action)

        def to_session_prompt(
            action: InputAction,
            *,
            tool_allowlist: Optional[Sequence[str]] = None,
            reminder_ids: Optional[Sequence[str]] = None,
        ) -> SessionPrompt:
            return SessionPrompt.create(
                action.text,
                images=action.images,
                tool_allowlist=tool_allowlist,
                reminder_ids=reminder_ids or (),
                label=_queue_label(action),
            )

        async def cancel_quick_task(
            message: str = "⎿ Shell 快捷命令已中止",
            *,
            show_notice: bool = True,
        ) -> None:
            nonlocal quick_task, quick_unsubscribe
            if quick_task is None:
                return
            runner.reject_pending_approval()
            if not quick_task.done():
                quick_task.cancel()
            await asyncio.gather(quick_task, return_exceptions=True)
            quick_task = None
            self.presenter.quick_status = "shell · 已中止"
            if quick_unsubscribe is not None:
                quick_unsubscribe()
                quick_unsubscribe = None
            renderer.end_stream()
            if show_notice:
                notice(message)

        async def status_loop() -> None:
            """没有新事件时也持续刷新耗时，避免状态栏停住。"""
            while True:
                await asyncio.sleep(0.5)
                if state.is_running:
                    refresh_status(state, force=True)

        async def handle_command(action: InputAction) -> CommandResult | bool:
            if action.command == "/plan":
                prompt.hide()
                plan_result = handle_plan_command(
                    action.arguments,
                    controller.plan_manager,
                    state,
                    allow_actions=not state.is_running,
                )
                for line in plan_result.output:
                    print(line)
                return plan_result if plan_result.run_prompt is not None else False
            result = await _handle_command(
                action.text, loop, renderer,
                session_path=session_path, sessions_dir=sessions_dir,
                mcp_supervisor=mcp_supervisor, mcp_reload=mcp_reload,
                subagents=subagents, checkpoints=checkpoints, usage=usage,
                restore_session=None, workspace_root=workspace_root,
                model_switch=model_switch, skills=skills,
                skills_reload=skills_reload, controller=controller,
                command_registry=processor.command_registry,
                diff_handler=self.presenter.handle_diff_command,
            )
            if action.command == "/memory":
                self.presenter.refresh_memory_cache()
            return result if result is not None else False

        restore_prompt()
        reader_task = asyncio.create_task(reader()) if self.input_fn is not None else None
        if reader_task is None:
            start_stdin_reader()
        processor_task = asyncio.create_task(process_inputs())
        action_get = asyncio.create_task(action_queue.get())
        job_notice_get = asyncio.create_task(job_notifications.get())
        status_task = asyncio.create_task(status_loop())
        runner.arm_approval_watcher()
        runner.arm_input_watcher()
        try:
            while True:
                wait_set = {action_get, job_notice_get, *runner.wait_tasks()}
                if quick_task is not None:
                    wait_set.add(quick_task)
                done, _ = await asyncio.wait(
                    wait_set, return_when=asyncio.FIRST_COMPLETED)
                handled = await runner.process_completed(done)

                if quick_task is not None and quick_task in done:
                    try:
                        direct_result = quick_task.result()
                        lines = [
                            " ".join(line.split())
                            for line in direct_result.content.splitlines()
                            if line.strip()
                        ]
                        preview = next(
                            (line for line in lines if not line.startswith("exit_code:")),
                            lines[0] if lines else "无输出",
                        )
                        self.presenter.quick_status = (
                            f"shell · {'失败' if direct_result.is_error else '完成'}"
                            f" · {preview[:100]}")
                    except asyncio.CancelledError:
                        self.presenter.quick_status = "shell · 已中止"
                    except Exception as exc:
                        self.presenter.quick_status = f"shell · {type(exc).__name__}: {exc}"
                    quick_task = None
                    if quick_unsubscribe is not None:
                        quick_unsubscribe()
                        quick_unsubscribe = None
                    renderer.end_stream()
                    handled = True

                if job_notice_get in done:
                    job_event = job_notice_get.result()
                    job_notice_get = asyncio.create_task(job_notifications.get())
                    notice(_format_job_notification(job_event))
                    handled = True

                if action_get not in done:
                    if (handled or runner.pending is not None
                            or runner.has_pending_input):
                        restore_prompt()
                    continue
                raw_action = action_get.result()
                action_get = asyncio.create_task(action_queue.get())

                if raw_action in {
                    ReplControl.APPROVE_DIFF, ReplControl.REJECT_DIFF,
                }:
                    prompt.hide()
                    if runner.pending is None:
                        clear_review()
                        restore_prompt()
                        continue
                    clear_review()
                    runner.resolve_approval(
                        "y" if raw_action is ReplControl.APPROVE_DIFF else "n")
                    restore_prompt()
                    continue

                # Shift-Tab：在任意前台状态快速轮换权限模式，保留当前草稿。
                if raw_action is ReplControl.CYCLE_PERMISSION:
                    plan_status_before = controller.plan_manager.status.value
                    previous, current = controller.cycle_permission_mode()
                    suffix = ""
                    if (
                        plan_status_before != "cancelled"
                        and controller.plan_manager.status.value == "cancelled"
                    ):
                        suffix = "；待确认计划已取消"
                    if current in {"plan", "read-only"} and runner.pending is not None:
                        runner.reject_pending_approval()
                        clear_review()
                        suffix += "；待审批请求已拒绝"
                    notice(
                        f"权限模式: {previous} → {current}{suffix}")
                    restore_prompt()
                    continue

                # Esc/Ctrl-C：运行中中止；空闲时只清空当前输入。
                if isinstance(raw_action, ReplControl):
                    prompt.hide()
                    if runner.pending is not None:
                        clear_review()
                    if quick_task is not None:
                        await cancel_quick_task()
                        restore_prompt()
                    elif runner.is_running:
                        await runner.finish_current(interrupt=True)
                        restore_prompt()
                    else:
                        restore_prompt()
                    continue

                action = raw_action

                # EOF：停掉运行中任务并退出；不启动已排队消息。
                if action is None:
                    prompt.hide()
                    if runner.is_running:
                        await runner.finish_current(
                            interrupt=True,
                            run_queued=False,
                            show_stopped_note=False,
                        )
                    return 0

                prompt.submitted(raw=state.raw_input)

                # 队列属于当前终端前台状态，必须在审批和会话命令之前截获。
                if action.kind is InputKind.UI_COMMAND and action.command == "/queue":
                    prompt.hide()
                    manage_prompt_queue(action.arguments, runner.queue, write=print)
                    restore_prompt()
                    continue

                # ask_user 请求优先消费普通文本；控制命令和展示命令仍可使用。
                if runner.has_pending_input:
                    if action.kind is InputKind.UI_COMMAND:
                        prompt.hide()
                        await handle_command(action)
                        restore_prompt()
                        continue
                    if action.kind is InputKind.CONTROL:
                        runner.reject_pending_input()
                        if action.command == "/stop" and runner.is_running:
                            await runner.finish_current(interrupt=True)
                        elif action.command == "/exit":
                            if runner.is_running:
                                await runner.finish_current(
                                    interrupt=True,
                                    run_queued=False,
                                    show_stopped_note=False,
                                )
                            return 0
                        else:
                            print("  （当前正在等待用户输入；可输入回答、/stop 或 /exit）")
                            restore_prompt()
                        continue
                    prompt.hide()
                    runner.resolve_user_input(action.raw_text)
                    restore_prompt()
                    continue

                # 审批优先消费 y/n/a；/thinking、/tool-output 仍可在审批期间使用。
                if runner.pending is not None:
                    if action.kind is InputKind.UI_COMMAND:
                        prompt.hide()
                        await handle_command(action)
                        restore_prompt()
                        continue
                    if action.kind is InputKind.CONTROL:
                        runner.reject_pending_approval()
                        clear_review()
                        if action.command == "/stop" and quick_task is not None:
                            await cancel_quick_task()
                            restore_prompt()
                            continue
                        if action.command == "/stop" and runner.is_running:
                            await runner.finish_current(interrupt=True)
                        elif action.command == "/exit":
                            await cancel_quick_task(show_notice=False)
                            if runner.is_running:
                                await runner.finish_current(
                                    interrupt=True,
                                    run_queued=False,
                                    show_stopped_note=False,
                                )
                            return 0
                        else:
                            print("  （审批输入必须是 y / n / a）")
                            restore_prompt()
                        continue
                    prompt.hide()
                    clear_review()
                    runner.resolve_approval(action.raw_text)
                    restore_prompt()
                    continue

                input_history.add(action.raw_text)

                if action.kind is InputKind.EMPTY:
                    restore_prompt()
                    continue

                if action.kind is InputKind.MEMORY:
                    if not action.text:
                        memory_cache = self.presenter.memory_cache
                        entries = memory_cache.entries if memory_cache else ()
                        if not entries:
                            pending = self.presenter.memory_pending_candidates
                            if pending:
                                notice(
                                    f"⎿ 暂无已确认记忆 · {pending} 条 Auto Memory "
                                    "候选待审核；输入 /memory review")
                            else:
                                notice(
                                    "⎿ 暂无记忆 · 输入 #内容 添加项目记忆；"
                                    "#user: / #project: / #session: 指定作用域")
                            restore_prompt()
                            continue
                        prompt.hide()
                        for line in memory_lines(entries):
                            print(f"  {line}")
                        self.presenter.panel_controller.focus("memory")
                        restore_prompt()
                        continue
                    if runner.is_running or quick_task is not None:
                        notice("⎿ Memory 写入需等待当前任务结束")
                        restore_prompt()
                        continue
                    try:
                        scope, note_text = parse_memory_note(action.text)
                        entry = memory_store.add(note_text, scope=scope)
                    except (OSError, ValueError) as exc:
                        notice(f"⎿ 无法保存记忆: {exc}")
                        restore_prompt()
                        continue
                    sync_memory_fragment(loop, memory_store)
                    self.presenter.refresh_memory_cache()
                    self.presenter.panel_controller.focus("memory")
                    notice(
                        f"⎿ 已保存{SCOPE_LABELS[scope]}记忆 #{entry.id}")
                    restore_prompt()
                    continue

                if action.kind is InputKind.SHELL:
                    if not action.text:
                        notice("用法：!<shell 命令>，例如 !pytest -q")
                        restore_prompt()
                        continue
                    if runner.is_running or quick_task is not None:
                        notice("⎿ Shell 快捷命令需等待当前任务结束")
                        restore_prompt()
                        continue
                    prompt.hide()
                    self.presenter.quick_status = f"shell · 运行中 · {action.text[:100]}"
                    quick_unsubscribe = loop.bus.subscribe(renderer)
                    quick_task = asyncio.create_task(
                        controller.execute_shell_shortcut(action.text))
                    restore_prompt()
                    continue

                if quick_task is not None and action.kind is InputKind.CONTROL:
                    prompt.hide()
                    await cancel_quick_task(show_notice=action.command != "/exit")
                    if action.command == "/exit":
                        return 0
                    restore_prompt()
                    continue

                # RUNNING：控制命令、展示命令、steering 或下一条排队。
                if runner.is_running:
                    prompt.hide()
                    if action.kind is InputKind.CONTROL:
                        if action.command == "/stop":
                            await runner.finish_current(interrupt=True)
                            restore_prompt()
                            continue
                        if action.command == "/exit":
                            await runner.finish_current(
                                interrupt=True,
                                run_queued=False,
                                show_stopped_note=False,
                            )
                            return 0
                    if action.kind is InputKind.UI_COMMAND:
                        await handle_command(action)
                        restore_prompt()
                        continue
                    if action.kind is InputKind.COMMAND:
                        spec = processor.command_registry.resolve(action.command)
                        if spec is not None and spec.available_during_run:
                            prompt.hide()
                            await handle_command(action)
                            restore_prompt()
                            continue
                        print(RUNNING_HINT)
                        restore_prompt()
                        continue
                    if action.kind is InputKind.PROMPT:
                        self.presenter.show_attachment_notes(action)
                        if action.delivery is InputDelivery.QUEUE:
                            position = runner.queue_prompt(
                                to_session_prompt(action))
                            print(f"  ⎿ 下一条已排队（队列 #{position}，"
                                  f"当前任务完成后自动发送）: "
                                  f"{_queue_label(action, 60)}")
                        else:
                            preview = controller.steer(
                                action.text,
                                images=list(action.images) or None,
                            )
                            print(f"  ⎿ 插话已排队（模型下一步看到）: {preview}")
                    restore_prompt()
                    continue

                # IDLE：控制命令、普通消息、业务/展示命令。
                if action.kind is InputKind.CONTROL:
                    prompt.hide()
                    if action.command == "/exit":
                        return 0
                    print("  （当前没有运行中的任务）")
                    restore_prompt()
                    continue

                if action.kind is InputKind.PROMPT:
                    prompt.hide()
                    self.presenter.show_attachment_notes(action)
                    if action.delivery is InputDelivery.QUEUE:
                        position = runner.queue_prompt(to_session_prompt(action))
                        print(f"  ⎿ 下一条已排队（队列 #{position}，"
                              f"当前任务已结束，立即发送）: "
                              f"{_queue_label(action, 60)}")
                        runner.start_queued()
                    else:
                        runner.start(to_session_prompt(action))
                    restore_prompt()
                    continue

                prompt.hide()
                handled_command = await handle_command(action)
                if (isinstance(handled_command, CommandResult)
                        and handled_command.run_prompt is not None):
                    skill_prompt = SessionPrompt.create(
                        handled_command.run_prompt,
                        tool_allowlist=handled_command.tool_allowlist,
                        reminder_ids=handled_command.reminder_ids,
                        label=_queue_label(action),
                    )
                    runner.start(skill_prompt)
                restore_prompt()
        finally:
            status_task.cancel()
            await asyncio.gather(status_task, return_exceptions=True)
            if reader_task is not None:
                reader_task.cancel()
                await asyncio.gather(reader_task, return_exceptions=True)
            processor_task.cancel()
            action_get.cancel()
            job_notice_get.cancel()
            unsubscribe_job_notifications()
            await asyncio.gather(
                processor_task, action_get, job_notice_get, return_exceptions=True)
            if quick_task is not None:
                if not quick_task.done():
                    quick_task.cancel()
                await asyncio.gather(quick_task, return_exceptions=True)
                quick_task = None
            if quick_unsubscribe is not None:
                quick_unsubscribe()
                quick_unsubscribe = None
            await runner.close()
            if approval_broker is not None:
                approval_broker.cancel_all()
            if input_broker is not None:
                input_broker.cancel_all()
            state.finish_turn()
            prompt.finish()
            presenter.stop_alt_screen()


__all__ = ["ConversationHost"]
