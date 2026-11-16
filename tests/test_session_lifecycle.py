"""第三阶段：会话 metadata、搜索、分支、重命名、归档和 native 导出。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from krelcode.agent.loop import AgentLoop
from krelcode.agent.runtime import AgentRuntime
from krelcode.core.events import EventBus, RunCancelled
from krelcode.core.messages import Message
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.codec import history_to_dicts
from krelcode.session.bootstrap import setup_session
from krelcode.session.commands import builtin_command_registry
from krelcode.session.controller import SessionController
from krelcode.session.export import export_native_session
from krelcode.session.lifecycle import (
    SessionConfirmationRequired,
    SessionLifecycle,
    SessionLifecycleError,
)
from krelcode.session.memory import MemoryStore
from krelcode.session.manager import (
    append_session_metadata,
    inspect_session,
    search_sessions,
)
from krelcode.session.recorder import SessionRecorder, load_history
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry


def make_loop(tmp_path: Path) -> AgentLoop:
    return AgentLoop(
        llm=MockProvider(scripted=[]),
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def start_session(
    loop: AgentLoop,
    sessions_dir: Path,
    *,
    title: str = "",
    session_id: str = "parent",
) -> Path:
    path = sessions_dir / f"{session_id}.jsonl"
    recorder = SessionRecorder(
        path,
        lambda: loop.history,
        metadata={
            "session_id": session_id,
            "title": title,
            "provider": "mock",
            "model": "mock-model",
            "workspace": str(loop.tool_context.workspace_root),
        },
    )
    recorder.attach(loop.bus)
    recorder.flush_history()
    loop.session_recorder = recorder
    return path


def test_inspect_metadata_and_search_all_user_inputs(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = sessions / "s1.jsonl"
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in [
        {"type": "session_start", "session_id": "sid-1", "title": "登录重构",
         "provider": "deepseek", "model": "deepseek-chat"},
        {"type": "user_input", "content": "先看认证模块"},
        {"type": "user_input", "content": "再检查 token 刷新"},
        {"type": "history", "messages": history_to_dicts([Message.user("x")])},
    ]) + "\n", encoding="utf-8")

    info = inspect_session(path)
    assert info is not None
    assert info.session_id == "sid-1"
    assert info.title == "登录重构"
    assert info.provider == "deepseek"
    assert [item.path for item in search_sessions(sessions, "TOKEN")] == [path]
    assert search_sessions(sessions, "不存在") == []


def test_setup_session_writes_provider_model_and_workspace_metadata(tmp_path):
    loop = make_loop(tmp_path)
    args = SimpleNamespace(resume=None, no_session=False)

    path = setup_session(args, loop)

    records = [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines()]
    start = records[0]
    assert start["type"] == "session_start"
    assert start["session_id"] == path.stem
    assert start["provider"] == "mock"
    assert start["model"] == loop.llm.model
    assert start["workspace"] == str(tmp_path)
    assert loop.session_recorder.path == path


def test_lifecycle_fork_rebinds_session_memory_scope(tmp_path):
    loop = make_loop(tmp_path)
    sessions = tmp_path / ".krelcode" / "sessions"
    sessions.mkdir(parents=True)
    original = start_session(loop, sessions, session_id="memory-parent")
    store = MemoryStore(
        tmp_path, user_path=tmp_path / "global" / "entries.jsonl",
        session_id="memory-parent")
    loop.memory_store = store
    lifecycle = SessionLifecycle(
        loop, sessions, original, runtime=AgentRuntime(loop))

    forked = lifecycle.fork("memory fork")

    assert store.session_id == forked.stem
    assert store.session_id != "memory-parent"
    assert store.session_path == store.project_path.parent / "sessions" / (
        forked.stem + ".jsonl")


def test_lifecycle_fork_rename_and_recorder_rebinding(tmp_path):
    loop = make_loop(tmp_path)
    sessions = tmp_path / ".krelcode" / "sessions"
    sessions.mkdir(parents=True)
    original = start_session(loop, sessions, title="original")
    runtime = AgentRuntime(loop)
    lifecycle = SessionLifecycle(
        loop, sessions, str(original), runtime=runtime)

    forked = lifecycle.fork("feature branch")
    assert forked != original
    assert lifecycle.session_path == forked
    assert loop.session_recorder.path == forked
    assert loop.tool_context.runtime.session_id == forked.stem
    info = inspect_session(forked)
    assert info is not None
    assert info.title == "feature branch"
    assert info.parent_id == "parent"
    assert load_history(forked) == loop.history

    lifecycle.rename("renamed branch")
    assert inspect_session(forked).title == "renamed branch"


def test_lifecycle_resume_creates_branch_without_overwriting_source(tmp_path):
    loop = make_loop(tmp_path)
    sessions = tmp_path / ".krelcode" / "sessions"
    sessions.mkdir(parents=True)
    current = start_session(loop, sessions, session_id="current")
    target = sessions / "target.jsonl"
    target_history = [Message.user("target question"), Message.assistant("target answer")]
    target.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in [
        {"type": "session_start", "session_id": "target-id", "title": "target"},
        {"type": "history", "messages": history_to_dicts(target_history)},
    ]) + "\n", encoding="utf-8")
    original_bytes = target.read_bytes()
    lifecycle = SessionLifecycle(loop, sessions, str(current))

    assert lifecycle.resume(target, log=lambda line: None) is True
    assert loop.history[-2].content == "target question"
    assert lifecycle.session_path != current
    assert inspect_session(lifecycle.session_path).parent_id == "target-id"
    assert target.read_bytes() == original_bytes


def test_delete_requires_confirmation_and_archives_non_active_session(tmp_path):
    loop = make_loop(tmp_path)
    sessions = tmp_path / ".krelcode" / "sessions"
    sessions.mkdir(parents=True)
    active = start_session(loop, sessions, session_id="active")
    other = start_session(loop, sessions, session_id="other")
    lifecycle = SessionLifecycle(loop, sessions, str(active))

    try:
        lifecycle.delete(str(other))
    except SessionConfirmationRequired:
        pass
    else:
        raise AssertionError("delete 应要求确认")
    assert other.exists()

    deleted = lifecycle.delete(str(other), confirmed=True)
    assert not other.exists()
    assert deleted.archive_path.exists()
    try:
        lifecycle.delete(str(active), confirmed=True)
    except SessionLifecycleError as exc:
        assert "当前活动会话" in str(exc)
    else:
        raise AssertionError("不应允许删除活动会话")


def test_native_export_is_resumeable_and_cancel_flushes_history(tmp_path):
    loop = make_loop(tmp_path)
    sessions = tmp_path / ".krelcode" / "sessions"
    sessions.mkdir(parents=True)
    start_session(loop, sessions, session_id="live")
    exported = export_native_session(
        loop.history, tmp_path / "share.json",
        metadata={"title": "portable", "provider": "mock"})
    assert load_history(exported) == loop.history
    payload = json.loads(exported.read_text(encoding="utf-8"))
    assert payload["format"] == "krelcode-session"

    loop.history.append(Message.assistant("after cancel"))
    recorder = loop.session_recorder

    async def cancel():
        await EventBus().emit(RunCancelled("test"))

    asyncio.run(cancel())  # 独立 bus 不影响 recorder
    asyncio.run(recorder.on_event(RunCancelled("test")))
    assert load_history(recorder.path)[-1].content == "after cancel"


def test_session_commands_fork_rename_search_and_export_native(tmp_path):
    loop = make_loop(tmp_path)
    sessions = tmp_path / ".krelcode" / "sessions"
    sessions.mkdir(parents=True)
    original = start_session(loop, sessions, title="alpha")
    loop.history.append(Message.user("searchable requirement"))
    loop.session_recorder.flush_history()
    runtime = AgentRuntime(loop)
    controller = SessionController(
        runtime, session_path=str(original), sessions_dir=sessions)

    renamed = asyncio.run(controller.execute_command("/rename beta"))
    assert "已重命名" in "\n".join(renamed.output)
    forked = asyncio.run(controller.execute_command("/fork gamma"))
    assert "已创建并切换" in "\n".join(forked.output)
    assert controller.session_path != str(original)

    search = asyncio.run(controller.execute_command("/sessions search requirement"))
    assert "gamma" in "\n".join(search.output)
    export_path = tmp_path / "portable.json"
    exported = asyncio.run(controller.execute_command(f"/export {export_path}"))
    assert "已导出" in "\n".join(exported.output)
    assert load_history(export_path)[-1].content == "searchable requirement"
    assert builtin_command_registry().resolve("/delete") is not None


def test_setup_session_binds_session_scoped_memory(tmp_path):
    loop = make_loop(tmp_path)
    store = MemoryStore(
        tmp_path, user_path=tmp_path / "global" / "entries.jsonl")
    store.add("initial session note", scope="session")
    loop.memory_store = store
    args = SimpleNamespace(resume=None, no_session=False)

    path = setup_session(args, loop)

    assert store.session_id == path.stem
    assert store.session_path.is_file()
    assert store.list(scopes=("session",))[0].text == "initial session note"
