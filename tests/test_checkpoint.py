"""Step 16 测试：检查点与回滚（快照/分组/undo/持久化/事件驱动/e2e）。"""

import asyncio
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import EventBus, RunFinished, RunStarted, ToolCallStarted
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.checkpoint import CheckpointManager
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.edit_file import EditFileTool
from krelcode.tools.builtin.write_file import WriteFileTool
from krelcode.tools.registry import ToolRegistry


# -- 单元：快照与恢复 -------------------------------------------------------------------------

def test_undo_restores_modified_file(tmp_path):
    (tmp_path / "a.txt").write_text("原始内容", encoding="utf-8")
    mgr = CheckpointManager(tmp_path)
    mgr.begin_group("改文件")
    assert mgr.snapshot("a.txt") is not None
    (tmp_path / "a.txt").write_text("被改坏", encoding="utf-8")   # 模拟工具修改
    mgr.close_group()

    result = mgr.undo_last()
    assert result is not None and result.restored == ["a.txt"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "原始内容"


def test_undo_deletes_created_file(tmp_path):
    mgr = CheckpointManager(tmp_path)
    mgr.begin_group("建文件")
    mgr.snapshot("new.py")                       # 快照时不存在 → existed=False
    (tmp_path / "new.py").write_text("x=1", encoding="utf-8")
    mgr.close_group()

    result = mgr.undo_last()
    assert result.deleted == ["new.py"] and result.restored == []
    assert not (tmp_path / "new.py").exists()


def test_same_group_keeps_earliest_version(tmp_path):
    (tmp_path / "f.txt").write_text("v1", encoding="utf-8")
    mgr = CheckpointManager(tmp_path)
    mgr.begin_group("两次修改")
    mgr.snapshot("f.txt")
    (tmp_path / "f.txt").write_text("v2", encoding="utf-8")
    mgr.snapshot("f.txt")                        # 第二次快照应被忽略
    (tmp_path / "f.txt").write_text("v3", encoding="utf-8")
    mgr.close_group()

    mgr.undo_last()
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "v1"


def test_empty_group_discarded(tmp_path):
    mgr = CheckpointManager(tmp_path)
    mgr.begin_group("纯聊天")
    mgr.close_group()
    assert mgr.undo_last() is None
    assert mgr.list_groups() == []
    assert not (tmp_path / ".krelcode" / "checkpoints").exists() or \
        list((tmp_path / ".krelcode" / "checkpoints").iterdir()) == []


def test_outside_path_snapshot_ignored(tmp_path):
    mgr = CheckpointManager(tmp_path)
    mgr.begin_group("越界")
    assert mgr.snapshot("../evil.txt") is None
    assert mgr.snapshot("/etc/passwd") is None
    mgr.close_group()
    assert mgr.undo_last() is None


def test_lifo_order_across_turns(tmp_path):
    (tmp_path / "a.txt").write_text("A1", encoding="utf-8")
    (tmp_path / "b.txt").write_text("B1", encoding="utf-8")
    mgr = CheckpointManager(tmp_path)

    mgr.begin_group("第一轮")
    mgr.snapshot("a.txt")
    (tmp_path / "a.txt").write_text("A2", encoding="utf-8")
    mgr.close_group()

    mgr.begin_group("第二轮")
    mgr.snapshot("b.txt")
    (tmp_path / "b.txt").write_text("B2", encoding="utf-8")
    mgr.close_group()

    assert len(mgr.list_groups()) == 2
    r2 = mgr.undo_last()                          # 先回滚第二轮
    assert r2.label == "第二轮"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "B1"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "A2"  # 第一轮还在
    r1 = mgr.undo_last()
    assert r1.label == "第一轮"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "A1"
    assert mgr.undo_last() is None


def test_groups_survive_restart(tmp_path):
    (tmp_path / "a.txt").write_text("原始", encoding="utf-8")
    mgr1 = CheckpointManager(tmp_path)
    mgr1.begin_group("重启前")
    mgr1.snapshot("a.txt")
    (tmp_path / "a.txt").write_text("改动", encoding="utf-8")
    mgr1.close_group()

    # 新实例（模拟重启/新会话）从磁盘重建组栈
    mgr2 = CheckpointManager(tmp_path)
    assert len(mgr2.list_groups()) == 1
    result = mgr2.undo_last()
    assert result.restored == ["a.txt"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "原始"


def test_corrupt_group_dir_skipped(tmp_path):
    checkpoints = tmp_path / ".krelcode" / "checkpoints"
    (checkpoints / "0001-bad").mkdir(parents=True)
    (checkpoints / "0001-bad" / "meta.json").write_text("{broken", encoding="utf-8")
    mgr = CheckpointManager(tmp_path)
    assert mgr.list_groups() == [] and mgr.undo_last() is None


# -- 事件驱动 ----------------------------------------------------------------------------------

def test_event_driven_group_lifecycle(tmp_path):
    (tmp_path / "a.txt").write_text("原始", encoding="utf-8")
    bus = EventBus()
    mgr = CheckpointManager(tmp_path)
    mgr.attach(bus)

    async def go():
        await bus.emit(RunStarted("帮我改 a.txt"))
        await bus.emit(ToolCallStarted(ToolCall("c1", "write_file", {"path": "a.txt", "content": "新"})))
        (tmp_path / "a.txt").write_text("新", encoding="utf-8")      # 模拟工具执行
        await bus.emit(ToolCallStarted(ToolCall("c2", "grep", {"pattern": "x"})))  # 非写工具忽略
        await bus.emit(RunFinished("完成"))

    asyncio.run(go())
    result = mgr.undo_last()
    assert result is not None and result.label == "帮我改 a.txt"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "原始"


# -- AgentLoop 端到端 ----------------------------------------------------------------------------

def test_e2e_write_then_edit_then_undo(tmp_path):
    (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
    bus = EventBus()
    mgr = CheckpointManager(tmp_path)
    mgr.attach(bus)

    scripted = [
        Message.assistant(tool_calls=[ToolCall(
            "e1", "edit_file",
            {"path": "code.py", "old_string": "x = 1", "new_string": "x = 2"})]),
        Message.assistant(tool_calls=[ToolCall(
            "w1", "write_file", {"path": "gen.txt", "content": "generated"})]),
        "两处改动完成。",
    ]
    loop = AgentLoop(
        llm=MockProvider(scripted=scripted),
        registry=ToolRegistry([EditFileTool(), WriteFileTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        bus=bus,
        system_prompt="SYS",
    )
    asyncio.run(loop.run("改 code.py 并生成 gen.txt"))

    # 改动真实发生
    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "x = 2\n"
    assert (tmp_path / "gen.txt").exists()

    # 一轮 = 一个组：undo 同时恢复两处
    result = mgr.undo_last()
    assert sorted(result.restored) == ["code.py"] and result.deleted == ["gen.txt"]
    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (tmp_path / "gen.txt").exists()
