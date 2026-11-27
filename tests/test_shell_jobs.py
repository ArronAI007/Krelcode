"""后台 Shell Job Manager：进程、实时输出、工具、命令和事件边界。"""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.runtime import AgentRuntime
from krelcode.agent.usage import UsageTracker
from krelcode.core.events import (
    EventBus,
    RunStarted,
    ShellJobFinished,
    ShellJobOutput,
    ShellJobStarted,
)
from krelcode.core.messages import ToolCall
from krelcode.core.redact import SecretRedactor
from krelcode.jobs import (
    ShellJobError,
    ShellJobManager,
    ShellJobStatus,
)
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy, SandboxPolicy
from krelcode.safety.sandbox import SandboxedCommand
from krelcode.session.commands import CommandRequest, builtin_command_registry
from krelcode.session.commands.context import CommandContext
from krelcode.session.commands.handlers.runtime_ops import handle_jobs
from krelcode.session.recorder import SessionRecorder
from krelcode.session.stream_json import (
    StreamJsonEncoder,
    StreamJsonMetadata,
)
from krelcode.session.stream_json_runner import run_stream_json_session
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.job_tools import (
    JobKillTool,
    JobListTool,
    JobOutputTool,
    JobStartTool,
)
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.repl import run_repl


class ScriptedInput:
    def __init__(self, lines, delay=0.01):
        self.lines = list(lines)
        self.delay = delay

    async def __call__(self, prompt):
        if not self.lines:
            raise EOFError
        await asyncio.sleep(self.delay)
        return self.lines.pop(0)


def make_manager(tmp_path: Path, *, redactor=None, bus=None, **kwargs):
    bus = bus or EventBus()
    events = []
    bus.subscribe(events.append)
    manager = ShellJobManager(
        bus, workspace_root=tmp_path, redactor=redactor, **kwargs)
    return manager, bus, events


async def wait_terminal(manager: ShellJobManager, job_id: str, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        info = manager.get(job_id)
        if not info.running:
            return info
        await asyncio.sleep(0.02)
    raise AssertionError(f"job did not finish: {job_id}")


def make_loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        llm=MockProvider(fallback="ok"),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path, sandbox_enabled=False),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_background_output_is_visible_before_process_exit(tmp_path):
    async def scenario():
        manager, _, events = make_manager(tmp_path)
        try:
            job = await manager.start(
                "printf first; sleep 0.25; printf second", cwd=tmp_path)
            page = await manager.output(job.job_id, wait_seconds=0.5)
            assert page.text == "first"
            assert manager.get(job.job_id).running is True
            assert any(
                isinstance(event, ShellJobOutput) and event.text == "first"
                for event in events)

            final = await wait_terminal(manager, job.job_id)
            assert final.status is ShellJobStatus.COMPLETED
            rest = await manager.output(
                job.job_id, after_sequence=page.next_sequence)
            assert "second" in rest.text
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_output_cursor_is_monotonic_and_non_duplicating(tmp_path):
    async def scenario():
        manager, _, _ = make_manager(tmp_path)
        try:
            job = await manager.start(
                "printf 'one\\n'; sleep 0.05; printf 'two\\n'", cwd=tmp_path)
            await wait_terminal(manager, job.job_id)
            first = await manager.output(job.job_id)
            assert "one" in first.text and "two" in first.text
            assert first.next_sequence > 0
            second = await manager.output(
                job.job_id, after_sequence=first.next_sequence)
            assert second.text == ""
            assert second.next_sequence == first.next_sequence
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_nonzero_exit_and_finished_event_once(tmp_path):
    async def scenario():
        manager, _, events = make_manager(tmp_path)
        try:
            job = await manager.start(
                "printf boom; exit 7", cwd=tmp_path)
            info = await wait_terminal(manager, job.job_id)
            await asyncio.sleep(0.05)
            assert info.status is ShellJobStatus.FAILED
            assert info.exit_code == 7
            finished = [
                event for event in events if isinstance(event, ShellJobFinished)]
            assert len(finished) == 1
            assert "boom" in finished[0].preview
            assert finished[0].exit_code == 7
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group semantics")
def test_kill_terminates_running_process_and_marks_killed(tmp_path):
    async def scenario():
        manager, _, _ = make_manager(tmp_path)
        try:
            job = await manager.start(
                "sleep 30 & child=$!; echo $child; wait $child", cwd=tmp_path)
            await manager.output(job.job_id, wait_seconds=0.2)
            info = await manager.kill(job.job_id)
            assert info.status is ShellJobStatus.KILLED
            assert info.running is False
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_list_and_shutdown_clean_all_running_jobs(tmp_path):
    async def scenario():
        manager, _, _ = make_manager(tmp_path)
        one = await manager.start("sleep 30", cwd=tmp_path)
        two = await manager.start("sleep 30", cwd=tmp_path)
        assert {job.job_id for job in manager.list_jobs()} == {
            one.job_id, two.job_id}
        await manager.shutdown()
        assert all(
            not manager.get(job.job_id).running for job in (one, two))
        await manager.shutdown()  # 幂等

    asyncio.run(scenario())


