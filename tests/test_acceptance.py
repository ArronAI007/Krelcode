"""Step 29：端到端行为验收套件（真实组件的全栈场景）。

与单元/集成测试的区别：这里用与 app.py 完全相同的组件装配整个产品
（default_tools + SandboxPolicy + SessionRecorder + CheckpointManager +
UsageTracker + AutoCommitter + SubagentManager），用脚本化 MockProvider
驱动典型 coding 任务，断言**最终工作区状态、审计痕迹与行为不变量**。

这是无真实模型条件下对"整机完成度"的行为级回归；真模型验收
（配 API key 跑自由任务）是它的补充而非替代。
"""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.agent.prompts import build_system_prompt
from krelcode.agent.usage import UsageTracker
from krelcode.app import default_tools
from krelcode.core.events import EventBus, PermissionDenied
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import SandboxPolicy
from krelcode.session.autocommit import AutoCommitter
from krelcode.session.checkpoint import CheckpointManager
from krelcode.session.recorder import SessionRecorder, load_history
from krelcode.subagent.manager import SubagentManager
from krelcode.tools.base import ToolContext
from krelcode.tools.registry import ToolRegistry

PY = f"'{sys.executable}'"
MOCK_USAGE = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}


def tc(call_id: str, name: str, **args) -> Message:
    return Message.assistant(tool_calls=[ToolCall(call_id, name, args)])


def build_stack(workspace, parent_script, child_script=None,
                with_git=False, auto_approve=True):
    """按 app.py 的方式装配全栈（除 UI/MCP 外的所有订阅者组件）。"""
    workspace = Path(workspace)
    bus = EventBus()
    tool_context = ToolContext(workspace_root=workspace)
    policy = SandboxPolicy(
        approver=None,   # 非交互：ASK 且无 auto_approve → 拒绝（fail-closed）
        rules_path=workspace / ".krelcode" / "allow_rules.json",
        bus=bus, auto_approve=auto_approve)
    registry = ToolRegistry(default_tools())
    manager = None
    if child_script is not None:
        manager = SubagentManager(
            llm=MockProvider(scripted=child_script),
            tool_factory=default_tools, tool_context=tool_context,
            policy=policy, bus=bus, max_steps=10)
        for tool in manager.build_tools():
            registry.register(tool)
    loop = AgentLoop(
        llm=MockProvider(scripted=parent_script, usage=MOCK_USAGE),
        registry=registry, tool_context=tool_context,
        policy=policy, bus=bus, system_prompt=build_system_prompt(),
        max_steps=12)
    usage = UsageTracker()
    usage.attach(bus)
    checkpoints = CheckpointManager(workspace)
    checkpoints.attach(bus)
    session_path = workspace / ".krelcode" / "sessions" / "acc.jsonl"
    recorder = SessionRecorder(session_path, lambda: loop.history)
    recorder.attach(bus)
    committer = None
    if with_git:
        committer = AutoCommitter(workspace)
        bus.subscribe(committer.on_event)
    return SimpleNamespace(
        loop=loop, bus=bus, usage=usage, checkpoints=checkpoints,
        recorder=recorder, session_path=session_path, manager=manager,
        committer=committer, policy=policy, workspace=workspace)


def read_records(path):
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()]


# -- 场景 1：修 bug 全链路（安全审批 + 审计 + 用量 + git 提交 + 检查点回滚）----------------------------

CHECK_CALC = (f"{PY} -B -c 'from calc import add; "
              "assert add(2, 3) == 5; print(\"PASS\")'")


