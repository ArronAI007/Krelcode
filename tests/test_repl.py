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
    """按脚本供行，耗尽后 EOF（等价于用户 Ctrl-D）。

    每行前 sleep 让出事件循环：保证「瞬时 run 任务」在下一行到达前完成，
    消除 reader 预读与状态机的时序竞态（测试确定性）。
    """

    def __init__(self, lines, delay=0.01):
        self.lines = list(lines)
        self.delay = delay

    async def __call__(self, prompt):
        if not self.lines:
            raise EOFError
        await asyncio.sleep(self.delay)
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
    assert "⏺ 你好回复" in out
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


# -- /context 统计与刷新（Step 25）-------------------------------------------------------------------

def test_context_stats_and_refresh(tmp_path, capsys):
    (tmp_path / "proj.py").write_text("x = 1", encoding="utf-8")
    loop = make_loop(tmp_path, scripted=["r1"])
    drive(loop, ["/context", "/context refresh", "/exit"])
    out = capsys.readouterr().out
    assert "[context] 历史" in out and "tokens" in out          # 统计
    assert "已刷新" in out                                        # refresh
    system_msg = loop.history[0]
    assert system_msg.role == "system"
    assert "<workspace_context>" in system_msg.content
    assert "proj.py" in system_msg.content                      # 目录树已进 system


# -- 运行中 steering / stop（Step 26）----------------------------------------------------------------

class TimedInput:
    """每行带独立延迟（秒），精确控制"运行中输入"的时机。"""

    def __init__(self, items):
        self.items = list(items)   # [(text, delay_before)]

    async def __call__(self, prompt):
        if not self.items:
            raise EOFError
        text, delay = self.items.pop(0)
        if delay:
            await asyncio.sleep(delay)
        return text


class SlowToolProvider:
    """第一次调用睡 delay 秒后返回 echo tool_call；之后回 final。无状态判定。"""
    name = "slow-tool"
    model = "m"

    def __init__(self, delay=0.25, final="已完成"):
        self.delay = delay
        self.final = final
        self.calls = 0

    async def stream(self, messages, tools=None):
        from krelcode.llm.base import StreamFinished
        self.calls += 1
        n_assistant = sum(1 for m in messages if m.role == "assistant")
        if n_assistant == 0:
            await asyncio.sleep(self.delay)
            msg = Message.assistant(
                tool_calls=[ToolCall("c1", "echo", {"m": "x"})])
        else:
            msg = Message.assistant(self.final)
        yield StreamFinished(message=msg, finish_reason="stop", usage={})


class EchoTool:
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {"m": {"type": "string"}}}

    async def execute(self, args, ctx):
        from krelcode.tools.base import ToolResult
        return ToolResult(content=f"echo: {args.get('m')}")


def make_loop_with(tmp_path, provider, tools=None):
    return AgentLoop(
        llm=provider,
        registry=ToolRegistry(tools if tools is not None else [EchoTool(), GetTimeTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(),
        system_prompt="SYS",
    )


def test_steering_injected_while_running(tmp_path, capsys):
    loop = make_loop_with(tmp_path, SlowToolProvider(delay=0.25))
    inp = TimedInput([
        ("跑个任务", 0),
        ("顺便注意拼写", 0.05),   # 运行中输入 → steering
        ("/exit", 0.6),           # 等任务完成后退出
    ])
    rc = asyncio.run(run_repl(loop, input_fn=inp))
    out = capsys.readouterr().out
    assert rc == 0
    assert "插话已排队" in out
    assert "⎿ 插话已注入: 顺便注意拼写" in out     # 渲染器确认注入
    assert "⏺ 已完成" in out
    # 注入点在 tool 结果之后（合法交替位），带前缀
    steer = [m for m in loop.history
             if m.role == "user" and "[用户插话]" in (m.content or "")]
    assert len(steer) == 1 and steer[0].content == "[用户插话] 顺便注意拼写"


def test_stop_cancels_running_task(tmp_path, capsys):
    loop = make_loop_with(tmp_path, SlowToolProvider(delay=5))   # 长任务
    inp = TimedInput([("慢任务", 0), ("/stop", 0.1), ("/exit", 0.05)])
    rc = asyncio.run(run_repl(loop, input_fn=inp))
    out = capsys.readouterr().out
    assert rc == 0
    assert "⎿ 已中止" in out
    # 历史被修复：未答完的 user 消息后补了中止说明
    assert loop.history[-1].role == "assistant"
    assert loop.history[-1].content == "[已被用户中止]"


def test_thinking_can_expand_while_run_in_progress(tmp_path, capsys):
    class SlowThinkingProvider:
        name = "slow-thinking"
        model = "m"

        async def stream(self, messages, tools=None):
            from krelcode.llm.base import (
                StreamFinished, TextDelta, ThinkingDelta,
            )
            yield ThinkingDelta("# 分析\n\n- 先检查缓存")
            await asyncio.sleep(0.2)
            yield TextDelta("最终回答")
            yield StreamFinished(
                message=Message.assistant("最终回答"),
                finish_reason="stop", usage={})

    loop = make_loop_with(tmp_path, SlowThinkingProvider())
    inp = TimedInput([
        ("任务", 0),
        ("/thinking", 0.05),      # thinking 已生成，任务仍在运行
        ("/exit", 0.3),
    ])
    rc = asyncio.run(run_repl(loop, input_fn=inp))
    out = capsys.readouterr().out
    assert rc == 0
    assert "✻ Thinking（Markdown）" in out
    assert "# 分析\n\n- 先检查缓存" in out
    assert "任务运行中" not in out


def test_slash_command_while_running_shows_hint(tmp_path, capsys):
    loop = make_loop_with(tmp_path, SlowToolProvider(delay=0.3))
    inp = TimedInput([("任务", 0), ("/help", 0.05), ("/stop", 0.1), ("/exit", 0.05)])
    rc = asyncio.run(run_repl(loop, input_fn=inp))
    out = capsys.readouterr().out
    assert rc == 0
    assert "任务运行中" in out


def test_stop_when_idle(tmp_path, capsys):
    loop = make_loop_with(tmp_path, SlowToolProvider())
    drive(loop, ["/stop", "/exit"])
    assert "当前没有运行中的任务" in capsys.readouterr().out