def test_cwd_escape_and_active_limit_are_rejected(tmp_path):
    outside = tmp_path.parent

    async def scenario():
        manager, _, _ = make_manager(tmp_path, max_active=1)
        try:
            with pytest.raises(ShellJobError, match="越出工作区"):
                await manager.start("pwd", cwd=outside)
            await manager.start("sleep 30", cwd=tmp_path)
            with pytest.raises(ShellJobError, match="并发上限"):
                await manager.start("sleep 30", cwd=tmp_path)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_sandbox_audit_and_prepare_are_enforced(tmp_path):
    class FakeSandbox:
        def __init__(self):
            self.audit_calls = 0
            self.prepare_calls = 0

        def audit_command(self, command, cwd):
            self.audit_calls += 1
            if command == "deny":
                return "blocked by test"
            return None

        def prepare_shell(self, command, cwd):
            self.prepare_calls += 1
            return SandboxedCommand(
                ["/bin/sh", "-c", "printf sandboxed"],
                {"PATH": "/usr/bin:/bin"},
                "fake",
            )

    async def scenario():
        manager, _, _ = make_manager(tmp_path)
        sandbox = FakeSandbox()
        try:
            with pytest.raises(ShellJobError, match="Sandbox 拒绝"):
                await manager.start("deny", cwd=tmp_path, sandbox=sandbox)
            job = await manager.start(
                "printf original", cwd=tmp_path, sandbox=sandbox)
            await wait_terminal(manager, job.job_id)
            page = await manager.output(job.job_id)
            assert page.text == "sandboxed"
            assert sandbox.audit_calls == 2
            assert sandbox.prepare_calls == 1
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_realtime_stream_and_log_redact_split_secret(tmp_path):
    secret = "supersecret"

    async def scenario():
        redactor = SecretRedactor([secret])
        manager, _, events = make_manager(tmp_path, redactor=redactor)
        try:
            job = await manager.start(
                "printf super; sleep 0.05; printf 'secret\\n'", cwd=tmp_path)
            info = await wait_terminal(manager, job.job_id)
            page = await manager.output(job.job_id)
            assert page.text == "***\n"
            assert secret not in page.render()
            assert secret not in Path(info.log_path).read_text(encoding="utf-8")
            if os.name == "posix":
                assert stat.S_IMODE(Path(info.log_path).stat().st_mode) == 0o600
            payload = "\n".join(
                event.text for event in events
                if isinstance(event, ShellJobOutput))
            assert secret not in payload
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_long_output_keeps_bounded_tail_and_reports_truncation(tmp_path):
    async def scenario():
        manager, _, _ = make_manager(tmp_path, max_output_chars=1000)
        try:
            job = await manager.start(
                "i=0; while [ $i -lt 2000 ]; do printf x; i=$((i+1)); done",
                cwd=tmp_path,
            )
            await wait_terminal(manager, job.job_id)
            page = await manager.output(job.job_id, max_chars=5000)
            assert page.truncated is True
            assert len(page.text) <= 1000
            assert page.text == "x" * len(page.text)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_job_tools_start_output_list_and_kill(tmp_path):
    async def scenario():
        manager, _, _ = make_manager(tmp_path)
        ctx = ToolContext(
            workspace_root=tmp_path,
            sandbox_enabled=False,
            shell_jobs=manager,
        )
        try:
            started = await JobStartTool().execute(
                {"command": "printf tool-ready; sleep 30"}, ctx)
            assert not started.is_error
            job_id = started.data["job_id"]

            output = await JobOutputTool().execute(
                {"job_id": job_id, "wait_seconds": 0.5}, ctx)
            assert "tool-ready" in output.content
            listed = await JobListTool().execute({}, ctx)
            assert job_id in listed.content
            killed = await JobKillTool().execute({"job_id": job_id}, ctx)
            assert "killed" in killed.content
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_job_start_uses_same_command_permission_as_run_command(tmp_path):
    async def scenario():
        policy = SandboxPolicy(auto_approve=True, workspace_root=tmp_path)
        ctx = ToolContext(workspace_root=tmp_path, sandbox_enabled=False)
        safe = await policy.check(
            ToolCall("c1", "job_start", {"command": "git status"}), ctx)
        denied = await policy.check(
            ToolCall("c2", "job_start", {"command": "rm -rf /"}), ctx)
        assert safe.allowed is True
        assert denied.allowed is False
        assert "危险" in denied.reason

    asyncio.run(scenario())