@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
def test_scenario_1_bugfix_full_stack(tmp_path):
    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for k, v in (("user.email", "acc@test"), ("user.name", "acc"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(tmp_path), "config", k, v], check=True)

    script = [
        tc("r1", "read_file", path="calc.py"),
        tc("c1", "run_command", command=CHECK_CALC),                      # 失败
        tc("e1", "edit_file", path="calc.py",
           old_string="return a - b", new_string="return a + b"),
        tc("c2", "run_command", command=CHECK_CALC),                      # PASS
        "修复完成：add() 现返回 a + b，检查命令输出 PASS。",
    ]
    stack = build_stack(tmp_path, script, with_git=True)
    reply = asyncio.run(stack.loop.run("修复 calc.py 的 bug 并让检查通过"))

    # 1) 工作区最终状态
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == \
        "def add(a, b):\n    return a + b\n"
    assert reply == "修复完成：add() 现返回 a + b，检查命令输出 PASS。"

    # 2) 行为不变量：第一次检查失败（AssertionError 回填给 LLM），修复后 PASS
    tool_msgs = [m for m in stack.loop.history if m.role == "tool"]
    assert "exit_code: 1" in tool_msgs[1].content
    assert "AssertionError" in tool_msgs[1].content
    assert "PASS" in tool_msgs[3].content

    # 3) 审计痕迹：JSONL 事件流 + history 快照可完整恢复
    records = read_records(stack.session_path)
    types = [r["type"] for r in records]
    assert types.count("tool_call") == 4
    assert "run_finished" in types and "history" in types
    assert load_history(stack.session_path) == stack.loop.history

    # 4) 用量：5 次 LLM 调用 × 150 tokens
    assert stack.usage.session.calls == 5
    assert stack.usage.session.total == 750

    # 5) git 自动提交：消息含任务预览，工作区变干净（.krelcode 被本地排除）
    log = subprocess.run(["git", "-C", str(tmp_path), "log", "--oneline"],
                         capture_output=True, text=True)
    assert log.returncode == 0 and "krelcode: 修复 calc.py 的 bug" in log.stdout
    status = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain"],
                            capture_output=True, text=True)
    assert status.stdout.strip() == ""

    # 6) 检查点安全网：undo 恢复 buggy 版本
    result = stack.checkpoints.undo_last()
    assert result is not None and "calc.py" in result.restored
    assert "a - b" in (tmp_path / "calc.py").read_text(encoding="utf-8")


# -- 场景 2：子代理委派（父 spawn/wait，子真实干活，事件进父审计流）------------------------------------

def test_scenario_2_subagent_delegation(tmp_path):
    child_script = [
        tc("cw1", "write_file", path="fib.py",
           content="def fib(n):\n    return n if n < 2 else fib(n - 1) + fib(n - 2)\n"),
        tc("cs1", "check_syntax", path="fib.py"),
        "已实现 fib.py（递归定义），check_syntax 通过。",
    ]
    parent_script = [
        tc("s1", "spawn_agent", task="在 fib.py 实现 fib(n) 并自检语法"),
        tc("w1", "wait_agent", agent_id="a1", timeout=10),
        tc("v1", "read_file", path="fib.py"),
        "委派完成：fib.py 已就绪并验证。",
    ]
    stack = build_stack(tmp_path, parent_script, child_script=child_script)
    reply = asyncio.run(stack.loop.run("用子代理实现 fib 函数"))

    fib = tmp_path / "fib.py"
    assert fib.exists() and "def fib" in fib.read_text(encoding="utf-8")
    assert reply == "委派完成：fib.py 已就绪并验证。"

    # 父历史里能看到子代理报告与文件内容
    tool_msgs = [m for m in stack.loop.history if m.role == "tool"]
    assert "[a1] completed" in tool_msgs[1].content
    assert "已实现 fib.py" in tool_msgs[1].content
    assert "def fib" in tool_msgs[2].content

    handle = stack.manager.get("a1")
    assert handle.status == "completed" and handle.turns == 1
    # 防递归：子代理注册表里没有 spawn 系工具
    assert "spawn_agent" not in handle.loop.registry.names()

    # 子代理事件进了父审计流
    records = read_records(stack.session_path)
    assert any(r["type"] == "subagent_spawned" for r in records)
    assert any(r["type"] == "subagent_finished" and r["status"] == "completed"
               for r in records)
    asyncio.run(stack.manager.shutdown())


# -- 场景 3：重构改名（find_refs 定位 → replace_all → grep 验证零残留 → 语法自检）-------------------------

