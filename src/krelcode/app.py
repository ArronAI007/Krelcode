"""组装根（composition root）：唯一知道所有具体类的地方。

- Provider 经统一工厂按「显式参数 > TOML > 环境变量 > 默认」创建
- MCP 服务器按 TOML [mcp_servers.*] 启动，远程工具适配为本地 Tool 注册
- 工具、审批策略、上下文管理、会话录制在这里接线注入 AgentLoop
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .agent.compression import ContextManager
from .agent.context import build_workspace_context
from .agent.loop import AgentLoop
from .agent.prompts import build_system_prompt
from .agent.usage import UsageTracker
from .config import load_config
from .core.events import EventBus, PermissionDenied, ToolCallCompleted
from .hooks import HookRunner, parse_hooks
from .core.messages import Message, ToolCall
from .llm.base import LLMProvider
from .llm.providers.mock import MockProvider
from .llm.registry import create_provider, resolve_settings
from .llm.retry import RetryPolicy, RetryingProvider
from .mcp.supervisor import MCPSupervisor
from .safety.policy import AllowAllPolicy, SandboxPolicy
from .session.autocommit import AutoCommitter
from .session.checkpoint import CheckpointManager
from .session.recorder import (
    SessionRecorder, latest_session, load_history, new_session_path,
)
from .subagent.manager import SubagentManager
from .tools.base import Tool, ToolContext
from .tools.builtin.edit_file import EditFileTool
from .tools.builtin.glob import GlobTool
from .tools.builtin.grep import GrepTool
from .tools.builtin.read_file import ReadFileTool
from .tools.builtin.repo_map import RepoMapTool
from .tools.builtin.run_command import RunCommandTool
from .tools.builtin.time_tool import GetTimeTool
from .tools.builtin.write_file import WriteFileTool
from .tools.registry import ToolRegistry
from .ui.approval import make_console_approver
from .ui.repl import run_repl


def default_tools() -> List[Tool]:
    """内置工具集（8 个）。MCP 远程工具在运行时追加注册。"""
    return [
        GetTimeTool(), ReadFileTool(), WriteFileTool(), EditFileTool(),
        RunCommandTool(), GrepTool(), GlobTool(), RepoMapTool(),
    ]


def build_llm(args: argparse.Namespace, config: Dict[str, Any], log=print) -> LLMProvider:
    if args.provider == "mock" or args.mock:
        # 演示脚本：第一轮 LLM 决定调用 get_time，拿到结果后给出最终回复
        return MockProvider(
            scripted=[
                Message.assistant(
                    tool_calls=[ToolCall(id="call_demo_1", name="get_time", arguments={})]
                ),
                "（mock 演示）我刚通过 get_time 工具查到了当前时间——这就是完整的 "
                "tool-call 循环：LLM 请求调用 → 审批 → 执行 → 结果回填 → LLM 总结。",
            ],
            fallback="[mock] 脚本已用完，但历史与工具循环依然正常工作。",
            chunk_size=12,  # 模拟流式分片
        )
    settings = resolve_settings(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        config=config,
    )
    provider = create_provider(settings)
    if args.retries > 0:
        def on_retry(attempt: int, delay: float, error: BaseException) -> None:
            log(f"[retry] LLM 调用失败（{type(error).__name__}: {error}），"
                f"第 {attempt} 次重试，{delay:g}s 后…")
        provider = RetryingProvider(
            provider, RetryPolicy(max_retries=args.retries), on_retry=on_retry)
    return provider


def build_policy(args: argparse.Namespace, workspace_root: Path, bus: EventBus):
    """--no-sandbox 回退全放行；否则 SandboxPolicy（规则分级+持久化+交互审批）。"""
    if args.no_sandbox:
        return AllowAllPolicy()
    return SandboxPolicy(
        approver=make_console_approver(),
        rules_path=workspace_root / ".krelcode" / "allow_rules.json",
        bus=bus,
        auto_approve=args.auto_approve,
    )


async def build_loop(
    args: argparse.Namespace,
    config: Dict[str, Any],
    extra_tools: Optional[List[Tool]] = None,
    log=print,
):
    """返回 (AgentLoop, SubagentManager)。

    子代理共享 provider/工作区/审批策略，但工具集由 default_tools() 单独构建
    （不含 spawn 系工具 → 防递归），历史与总线各自独立。
    """
    workspace_root = Path(args.workspace).resolve()
    context = ""
    if not args.no_context:
        # 启动时一次性收集：目录树 + git 状态 + AGENTS.md（失败安全）
        context = await build_workspace_context(workspace_root)
    bus = EventBus()
    llm = build_llm(args, config, log=log)
    context_manager = None
    if not args.no_compress:
        context_manager = ContextManager(llm, max_tokens=args.context_tokens)
    tool_context = ToolContext(workspace_root=workspace_root)
    policy = build_policy(args, workspace_root, bus)
    registry = ToolRegistry(default_tools() + list(extra_tools or []))
    manager = SubagentManager(
        llm=llm,
        tool_factory=default_tools,
        tool_context=tool_context,
        policy=policy,
        bus=bus,
        max_steps=args.max_steps,
    )
    for tool in manager.build_tools():
        registry.register(tool)
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        tool_context=tool_context,
        policy=policy,
        bus=bus,
        system_prompt=build_system_prompt(context),
        max_steps=args.max_steps,
        repeat_failure_limit=args.repeat_failure_limit,
        context_manager=context_manager,
        token_budget=args.token_budget,
    )
    return loop, manager


def setup_session(args: argparse.Namespace, loop: AgentLoop, log=print):
    """恢复历史（--resume）并挂载 JSONL 录制器；返回会话文件路径或 None。"""
    sessions_dir = loop.tool_context.workspace_root / ".krelcode" / "sessions"

    if args.resume:
        if args.resume == "latest":
            path = latest_session(sessions_dir)
            if path is None:
                log(f"（{sessions_dir} 下没有可恢复的会话，从空白开始）")
            else:
                restore_session(loop, path, log=log)
        else:
            restore_session(loop, Path(args.resume), log=log)

    if args.no_session:
        return None
    session_path = new_session_path(sessions_dir)
    recorder = SessionRecorder(session_path, lambda: loop.history)
    recorder.attach(loop.bus)
    return session_path


def restore_session(loop: AgentLoop, path: Path, log=print) -> bool:
    """把会话历史装入 loop（REPL /resume 命令与 --resume 参数共用）。"""
    if not Path(path).is_file():
        log(f"（会话文件不存在: {path}）")
        return False
    history = load_history(Path(path))
    if not history:
        log(f"（会话 {path} 中没有历史快照）")
        return False
    if history[0].role != "system":
        history.insert(0, Message.system(build_system_prompt()))
    loop.history = history
    log(f"（已恢复会话 {path}，共 {len(history)} 条消息）")
    return True


class _RunSummary:
    """JSON 输出模式的事件收集器（旁路订阅）。"""

    def __init__(self) -> None:
        self.tools_used: List[str] = []
        self.denied = 0

    def on_event(self, event) -> None:
        if isinstance(event, ToolCallCompleted):
            self.tools_used.append(event.tool_call.name)
        elif isinstance(event, PermissionDenied):
            self.denied += 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="krelcode", description="Krelcode coding agent (统一 Provider + tool-calling 循环 + MCP)"
    )
    p.add_argument("--provider", default=None,
                   help="openai / anthropic / mock（默认: KRELCODE_PROVIDER 或配置文件 default_provider 或 openai）")
    p.add_argument("--mock", action="store_true", help="等价于 --provider mock（含演示脚本），无需 API key")
    p.add_argument("--model", default=None, help="模型名（默认: 配置文件 / KRELCODE_MODEL / provider 默认）")
    p.add_argument("--base-url", default=None, help="覆盖 provider 的 base_url（任意兼容端点）")
    p.add_argument("--config", default=None, help="TOML 配置文件路径（默认 krelcode.toml 或 ~/.krelcode/config.toml）")
    p.add_argument("--workspace", default=".", help="工作区根目录（默认当前目录）")
    p.add_argument("--max-steps", type=int, default=20, help="单轮任务的最大 LLM 迭代步数")
    p.add_argument("--repeat-failure-limit", type=int, default=3,
                   help="同一调用连续失败多少次判定卡死并中止（默认 3）")
    p.add_argument("--token-budget", type=int, default=None,
                   help="单轮任务的 token 预算，超限中止（默认不限）")
    p.add_argument("--retries", type=int, default=2,
                   help="LLM 瞬态失败（429/5xx/网络）自动重试次数，指数退避（默认 2，0 关闭）")
    p.add_argument("--no-context", action="store_true",
                   help="跳过启动时的工作区上下文注入（目录树/git/AGENTS.md）")
    p.add_argument("--auto-approve", action="store_true",
                   help="ASK 级操作自动批准（信任模式；DENY 级仍然拒绝）")
    p.add_argument("--no-sandbox", action="store_true",
                   help="关闭安全策略（AllowAll，仅建议在隔离环境中使用）")
    p.add_argument("--context-tokens", type=int, default=100_000,
                   help="上下文 token 预算，超限自动摘要压缩（默认 100000）")
    p.add_argument("--no-compress", action="store_true",
                   help="关闭上下文自动压缩")
    p.add_argument("--no-mcp", action="store_true",
                   help="不启动配置文件中的 MCP 服务器")
    p.add_argument("--resume", nargs="?", const="latest", default=None,
                   help="恢复会话：--resume 取最新，--resume <path> 取指定文件")
    p.add_argument("--no-session", action="store_true",
                   help="关闭会话 JSONL 录制（默认写入 <workspace>/.krelcode/sessions/）")
    p.add_argument("--no-checkpoint", action="store_true",
                   help="关闭文件检查点（/undo 回滚功能）")
    p.add_argument("--auto-commit", action="store_true",
                   help="每轮结束若有文件变更自动 git 提交（需工作区是 git 仓库）")
    p.add_argument("-m", "--message", default=None, help="单次提问模式：执行完直接退出")
    p.add_argument("--output-format", choices=("text", "json"), default="text",
                   help="配合 -m：json 输出结构化结果到 stdout（启动提示走 stderr），适合 CI/脚本")
    p.add_argument("--version", action="version", version=f"krelcode {__version__}")
    return p


async def async_main(args: argparse.Namespace) -> int:
    quiet = bool(args.message) and args.output_format == "json"
    note = (lambda msg: print(msg, file=sys.stderr)) if quiet else print
    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)
    loop, subagents = await build_loop(args, config, log=note)
    supervisor = MCPSupervisor(loop.registry, log=note)
    if not args.no_mcp:
        await supervisor.sync(config.get("mcp_servers") or {})

    async def reload_mcp():
        """REPL /mcp reload：重读配置 → diff 同步 → 重启已断开的服务器。"""
        new_config = load_config(config_path)
        await supervisor.sync(new_config.get("mcp_servers") or {})
        return await supervisor.check_and_restart()

    checkpoints = None
    if not args.no_checkpoint:
        checkpoints = CheckpointManager(loop.tool_context.workspace_root)
        checkpoints.attach(loop.bus)
    usage = UsageTracker()
    usage.attach(loop.bus)
    hook_specs = parse_hooks(config)
    if hook_specs:
        hooks = HookRunner(loop.tool_context.workspace_root, hook_specs, log=note)
        hooks.attach(loop.bus)
        note(f"[hooks] 已加载 {len(hook_specs)} 个钩子: "
             + ", ".join(sorted({h.on for h in hook_specs})))
    if args.auto_commit:
        committer = AutoCommitter(loop.tool_context.workspace_root)
        if await committer.is_git_repo():
            await committer.ensure_local_exclude()
            loop.bus.subscribe(committer.on_event)
            note("[git] 自动提交已开启（每轮有变更时 commit；.krelcode/ 已本地排除）")
        else:
            note("[git] 工作区不是 git 仓库，--auto-commit 未生效")
    try:
        session_path = setup_session(args, loop, log=note)
        sessions_dir = loop.tool_context.workspace_root / ".krelcode" / "sessions"
        model = getattr(loop.llm, "model", "?")
        if args.message:
            if args.output_format == "json":
                summary = _RunSummary()
                loop.bus.subscribe(summary.on_event)
                try:
                    reply = await loop.run(args.message)
                    payload = {
                        "ok": True,
                        "reply": reply,
                        "usage": {
                            "calls": usage.session.calls,
                            "input": usage.session.input,
                            "output": usage.session.output,
                            "total": usage.session.total,
                        },
                        "tools_used": summary.tools_used,
                        "denied": summary.denied,
                        "session": str(session_path) if session_path else None,
                    }
                    code = 0
                except Exception as e:
                    payload = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    code = 1
                print(json.dumps(payload, ensure_ascii=False))
                return code
            try:
                print(await loop.run(args.message))
            except Exception as e:
                print(f"错误：{type(e).__name__}: {e}")
                return 1
            if usage.session.calls:
                print(f"[usage] {usage.session.text()}")
            return 0
        print(f"[provider: {loop.llm.name} | model: {model}]")
        return await run_repl(
            loop,
            tool_names=loop.registry.names(),
            session_path=str(session_path) if session_path else None,
            sessions_dir=sessions_dir,
            mcp_supervisor=supervisor,
            mcp_reload=reload_mcp,
            subagents=subagents,
            checkpoints=checkpoints,
            usage=usage,
        )
    finally:
        await subagents.shutdown()
        await supervisor.close_all()


def main(argv=None) -> int:
    return asyncio.run(async_main(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
