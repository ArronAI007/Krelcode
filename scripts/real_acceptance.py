#!/usr/bin/env python3
"""Krelcode 真模型验收脚本（Step 33）。

配好 API key 后对整机行为做端到端验收：

    export OPENAI_API_KEY=sk-...          # 或 ANTHROPIC_API_KEY / GEMINI_API_KEY
    python scripts/real_acceptance.py
    python scripts/real_acceptance.py --provider anthropic --model claude-sonnet-4-5
    python scripts/real_acceptance.py --workspace /tmp/acc --keep

7 个阶段各自 PASS/FAIL，进程退出码 = 失败阶段数。
无人值守：SandboxPolicy auto_approve=True（ASK 级放行，DENY 级仍拒绝）。
与 tests/test_acceptance.py（MockProvider 脚本化）互补：那边验证「机制」，
这边验证「真实模型驱动下的整机行为」。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

# 允许未安装时直接跑（源码树内）
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from krelcode.agent.loop import AgentLoop                      # noqa: E402
from krelcode.agent.pricing import default_budget_for          # noqa: E402
from krelcode.agent.prompts import build_system_prompt         # noqa: E402
from krelcode.agent.usage import UsageTracker                  # noqa: E402
from krelcode.agent.context import build_workspace_context     # noqa: E402
from krelcode.app import default_tools                         # noqa: E402
from krelcode.config import load_config                        # noqa: E402
from krelcode.core.events import EventBus                      # noqa: E402
from krelcode.llm.registry import create_provider, resolve_settings  # noqa: E402
from krelcode.llm.retry import RetryPolicy, RetryingProvider   # noqa: E402
from krelcode.safety.policy import SandboxPolicy               # noqa: E402
from krelcode.session.recorder import (                        # noqa: E402
    SessionRecorder, load_history, new_session_path,
)
from krelcode.tools.base import ToolContext                    # noqa: E402
from krelcode.tools.registry import ToolRegistry               # noqa: E402


# ---------------------------------------------------------------------------
# 阶段实现：每个返回 (ok: bool, detail: str)
# ---------------------------------------------------------------------------

async def stage_chat(loop: AgentLoop, ws: Path):
    reply = await loop.run("请只回复两个字：收到。不要调用任何工具。")
    ok = bool(reply and reply.strip())
    return ok, f"回复: {reply[:60]!r}"


async def stage_tool_call(loop: AgentLoop, ws: Path):
    await loop.run("请用 get_time 工具查询 UTC 时区的当前时间，然后告诉我结果。")
    called = any(
        m.role == "assistant" and m.tool_calls
        and any(tc.name == "get_time" for tc in m.tool_calls)
        for m in loop.history
    )
    return called, "get_time 已被调用" if called else "模型未调用 get_time 工具"


async def stage_file_ops(loop: AgentLoop, ws: Path):
    await loop.run(
        "请用 write_file 工具在工作区创建 hello.txt，内容为 'hello krelcode'，"
        "然后用 read_file 读回确认。")
    target = ws / "hello.txt"
    if not target.is_file():
        return False, "hello.txt 未被创建"
    content = target.read_text(encoding="utf-8")
    ok = "hello" in content.lower()
    return ok, f"hello.txt 内容: {content[:60]!r}"


async def stage_run_command(loop: AgentLoop, ws: Path):
    await loop.run("请用 run_command 工具执行 echo ok-123，并把输出原样告诉我。")
    in_history = any(
        m.role == "tool" and "ok-123" in (m.content or "") for m in loop.history)
    return in_history, "命令输出 ok-123 已回填" if in_history else "未见 ok-123 输出"


def stage_context_injection(loop: AgentLoop, ws: Path):
    system_msg = loop.history[0].content if loop.history else ""
    ok = "<workspace_context>" in (system_msg or "")
    return ok, "system prompt 含 <workspace_context>" if ok else "缺少工作区上下文"


def stage_session_record(session_path: Path, loop: AgentLoop):
    if not session_path.is_file():
        return False, "会话文件未生成"
    restored = load_history(session_path)
    ok = len(restored) > 0 and len(restored) == len(loop.history)
    return ok, f"会话快照 {len(restored)} 条消息，与内存历史一致" if ok else "快照缺失或不一致"


def stage_usage(usage: UsageTracker):
    ok = usage.session.calls >= 4 and usage.session.total > 0
    detail = usage.session.text() + usage.cost_note()
    return ok, detail


STAGES = [
    ("基础对话", stage_chat),
    ("工具调用 get_time", stage_tool_call),
    ("文件写入与读回", stage_file_ops),
    ("命令执行", stage_run_command),
    ("上下文注入", stage_context_injection),
    ("会话录制与恢复", None),     # 特殊：需要 session_path，主流程内联
    ("用量与成本", None),          # 特殊：需要 usage，主流程内联
]


# ---------------------------------------------------------------------------
# 装配与主流程
# ---------------------------------------------------------------------------

def build_stack(args, workspace: Path):
    config = load_config(Path(args.config) if args.config else None)
    settings = resolve_settings(
        provider=args.provider, model=args.model,
        base_url=args.base_url, config=config)
    provider = create_provider(settings)
    if args.retries > 0:
        provider = RetryingProvider(provider, RetryPolicy(max_retries=args.retries))

    bus = EventBus()
    loop = AgentLoop(
        llm=provider,
        registry=ToolRegistry(default_tools()),
        tool_context=ToolContext(workspace_root=workspace),
        policy=SandboxPolicy(
            approver=None, auto_approve=True, bus=bus,
            rules_path=workspace / ".krelcode" / "allow_rules.json"),
        bus=bus,
        system_prompt=build_system_prompt(),   # 上下文在 async_main 注入
        max_steps=args.max_steps,
        token_budget=args.token_budget,
    )
    usage = UsageTracker(model=getattr(provider, "model", None))
    usage.attach(bus)
    session_path = new_session_path(workspace / ".krelcode" / "sessions")
    SessionRecorder(session_path, lambda: loop.history).attach(bus)
    return loop, usage, session_path, provider


async def run_acceptance(args) -> int:
    tmp = None
    if args.workspace:
        workspace = Path(args.workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
    else:
        tmp = tempfile.TemporaryDirectory(prefix="krelcode-acc-")
        workspace = Path(tmp.name)

    try:
        loop, usage, session_path, provider = build_stack(args, workspace)
        # 注入真实工作区上下文（与 app 一致）
        context = await build_workspace_context(workspace)
        loop.history[0].content = build_system_prompt(context)
        if loop.context_manager is None:
            from krelcode.agent.compression import DEFAULT_MAX_TOKENS, ContextManager
            budget = (args.context_tokens or default_budget_for(
                getattr(provider, "model", None)) or DEFAULT_MAX_TOKENS)
            loop.context_manager = ContextManager(loop.llm, max_tokens=budget)

        print(f"== Krelcode 真模型验收 ==")
        print(f"provider: {provider.name} / {getattr(provider, 'model', '?')}"
              f" · workspace: {workspace}\n")

        failures = 0
        # 阶段 1-4：真实模型驱动的连续会话
        for idx, (name, fn) in enumerate(STAGES[:4], start=1):
            try:
                ok, detail = await fn(loop, workspace)
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"
            failures += 0 if ok else 1
            print(f"  [{'PASS' if ok else 'FAIL'}] 阶段{idx} {name}: {detail}")

        # 阶段 5-7：本地状态校验
        for idx, (name, fn, a) in enumerate([
                (STAGES[4][0], stage_context_injection, (loop, workspace)),
                (STAGES[5][0], stage_session_record, (session_path, loop)),
                (STAGES[6][0], stage_usage, (usage,))], start=5):
            try:
                ok, detail = fn(*a)
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"
            failures += 0 if ok else 1
            print(f"  [{'PASS' if ok else 'FAIL'}] 阶段{idx} {name}: {detail}")

        print(f"\n== 结果: {7 - failures}/7 通过 ==")
        if args.keep:
            print(f"工作区已保留: {workspace}")
        return failures
    finally:
        if tmp is not None and not args.keep:
            tmp.cleanup()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Krelcode 真模型验收（7 阶段）")
    p.add_argument("--provider", default=None, help="openai / anthropic / gemini（默认按配置/环境）")
    p.add_argument("--model", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--workspace", default=None, help="验收工作区（默认新建临时目录）")
    p.add_argument("--keep", action="store_true", help="保留临时工作区")
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--token-budget", type=int, default=None)
    p.add_argument("--context-tokens", type=int, default=None)
    args = p.parse_args(argv)
    return asyncio.run(run_acceptance(args))


if __name__ == "__main__":
    raise SystemExit(main())
