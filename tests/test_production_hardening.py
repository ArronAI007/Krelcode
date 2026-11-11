"""Step 18 测试：token 预算 / 敏感文件写保护 / headless JSON 输出。"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from krelcode.agent.loop import AgentLoop
from krelcode.core.events import EventBus
from krelcode.core.messages import Message, ToolCall
from krelcode.llm.providers.mock import MockProvider
from krelcode.safety.policy import AllowAllPolicy, SandboxPolicy, classify_protected_path
from krelcode.tools.base import ToolContext, ToolResult
from krelcode.tools.registry import ToolRegistry


class EchoTool:
    name = "echo"
    description = "echo"
    parameters: Dict[str, Any] = {"type": "object", "properties": {"m": {"type": "string"}}}

    async def execute(self, args, ctx) -> ToolResult:
        return ToolResult(content=f"echo: {args.get('m')}")


def tc(call_id, name="echo", args=None):
    return Message.assistant(tool_calls=[ToolCall(call_id, name, args or {"m": "x"})])


# -- 18a: token 预算 -----------------------------------------------------------------------------

USAGE_8 = {"input_tokens": 4, "output_tokens": 4, "total_tokens": 8}


def test_token_budget_aborts_before_next_llm_call(tmp_path):
    provider = MockProvider(scripted=[tc("c1"), tc("c2"), "不应被消费"], usage=USAGE_8)
    loop = AgentLoop(
        llm=provider, registry=ToolRegistry([EchoTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), bus=EventBus(), system_prompt="SYS",
        token_budget=15,
    )
    reply = asyncio.run(loop.run("跑起来"))
    # 8 → 16 tokens：第三次 LLM 调用前 16 > 15，中止
    assert "token 预算耗尽" in reply and "16" in reply
    assert len(provider.calls) == 2
    assert loop.history[-1].role == "assistant"


def test_token_budget_none_means_unlimited(tmp_path):
    provider = MockProvider(scripted=[tc("c1"), tc("c2"), "正常完成"], usage=USAGE_8)
    loop = AgentLoop(
        llm=provider, registry=ToolRegistry([EchoTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), bus=EventBus(), system_prompt="SYS",
    )
    assert asyncio.run(loop.run("x")) == "正常完成"


def test_token_budget_falls_back_to_input_plus_output(tmp_path):
    usage = {"input_tokens": 10, "output_tokens": 7}  # 无 total_tokens
    provider = MockProvider(scripted=[tc("c1"), "不应被消费"], usage=usage)
    loop = AgentLoop(
        llm=provider, registry=ToolRegistry([EchoTool()]),
        tool_context=ToolContext(workspace_root=tmp_path),
        policy=AllowAllPolicy(), bus=EventBus(), system_prompt="SYS",
        token_budget=10,
    )
    reply = asyncio.run(loop.run("x"))
    assert "token 预算耗尽" in reply and "17" in reply  # 10+7=17 > 10


# -- 18b: 敏感文件写保护 ----------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    (".env", True), ("config/.env.local", True), (".git/config", True),
    (".git", True), ("keys/server.pem", True), ("~/.ssh/id_rsa", True),
    (".krelcode/allow_rules.json", True),   # 防 LLM 给自己提权
    (".krelcode/sessions/x.jsonl", True),   # 防篡改会话/检查点
    ("krelcode.toml", True),                # 防注入 hooks/改 provider 配置
    ("sub/krelcode.toml", True),
    ("src/app.py", False), ("notes.md", False), ("env.txt", False),
])
def test_classify_protected_path(tmp_path, path, expected):
    got = classify_protected_path(path, tmp_path) is not None
    assert got is expected, path


def test_sandbox_policy_write_protection(tmp_path):
    ctx = ToolContext(workspace_root=tmp_path)
    policy = SandboxPolicy(approver=None)  # 非交互 → fail-closed

    async def check(path, tool="write_file"):
        return await policy.check(ToolCall("c", tool, {"path": path}), ctx)

    # 普通文件：放行且不询问
    assert asyncio.run(check("src/app.py")).allowed
    # 敏感文件：非交互拒绝
    d = asyncio.run(check(".env"))
    assert not d.allowed and "受保护路径" in d.reason
    d2 = asyncio.run(check(".git/hooks/post-checkout", tool="edit_file"))
    assert not d2.allowed
    # 交互批准后可以写
    approved = []
    async def approver(tool_call, reason):
        approved.append((tool_call, reason))
        return "yes"
    policy2 = SandboxPolicy(approver=approver)
    d3 = asyncio.run(policy2.check(ToolCall("c", "write_file", {"path": ".env"}), ctx))
    assert d3.allowed and len(approved) == 1


def test_allow_all_policy_unaffected(tmp_path):
    ctx = ToolContext(workspace_root=tmp_path)
    d = asyncio.run(AllowAllPolicy().check(
        ToolCall("c", "write_file", {"path": ".env"}), ctx))
    assert d.allowed  # --no-sandbox 语义不变：用户自担风险


# -- 18c: headless JSON 输出 ------------------------------------------------------------------------

def test_json_output_mode(tmp_path, capsys):
    from krelcode.app import async_main, build_parser

    args = build_parser().parse_args([
        "--mock", "-m", "现在几点？",
        "--workspace", str(tmp_path),
        "--no-session", "--no-context",
        "--output-format", "json",
    ])
    rc = asyncio.run(async_main(args))
    captured = capsys.readouterr()

    assert rc == 0
    # stdout 必须是纯净的单行 JSON（启动提示走 stderr）
    payload = json.loads(captured.out.strip())
    assert payload["ok"] is True
    assert payload["tools_used"] == ["get_time"]     # mock 演示脚本调用 get_time
    assert payload["denied"] == 0
    assert payload["session"] is None                # --no-session
    assert set(payload["usage"]) == {"calls", "input", "output", "total"}
    assert payload["usage"]["calls"] == 2            # tool-call 轮 + 总结轮
    assert "tool-call 循环" in payload["reply"]


def test_text_mode_still_prints_plain_reply(tmp_path, capsys):
    from krelcode.app import async_main, build_parser

    args = build_parser().parse_args([
        "--mock", "-m", "hi", "--workspace", str(tmp_path),
        "--no-session", "--no-context",
    ])
    rc = asyncio.run(async_main(args))
    out = capsys.readouterr().out
    assert rc == 0
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.strip().splitlines()[0])      # 文本模式不是 JSON
