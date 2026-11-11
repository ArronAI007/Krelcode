"""Step 22c 测试：REPL 全命令自动化（input_fn 注入，零真实终端）。

此前 REPL 命令只靠人工演示验证；这里把每个 /command 都固化为回归测试。
"""

import asyncio
import json
import time
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.usage import UsageTracker
from krelcode.core.messages import Message, ToolCall
from krelcode.core.errors import LLMError
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy
from krelcode.session.checkpoint import CheckpointManager
from krelcode.session.codec import history_to_dicts
from krelcode.tools.base import ToolContext
from krelcode.tools.builtin.time_tool import GetTimeTool
from krelcode.tools.registry import ToolRegistry
from krelcode.ui.repl import run_repl


class ScriptedInput:
    """按脚本供行，耗尽后 EOF（等价于用户 Ctrl-D）。"""

    def __init__(self, lines):
        self.lines = list(lines)

    async def __call__(self, prompt):
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


def make_loop(tmp_path, scripted=None, fallback="[fallback]", **provider_kw):
    llm = MockProvider(scripted=scripted or [], fallback=fallback, **provider_kw)
    return AgentLoop(
        llm=llm, registry=ToolRegistry([GetTimeTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), system_prompt="SYS",
    )


def drive(loop, lines, **kw):
    return asyncio.run(run_repl(loop, input_fn=ScriptedInput(lines), **kw))


# -- 基础流 ---------------------------------------------------------------------------

def test_exit_command_and_eof(tmp_path):
    loop = make_loop(tmp_path)
    assert drive(loop, ["/exit"]) == 0
    assert drive(loop, []) == 0                     # EOF 同样干净退出


def test_help_and_tools(tmp_path, capsys):
    loop = make_loop(tmp_path)
    drive(loop, ["/help", "/tools", "/exit"])
    out = capsys.readouterr().out
    assert "可用命令" in out and "/export" in out
    assert "get_time:" in out


def test_plain_message_streamed_reply(tmp_path, capsys):
    loop = make_loop(tmp_path, scripted=["你好回复"])
    drive(loop, ["hi", "/exit"])
    out = capsys.readouterr().out
    assert "agent> 你好回复" in out
    assert loop.history[1] == Message.user("hi")


def test_exception_path_prints_error(tmp_path, capsys):
    class BoomProvider(MockProvider):
        async def stream(self, messages, tools=None):
            raise LLMError("服务不可用")
            yield  # pragma: no cover

    loop = AgentLoop(
        llm=BoomProvider(), registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), system_prompt="SYS",
    )
    rc = drive(loop, ["x", "/exit"])
    assert rc == 0                                   # REPL 不因单轮失败退出
    assert "错误：LLMError" in capsys.readouterr().out


# -- @文件引用 ---------------------------------------------------------------------------

def test_at_file_expansion_end_to_end(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("文件内容A", encoding="utf-8")
    loop = make_loop(tmp_path, scripted=["ok"])
    drive(loop, ["解释 @a.txt", "/exit"])
    out = capsys.readouterr().out
    assert "[@] @a.txt: 已注入 a.txt" in out
    user_msg = loop.history[1].content
    assert '<attached_file path="a.txt">' in user_msg and "文件内容A" in user_msg


# -- 历史 / 重置 / 用量 ---------------------------------------------------------------------

def test_history_reset_compact_cost(tmp_path, capsys):
    loop = make_loop(tmp_path, scripted=["r1"],
                     usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5})
    usage = UsageTracker()
    usage.attach(loop.bus)
    drive(loop, ["hi", "/history", "/cost", "/compact", "/reset", "/exit"], usage=usage)
    out = capsys.readouterr().out
    assert "[user] hi" in out                        # /history
    assert "[cost]" in out and "1 次" in out          # /cost
    assert "上下文压缩未启用" in out                     # /compact（无 manager）
    assert "（历史已清空）" in out                       # /reset
    assert len(loop.history) == 1


# -- 未启用组件的提示 -------------------------------------------------------------------------

def test_disabled_components_messages(tmp_path, capsys):
    loop = make_loop(tmp_path)
    drive(loop, ["/session", "/agents", "/mcp", "/undo", "/exit"])
    out = capsys.readouterr().out
    assert "未启用" in out            # /session /undo
    assert "子代理未启用" in out       # /agents
    assert "MCP 未启用" in out        # /mcp


# -- 会话列表 / 切换 / 导出 ---------------------------------------------------------------------

def write_session_file(directory: Path, name: str, first_input: str, history):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    lines = [
        json.dumps({"type": "user_input", "ts": time.time(), "content": first_input},
                   ensure_ascii=False),
        json.dumps({"type": "run_finished", "ts": time.time()}),
        json.dumps({"type": "history", "ts": time.time(),
                    "messages": history_to_dicts(history)}, ensure_ascii=False),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_sessions_list_and_resume(tmp_path, capsys):
    sessions = tmp_path / ".krelcode" / "sessions"
    write_session_file(sessions, "s1.jsonl", "旧问题",
                       [Message.user("旧问题"), Message.assistant("旧回复")])
    loop = make_loop(tmp_path)
    drive(loop, ["/sessions", "/resume 1", "/history", "/resume 99", "/exit"],
          sessions_dir=sessions)
    out = capsys.readouterr().out
    assert "[1]" in out and "旧问题" in out             # /sessions 列表
    assert "已恢复会话" in out                           # /resume 1
    assert "[user] 旧问题" in out                        # 切换后的历史
    assert "找不到会话: 99" in out                       # 非法序号
    assert loop.history[1].content == "旧问题"


def test_export_command(tmp_path, capsys):
    loop = make_loop(tmp_path, scripted=["r1"])
    drive(loop, ["hi", "/export", "/exit"])
    out = capsys.readouterr().out
    assert "[export] 已导出" in out
    exports = list((tmp_path / ".krelcode" / "exports").glob("session-*.md"))
    assert len(exports) == 1
    content = exports[0].read_text(encoding="utf-8")
    assert "## 👤 User" in content and "hi" in content


def test_export_explicit_path(tmp_path, capsys):
    loop = make_loop(tmp_path, scripted=["r1"])
    target = tmp_path / "my_export.md"
    drive(loop, ["hi", f"/export {target}", "/exit"])
    assert target.is_file() and "# Krelcode 会话" in target.read_text(encoding="utf-8")


# -- /undo 与检查点集成 ----------------------------------------------------------------------------

def test_undo_command_restores_file(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("原始", encoding="utf-8")
    mgr = CheckpointManager(tmp_path)
    mgr.begin_group("上一轮")
    mgr.snapshot("a.txt")
    (tmp_path / "a.txt").write_text("改坏", encoding="utf-8")
    mgr.close_group()

    loop = make_loop(tmp_path)
    drive(loop, ["/undo", "/undo list", "/exit"], checkpoints=mgr)
    out = capsys.readouterr().out
    assert "[undo] 已回滚上一轮" in out and "a.txt" in out
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "原始"
    assert "无可回滚的检查点" in out      # /undo list 此时已空