def test_jobs_slash_command_list_output_and_kill(tmp_path):
    async def scenario():
        manager, _, _ = make_manager(tmp_path)
        loop = make_loop(tmp_path)
        job = await manager.start(
            "printf slash-output; sleep 30", cwd=tmp_path)

        def context(arguments: str) -> CommandContext:
            return CommandContext(
                request=CommandRequest("jobs", arguments),
                loop=loop,
                command_registry=builtin_command_registry(),
                workspace_root=tmp_path,
                jobs=manager,
            )

        try:
            listed = await handle_jobs(context("list"))
            assert job.job_id in "\n".join(listed.output)
            output = await handle_jobs(context(
                f"output {job.job_id} 0 --wait 0.5"))
            assert "slash-output" in "\n".join(output.output)
            killed = await handle_jobs(context(f"kill {job.job_id}"))
            assert "killed" in "\n".join(killed.output)
        finally:
            await manager.shutdown()

    asyncio.run(scenario())


def test_stream_json_encodes_job_event_families():
    encoder = StreamJsonEncoder(StreamJsonMetadata(
        session_id="s", cwd="/repo", provider="mock", model="m"))
    started = encoder.job_event_message(
        ShellJobStarted("job-1", "watch", 42, "/repo"))
    output = encoder.job_event_message(
        ShellJobOutput("job-1", 3, "line\n"))
    finished = encoder.job_event_message(
        ShellJobFinished("job-1", "watch", "completed", 0, "line"))
    assert started["type"] == "job" and started["subtype"] == "started"
    assert output["subtype"] == "output_delta"
    assert output["sequence"] == 3
    assert finished["subtype"] == "finished"
    assert finished["session_id"] == "s"


def test_stream_json_runner_emits_job_events_outside_turn_lifecycle(tmp_path):
    async def scenario():
        loop = make_loop(tmp_path)
        runtime = AgentRuntime(loop)
        output = io.StringIO()

        async def emit_during_turn(event):
            if isinstance(event, RunStarted):
                await loop.bus.emit(ShellJobOutput("job-1", 4, "live\n"))

        loop.bus.subscribe(emit_during_turn)
        code = await run_stream_json_session(
            runtime,
            session_path=None,
            input_processor=None,
            usage=UsageTracker(model="mock-model"),
            redactor=SecretRedactor(),
            initial_prompt="hello",
            output_stream=output,
            loop=loop,
        )
        return code, output.getvalue()

    code, raw = asyncio.run(scenario())
    assert code == 0
    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    job_events = [record for record in records if record.get("type") == "job"]
    assert job_events == [{
        "type": "job",
        "subtype": "output_delta",
        "job_id": "job-1",
        "job_sequence": 4,
        "text": "live\n",
        "sequence": 4,
        "session_id": None,
    }]


def test_recorder_audits_started_and_finished_without_output_chunks(tmp_path):
    path = tmp_path / "s.jsonl"
    bus = EventBus()
    recorder = SessionRecorder(path, lambda: [])
    recorder.attach(bus)

    async def scenario():
        await bus.emit(ShellJobStarted("job-1", "watch", 42, str(tmp_path)))
        await bus.emit(ShellJobOutput("job-1", 1, "x" * 10_000))
        await bus.emit(ShellJobFinished(
            "job-1", "watch", "completed", 0, "done"))

    asyncio.run(scenario())
    records = [json.loads(line) for line in path.read_text().splitlines()]
    types = [record["type"] for record in records]
    assert types == ["shell_job_started", "shell_job_finished"]
    assert records[-1]["preview"] == "done"


def test_runtime_close_shuts_down_shell_jobs(tmp_path):
    class Jobs:
        def __init__(self):
            self.calls = 0

        async def shutdown(self):
            self.calls += 1

    jobs = Jobs()
    runtime = AgentRuntime(make_loop(tmp_path), shell_jobs=jobs)

    async def scenario():
        await runtime.close()
        await runtime.close()

    asyncio.run(scenario())
    assert jobs.calls == 1


def test_repl_idle_receives_background_completion_notice(tmp_path, capsys):
    loop = make_loop(tmp_path)

    async def scenario():
        manager, _, _ = make_manager(tmp_path, bus=loop.bus)
        loop.tool_context.shell_jobs = manager
        completed = asyncio.Event()
        unsubscribe = loop.bus.subscribe(
            lambda event: completed.set()
            if isinstance(event, ShellJobFinished) else None
        )

        async def start_after_subscribe():
            await asyncio.sleep(0.05)
            await manager.start(
                "printf notified; sleep 0.01", cwd=tmp_path)

        exit_sent = False

        async def exit_after_completion(_prompt):
            nonlocal exit_sent
            if exit_sent:
                raise EOFError
            await asyncio.wait_for(completed.wait(), timeout=5.0)
            exit_sent = True
            return "/exit"

        starter = asyncio.create_task(start_after_subscribe())
        try:
            return await run_repl(
                loop,
                input_fn=exit_after_completion,
            )
        finally:
            unsubscribe()
            await starter
            await manager.shutdown()

    assert asyncio.run(scenario()) == 0
    out = capsys.readouterr().out
    assert "后台任务 job-1 completed" in out
    assert "notified" in out