def test_scenario_3_refactor_rename(tmp_path):
    (tmp_path / "mathx.py").write_text(
        "def compute(x):\n    return x * 2\n\n\ndef helper():\n    return compute(3)\n",
        encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "from mathx import compute\n\nprint(compute(5))\n", encoding="utf-8")

    script = [
        tc("f1", "find_refs", symbol="compute"),
        tc("e1", "edit_file", path="mathx.py", old_string="compute",
           new_string="calculate", replace_all=True),
        tc("e2", "edit_file", path="main.py", old_string="compute",
           new_string="calculate", replace_all=True),
        tc("g1", "grep", pattern="compute"),
        tc("c1", "check_syntax"),
        "重命名完成：compute → calculate（2 个文件），grep 无残留，语法检查通过。",
    ]
    stack = build_stack(tmp_path, script)
    reply = asyncio.run(stack.loop.run("把 compute 重命名为 calculate"))

    assert reply == "重命名完成：compute → calculate（2 个文件），grep 无残留，语法检查通过。"
    mathx = (tmp_path / "mathx.py").read_text(encoding="utf-8")
    main = (tmp_path / "main.py").read_text(encoding="utf-8")
    assert "compute" not in mathx and "compute" not in main
    assert "def calculate(x):" in mathx and "return calculate(3)" in mathx
    assert "from mathx import calculate" in main and "print(calculate(5))" in main

    tool_msgs = [m for m in stack.loop.history if m.role == "tool"]
    assert "[def] def compute" in tool_msgs[0].content       # find_refs 定位
    assert "未找到匹配" in tool_msgs[3].content                # grep 零残留
    assert "无语法错误" in tool_msgs[4].content                # check_syntax 通过

    # 检查点一轮一组：undo 同时还原两个文件
    result = stack.checkpoints.undo_last()
    assert sorted(result.restored) == ["main.py", "mathx.py"]
    assert "compute" in (tmp_path / "mathx.py").read_text(encoding="utf-8")


# -- 场景 4：自我修正 + 敏感路径写保护 ----------------------------------------------------------------------

def test_scenario_4_self_correction_and_write_protection(tmp_path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    script = [
        # 错误的 old_string → 失败信息带「最接近的行」提示（Step 20）
        tc("e1", "edit_file", path="app.py",
           old_string="VALUE = 2", new_string="VALUE = 42"),
        tc("r1", "read_file", path="app.py"),
        # LLM "根据提示修正" → 成功
        tc("e2", "edit_file", path="app.py",
           old_string="VALUE = 1", new_string="VALUE = 42"),
        # 写 .env → SandboxPolicy 保护路径，非交互 fail-closed 拒绝
        tc("w1", "write_file", path=".env", content="SECRET=x"),
        "VALUE 已更新为 42；.env 写入被安全策略拒绝（受保护路径）。",
    ]
    stack = build_stack(tmp_path, script, auto_approve=False)
    denied = []
    stack.bus.subscribe(
        lambda e: denied.append(e) if isinstance(e, PermissionDenied) else None)

    reply = asyncio.run(stack.loop.run("把 VALUE 改成 42，顺便写个 .env"))

    # 工作区状态：正常修改生效，敏感文件未落地
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 42\n"
    assert not (tmp_path / ".env").exists()
    assert reply == "VALUE 已更新为 42；.env 写入被安全策略拒绝（受保护路径）。"

    tool_msgs = [m for m in stack.loop.history if m.role == "tool"]
    # 第一次编辑失败：未找到 + 相近行提示（含正确原文，LLM 可据此修正）
    assert "未找到" in tool_msgs[0].content
    assert "最接近的行" in tool_msgs[0].content and "VALUE = 1" in tool_msgs[0].content
    # 修正后成功
    assert "替换 1 处" in tool_msgs[2].content
    # .env 被拒：拒绝原因回填给 LLM + PermissionDenied 事件
    assert "被安全策略拒绝" in tool_msgs[3].content and "受保护路径" in tool_msgs[3].content
    assert len(denied) == 1 and denied[0].tool_call.name == "write_file"

    # 审计流里有 permission_denied 记录
    records = read_records(stack.session_path)
    assert any(r["type"] == "permission_denied" for r in records)
