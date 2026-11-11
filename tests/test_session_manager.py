"""多会话管理测试：list/inspect/format + REPL 目标解析。"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from krelcode.core.messages import Message
from krelcode.session.codec import history_to_dicts
from krelcode.session.manager import (
    format_sessions, inspect_session, list_sessions,
)
from krelcode.ui.repl import _resolve_resume_target


def write_session(path: Path, first_input: str, history, runs: int = 1):
    lines = [
        {"type": "user_input", "ts": time.time(), "content": first_input},
    ]
    for _ in range(runs):
        lines.append({"type": "run_finished", "ts": time.time()})
    lines.append({"type": "history", "ts": time.time(),
                  "messages": history_to_dicts(history)})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l, ensure_ascii=False) for l in lines) + "\n",
                    encoding="utf-8")


@pytest.fixture
def sessions_dir(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    h1 = [Message.user("旧会话问题"), Message.assistant("旧回复")]
    h2 = [Message.user("新会话问题"), Message.assistant("新回复"), Message.user("追问")]
    write_session(d / "old.jsonl", "旧会话问题", h1, runs=1)
    write_session(d / "new.jsonl", "新会话问题", h2, runs=2)
    os.utime(d / "old.jsonl", (1000, 1000))
    os.utime(d / "new.jsonl", (2000, 2000))
    return d


def test_inspect_session_metadata(sessions_dir):
    info = inspect_session(sessions_dir / "new.jsonl")
    assert info is not None
    assert info.num_runs == 2
    assert info.num_messages == 3
    assert info.preview == "新会话问题"


def test_inspect_session_empty_or_corrupt(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert inspect_session(empty) is None
    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text("{{{not json\n", encoding="utf-8")
    assert inspect_session(corrupt) is None


def test_list_sessions_sorted_new_first(sessions_dir):
    infos = list_sessions(sessions_dir)
    assert [i.path.name for i in infos] == ["new.jsonl", "old.jsonl"]


def test_list_sessions_missing_dir(tmp_path):
    assert list_sessions(tmp_path / "nope") == []


def test_format_sessions_output(sessions_dir):
    text = format_sessions(list_sessions(sessions_dir))
    assert "[1]" in text and "new.jsonl" in text
    assert "2 轮" in text and "新会话问题" in text
    assert format_sessions([]) == "  （暂无会话）"


def test_resolve_resume_targets(sessions_dir):
    infos = list_sessions(sessions_dir)
    assert _resolve_resume_target("latest", sessions_dir) == infos[0].path
    assert _resolve_resume_target("1", sessions_dir) == infos[0].path
    assert _resolve_resume_target("2", sessions_dir) == infos[1].path
    assert _resolve_resume_target("99", sessions_dir) is None
    assert _resolve_resume_target(str(infos[1].path), sessions_dir) == infos[1].path
    assert _resolve_resume_target("ghost.jsonl", sessions_dir) is None


def test_repl_resume_swaps_history(sessions_dir, tmp_path):
    """/resume 的核心动作：restore_session 换掉 loop.history。"""
    from krelcode.app import restore_session
    from krelcode.agent.loop import AgentLoop
    from krelcode.llm.providers.mock import MockProvider
    from krelcode.safety.policy import AllowAllPolicy
    from krelcode.tools.base import ToolContext
    from krelcode.tools.registry import ToolRegistry

    loop = AgentLoop(
        llm=MockProvider(), registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), system_prompt="SYS",
    )
    assert len(loop.history) == 1
    ok = restore_session(loop, sessions_dir / "new.jsonl")
    assert ok
    assert loop.history[1].content == "新会话问题"
    # 快照无 system 时自动补一条（3 条快照 + 1 条 system）
    assert len(loop.history) == 4 and loop.history[0].role == "system"
