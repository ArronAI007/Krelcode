"""Step 32 测试：/allow 审批规则管理 + /model 运行时热切换。"""

import asyncio
import json
from pathlib import Path

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.app import build_model_switcher, build_parser
from krelcode.agent.usage import UsageTracker
from krelcode.llm.providers.mock import MockProvider
from krelcode.llm.retry import RetryingProvider
from krelcode.safety.policy import AllowAllPolicy, SandboxPolicy
from krelcode.safety.rules import AllowRules
from krelcode.tools.base import ToolContext
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


def drive(loop, lines, **kw):
    return asyncio.run(run_repl(loop, input_fn=ScriptedInput(lines), **kw))


def make_loop(tmp_path, policy=None):
    from krelcode.agent.compression import ContextManager
    llm = MockProvider(fallback="ok")
    return AgentLoop(
        llm=llm,
        registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=policy or AllowAllPolicy(),
        system_prompt="SYS",
        context_manager=ContextManager(llm, max_tokens=100_000),
    )


# -- AllowRules 管理方法 -----------------------------------------------------------------------------

def test_allow_rules_add_remove_persistence(tmp_path):
    path = tmp_path / "allow_rules.json"
    rules = AllowRules(path)
    assert rules.add_program("pytest") is True
    assert rules.add_program("pytest") is False        # 幂等
    assert rules.add_program("  ") is False            # 空白拒绝
    assert json.loads(path.read_text()) == {"allow_programs": ["pytest"]}

    rules2 = AllowRules(path)                          # 重载
    assert rules2.matches("pytest -q tests/")
    assert rules2.remove_program("pytest") is True
    assert rules2.remove_program("pytest") is False    # 已不存在
    assert json.loads(path.read_text()) == {"allow_programs": []}


# -- REPL /allow -----------------------------------------------------------------------------------

def test_repl_allow_commands(tmp_path, capsys):
    policy = SandboxPolicy(approver=None,
                           rules_path=tmp_path / ".krelcode" / "allow_rules.json")
    loop = make_loop(tmp_path, policy=policy)
    drive(loop, ["/allow", "/allow add pytest", "/allow", "/allow remove pytest",
                 "/allow", "/allow bad args here", "/exit"])
    out = capsys.readouterr().out
    assert "（空）" in out                              # 初始为空
    assert "已添加: pytest" in out
    assert "pytest" in out                              # 列表显示
    assert "已移除: pytest" in out
    assert "用法: /allow" in out                        # 非法参数给用法
    # 持久化文件已更新
    assert (tmp_path / ".krelcode" / "allow_rules.json").is_file()


def test_repl_allow_without_sandbox(tmp_path, capsys):
    loop = make_loop(tmp_path)                          # AllowAllPolicy
    drive(loop, ["/allow", "/exit"])
    assert "无审批白名单" in capsys.readouterr().out


# -- /model 热切换 -----------------------------------------------------------------------------------

def make_switcher(tmp_path, monkeypatch=None, retries=0):
    parser = build_parser()
    args = parser.parse_args(["--mock", "--workspace", str(tmp_path),
                              "--retries", str(retries)])
    loop = make_loop(tmp_path)
    usage = UsageTracker(model="mock-model")
    return build_model_switcher(args, {}, loop, usage), loop, usage


def test_model_switch_current_info(tmp_path):
    switch, loop, _ = make_switcher(tmp_path)
    msg = switch(None)
    assert "当前: mock" in msg


def test_model_switch_from_mock_requires_provider(tmp_path):
    switch, _, _ = make_switcher(tmp_path)
    msg = switch("gpt-4o")
    assert "显式指定目标 provider" in msg               # mock 基座不能裸切


def test_model_switch_to_openai(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    switch, loop, usage = make_switcher(tmp_path, retries=2)
    msg = switch("openai/gpt-4o-mini")
    assert "已切换: openai / gpt-4o-mini" in msg
    assert "压缩预算 96000" in msg                       # 窗口×75% 联动
    assert isinstance(loop.llm, RetryingProvider)        # 重试包装保持
    assert loop.llm.model == "gpt-4o-mini"
    assert loop.context_manager is None or usage.model == "gpt-4o-mini"


def test_model_switch_same_provider_keeps_base(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    switch, loop, _ = make_switcher(tmp_path)
    switch("openai/gpt-4o-mini")
    switch2 = build_model_switcher(
        build_parser().parse_args(["--provider", "openai", "--workspace", str(tmp_path)]),
        {}, loop, UsageTracker())
    # 已切到 openai 后，裸模型名沿用当前 provider
    msg = switch2("gpt-4o")
    assert "已切换: openai / gpt-4o" in msg


def test_model_switch_missing_key_fails_gracefully(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    switch, loop, _ = make_switcher(tmp_path)
    msg = switch("anthropic/claude-sonnet-4-5")
    assert "缺少 API key" in msg
    assert isinstance(loop.llm, MockProvider)            # 原 provider 未被破坏


def test_model_switch_unknown_provider(tmp_path):
    switch, loop, _ = make_switcher(tmp_path)
    msg = switch("nope/model-x")
    assert "切换失败" in msg
    assert isinstance(loop.llm, MockProvider)


def test_model_switch_explicit_budget_not_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    parser = build_parser()
    args = parser.parse_args(["--mock", "--workspace", str(tmp_path),
                              "--context-tokens", "777"])
    loop = AgentLoop(
        llm=MockProvider(), registry=ToolRegistry(),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), system_prompt="SYS",
    )
    # 手动挂一个 context_manager 模拟 build_loop 行为
    from krelcode.agent.compression import ContextManager
    loop.context_manager = ContextManager(loop.llm, max_tokens=777)
    switch = build_model_switcher(args, {}, loop, UsageTracker())
    msg = switch("openai/gpt-4o-mini")
    assert "已切换" in msg
    assert loop.context_manager.max_tokens == 777        # 显式预算不被覆盖


def test_repl_model_command(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    switch, loop, _ = make_switcher(tmp_path)
    drive(loop, ["/model", "/model openai/gpt-4o-mini", "/exit"], model_switch=switch)
    out = capsys.readouterr().out
    assert "当前: mock" in out
    assert "已切换: openai / gpt-4o-mini" in out
