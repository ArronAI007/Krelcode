"""组装根（composition root）：唯一知道所有具体类的地方。

- Provider 经统一工厂按「显式参数 > TOML > 环境变量 > 默认」创建
- MCP 服务器按 TOML [mcp_servers.*] 启动，远程工具适配为本地 Tool 注册
- 工具、审批策略、上下文管理、会话录制在这里接线注入 AgentLoop
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .agent.compression import DEFAULT_MAX_TOKENS as DEFAULT_CONTEXT_BUDGET
from .agent.compression import ContextManager
from .agent.pricing import default_budget_for
from .agent.context import (
    ContextOptions,
    build_workspace_context_bundle,
)
from .agent.loop import DEFAULT_MAX_STEPS, AgentLoop
from .agent.prompts import build_system_prompt
from .agent.runtime import AgentRuntime
from .agent.usage import UsageTracker
from .config import load_config
from .core.events import EventBus
from .core.redact import SecretRedactor
from .hooks import HookRunner, parse_hooks
from .core.messages import Message, ToolCall
from .llm.base import LLMProvider
from .llm.providers.mock import MockProvider
from .llm.registry import create_provider, resolve_settings
from .llm.retry import RetryPolicy, RetryingProvider
from .mcp.supervisor import MCPSupervisor
from .plugins import PluginManager
from .safety.policy import AllowAllPolicy, SandboxPolicy
from .safety.sandbox import ExecutionSandbox, SandboxError
from .session.autocommit import AutoCommitter
from .session.checkpoint import CheckpointManager
from .session.input import InputProcessor
from .session.interactions import UserInputBroker
from .session.restore import refresh_skill_system_prompt, restore_session
from .session.runner import run_session
from .skills import SkillManager, build_skill_tools
from .subagent.manager import SUBAGENT_SYSTEM_PROMPT, SubagentManager
from .tools.base import (
    PermissionMode,
    RunMode,
    RuntimeSnapshot,
    Tool,
    ToolContext,
    ToolDescriptor,
    ToolSource,
)
from .tools.builtin.check_syntax import CheckSyntaxTool
from .tools.builtin.edit_file import EditFileTool
from .tools.builtin.glob import GlobTool
from .tools.builtin.grep import GrepTool
from .tools.builtin.read_file import ReadFileTool
from .tools.builtin.repo_map import RepoMapTool
from .tools.builtin.run_command import RunCommandTool
from .tools.builtin.find_refs import FindRefsTool
from .tools.builtin.time_tool import GetTimeTool
from .tools.builtin.write_file import WriteFileTool
from .tools.extension.ask_user import AskUserTool
from .tools.extension.update_plan import UpdatePlanTool
from .tools.registry import ToolRegistry
from .ui.approval import ApprovalBroker
from .ui.repl import run_repl


def default_tools() -> List[Tool]:
    """内置工具集（12 个 + 4 个子代理工具在 build_loop 注册）。
    MCP 远程工具在运行时追加注册。"""
    return [
        GetTimeTool(), ReadFileTool(), WriteFileTool(), EditFileTool(),
        RunCommandTool(), GrepTool(), GlobTool(), RepoMapTool(),
        FindRefsTool(), CheckSyntaxTool(), AskUserTool(), UpdatePlanTool(),
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


def build_mcp_sampler(llm: LLMProvider):
    """MCP sampling 回调：服务器请求 LLM 补全时，用当前 provider 完成。

    契约：async (messages, params) -> {"text": str, "model": str}
    messages 为 MCP 格式 [{"role","content":{"type":"text","text"}}]。
    """
    from .core.messages import Message as Msg
    from .llm.base import collect

    model_name = getattr(llm, "model", "krelcode")

    async def sampler(messages, params):
        msgs = []
        system_prompt = (params or {}).get("systemPrompt")
        if system_prompt:
            msgs.append(Msg.system(str(system_prompt)))
        for m in messages or []:
            content = m.get("content") or {}
            text = content.get("text", "") if isinstance(content, dict) else str(content)
            if m.get("role") == "assistant":
                msgs.append(Msg.assistant(text))
            else:
                msgs.append(Msg.user(text))
        if not msgs:
            msgs.append(Msg.user("(空的 sampling 请求)"))
        finished = await collect(llm, msgs)
        return {"text": finished.message.content or "", "model": model_name}

    return sampler


def build_mcp_roots(workspace_root: Path) -> List[Dict[str, Any]]:
    return [{"uri": workspace_root.as_uri(), "name": workspace_root.name}]


def build_policy(
    args: argparse.Namespace,
    workspace_root: Path,
    bus: EventBus,
    *,
    approver=None,
):
    """--no-sandbox 回退全放行；否则 SandboxPolicy（规则分级+持久化+交互审批）。"""
    if args.no_sandbox:
        return AllowAllPolicy()
    return SandboxPolicy(
        approver=approver,
        rules_path=workspace_root / ".krelcode" / "allow_rules.json",
        bus=bus,
        auto_approve=args.auto_approve,
    )


def resolve_permission_mode(
    args: argparse.Namespace, config: Dict[str, Any],
) -> PermissionMode:
    """CLI > [permissions].mode > --auto-approve > default。"""
    explicit = getattr(args, "permission_mode", None)
    if explicit:
        return PermissionMode(explicit)
    section = config.get("permissions") or {}
    configured = section.get("mode") if isinstance(section, dict) else None
    if configured:
        return PermissionMode(str(configured))
    if getattr(args, "auto_approve", False):
        return PermissionMode.AUTO_APPROVE
    return PermissionMode.DEFAULT


def resolve_run_mode(args: argparse.Namespace) -> RunMode:
    if getattr(args, "input_format", "text") == "stream-json":
        return RunMode.JSON
    if getattr(args, "message", None):
        if getattr(args, "output_format", "text") in {"json", "stream-json"}:
            return RunMode.JSON
        return RunMode.PRINT
    return RunMode.REPL


def _configured_features(config: Dict[str, Any]) -> frozenset[str]:
    raw = config.get("features") or {}
    if isinstance(raw, dict):
        enabled = raw.get("enabled", raw)
        if isinstance(enabled, dict):
            return frozenset(str(k) for k, v in enabled.items() if bool(v))
        raw = enabled
    if isinstance(raw, str):
        raw = [raw]
    return frozenset(str(item) for item in raw or ())


def _disabled_tools(config: Dict[str, Any]) -> frozenset[str]:
    section = config.get("tools") or {}
    raw = section.get("disabled", ()) if isinstance(section, dict) else ()
    if isinstance(raw, str):
        raw = [raw]
    return frozenset(str(item) for item in raw or ())


def build_execution_sandbox(
    args: argparse.Namespace, config: Dict[str, Any], workspace_root: Path,
) -> Optional[ExecutionSandbox]:
    """显式 CLI > TOML [sandbox] > auto；--no-sandbox 同时关闭运行时隔离。"""
    if getattr(args, "no_sandbox", False):
        return None
    section = config.get("sandbox") or {}
    if not isinstance(section, dict):
        section = {}
    backend = getattr(args, "sandbox_backend", None) or section.get("backend", "auto")
    network_arg = getattr(args, "sandbox_network", None)
    network = bool(section.get("network", False)) if network_arg is None else bool(network_arg)
    return ExecutionSandbox(
        workspace_root, backend=str(backend), network=network)


def build_skill_manager(args: argparse.Namespace, config: Dict[str, Any]) -> SkillManager:
    """按配置和 CLI 构建 SkillManager；CLI 路径最后扫描、同名覆盖配置路径。"""
    skill_config = config.get("skills") or {}
    enabled = (not getattr(args, "no_skills", False)
               and bool(skill_config.get("enabled", True)))
    configured = skill_config.get("paths") or []
    if isinstance(configured, str):
        configured = [configured]
    cli_paths = getattr(args, "skills_dir", None) or []
    return SkillManager(
        Path(args.workspace).resolve(),
        extra_paths=list(configured) + list(cli_paths),
        enabled=enabled,
    )


def build_plugin_manager(
    args: argparse.Namespace,
    config: Dict[str, Any],
    workspace_root: Path,
) -> PluginManager:
    """按配置/CLI 构建目录插件管理器；插件代码仅从显式目录加载。"""
    section = config.get("plugins") or {}
    if not isinstance(section, dict):
        section = {}
    configured = section.get("paths") or []
    if isinstance(configured, str):
        configured = [configured]
    cli_paths = getattr(args, "plugins_dir", None) or []
    enabled = (
        not getattr(args, "no_plugins", False)
        and bool(section.get("enabled", True))
    )
    return PluginManager(
        workspace_root,
        extra_paths=list(configured) + list(cli_paths),
        enabled=enabled,
    )


async def build_loop(
    args: argparse.Namespace,
    config: Dict[str, Any],
    extra_tools: Optional[List[Tool]] = None,
    log=print,
    approver=None,
    user_input_port=None,
):
    """返回 (AgentLoop, SubagentManager)。

    子代理共享 provider/工作区/审批策略，但工具集由 default_tools() 单独构建
    （不含 spawn 系工具 → 防递归），历史与总线各自独立。
    """
    workspace_root = Path(args.workspace).resolve()
    context_options = ContextOptions.from_config(config.get("context"))
    workspace_context = None
    context = ""
    if not args.no_context:
        workspace_context = await build_workspace_context_bundle(
            workspace_root, options=context_options)
        context = workspace_context.text
    bus = EventBus()
    llm = build_llm(args, config, log=log)
    skill_manager = build_skill_manager(args, config)
    skill_tools = build_skill_tools(skill_manager) if skill_manager.enabled else []
    plugin_manager = build_plugin_manager(args, config, workspace_root)
    await plugin_manager.load()
    plugin_tools = list(plugin_manager.tools)
    if skill_manager.enabled:
        if skill_manager.list():
            log("[skills] 已加载 "
                + ", ".join(s.name for s in skill_manager.list())
                + f"（{len(skill_manager.list())} 个）")
        for issue in skill_manager.issues:
            log(f"[skills] 跳过无效 skill: {issue.path}: {issue.message}")
    if plugin_manager.enabled:
        if plugin_manager.records():
            log("[plugins] 已发现 "
                + ", ".join(
                    f"{record.name}@{record.version}[{record.status}]"
                    for record in plugin_manager.records())
                + f"（{len(plugin_tools)} 个工具）")
        for issue in plugin_manager.issues:
            log(f"[plugins] 跳过问题: {issue.path}: {issue.message}")
    context_manager = None
    context_budget = None
    if not args.no_compress:
        # 预算优先级：显式 --context-tokens > 模型目录推导（窗口×75%）> 兜底默认
        context_budget = (args.context_tokens
                          or default_budget_for(getattr(llm, "model", None))
                          or DEFAULT_CONTEXT_BUDGET)
        context_manager = ContextManager(llm, max_tokens=context_budget)
    sandbox = build_execution_sandbox(args, config, workspace_root)
    if sandbox is not None:
        log(f"[sandbox] backend={sandbox.backend} "
            f"network={'on' if sandbox.network else 'off'}")
        if sandbox.backend == "static":
            log("[sandbox] 警告：OS 隔离后端不可用，当前为静态护栏模式"
                "（无法抵御解释器中的动态绕过）")
    tool_context = ToolContext(
        workspace_root=workspace_root,
        sandbox=sandbox,
        sandbox_enabled=sandbox is not None,
        runtime=RuntimeSnapshot(
            run_mode=resolve_run_mode(args),
            permission_mode=resolve_permission_mode(args, config),
            features=_configured_features(config),
            disabled_tools=_disabled_tools(config),
        ),
    )
    policy = build_policy(args, workspace_root, bus, approver=approver)
    tool_factory = lambda: default_tools() + skill_tools + list(plugin_manager.tools)
    registry = ToolRegistry(default_tools())
    for tool in skill_tools:
        registry.register(
            tool, ToolDescriptor.from_tool(tool, source=ToolSource.SKILL))
    for tool in extra_tools or []:
        registry.register(
            tool, ToolDescriptor.from_tool(tool, source=ToolSource.PLUGIN))
    for tool in plugin_tools:
        registry.register(
            tool, ToolDescriptor.from_tool(tool, source=ToolSource.PLUGIN))
    manager = SubagentManager(
        llm=llm,
        tool_factory=tool_factory,
        tool_context=tool_context,
        policy=policy,
        bus=bus,
        max_steps=args.max_steps,
        context_budget=context_budget,   # 子代理长任务同样受压缩保护
        system_prompt_provider=(
            lambda: skill_manager.apply_to_prompt(SUBAGENT_SYSTEM_PROMPT)),
    )
    for tool in manager.build_tools():
        registry.register(
            tool, ToolDescriptor.from_tool(tool, source=ToolSource.SUBAGENT))
    skill_base_prompt = build_system_prompt(context)
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        tool_context=tool_context,
        policy=policy,
        bus=bus,
        system_prompt=skill_manager.apply_to_prompt(skill_base_prompt),
        max_steps=args.max_steps,
        repeat_failure_limit=args.repeat_failure_limit,
        context_manager=context_manager,
        token_budget=args.token_budget,
        user_input_port=user_input_port,
    )
    # fork_context 数据源：getter 形式，/resume 重绑定 loop.history 后依然正确
    manager.get_parent_history = lambda: loop.history
    loop.skill_manager = skill_manager
    loop.plugin_manager = plugin_manager
    loop.skill_base_prompt = skill_base_prompt
    loop.context_options = context_options
    loop.workspace_context = workspace_context

    async def reload_plugins():
        records = await plugin_manager.reload()
        registry.unregister_prefix("plugin__")
        for plugin_tool in plugin_manager.tools:
            registry.register(
                plugin_tool,
                ToolDescriptor.from_tool(
                    plugin_tool, source=ToolSource.PLUGIN))
        return records

    loop.plugin_reload = reload_plugins
    return loop, manager


def build_model_switcher(args: argparse.Namespace, config: Dict[str, Any],
                         loop: AgentLoop, usage: UsageTracker, subagents=None,
                         redactor: Optional[SecretRedactor] = None):
    """/model 热切换：返回 callable(new_spec|None) -> 结果描述文本。

    new_spec 支持 "model"（沿用当前 provider）或 "provider/model"。
    切换动作：重建 provider（含重试包装）→ 替换 loop.llm 与压缩器 llm →
    同步 usage.model（成本核算）→ 预算未显式指定时按新模型窗口自适应。
    """
    def current_base() -> str:
        return getattr(loop.llm, "name", "?")

    def switch(new_spec: Optional[str]) -> str:
        if not new_spec:
            model = getattr(loop.llm, "model", "?")
            return f"当前: {current_base()} / {model}（/model <名称> 或 <provider>/<名称> 切换）"
        if "/" in new_spec:
            provider_name, model_name = new_spec.split("/", 1)
        else:
            provider_name, model_name = current_base(), new_spec
        if provider_name == "mock":
            return ("mock 模式请显式指定目标 provider，如: /model openai/gpt-4o-mini")
        try:
            settings = resolve_settings(
                provider=provider_name, model=model_name,
                base_url=args.base_url, config=config)
            inner = create_provider(settings)
            if getattr(inner, "api_key", None) is None:
                return f"切换失败: {provider_name} 缺少 API key（设置对应环境变量后重试）"
            provider: LLMProvider = inner
            if args.retries > 0:
                provider = RetryingProvider(
                    inner, RetryPolicy(max_retries=args.retries),
                    on_retry=lambda a, d, e: print(
                        f"[retry] LLM 调用失败（{type(e).__name__}），第 {a} 次重试，{d:g}s 后…"))
        except Exception as e:
            return f"切换失败: {type(e).__name__}: {e}"

        loop.llm = provider
        if subagents is not None:
            subagents.llm = provider   # 之后 spawn 的子代理用新模型
        budget_note = ""
        if loop.context_manager is not None:
            loop.context_manager.llm = provider
            if args.context_tokens is None:
                new_budget = (default_budget_for(getattr(inner, "model", None))
                              or DEFAULT_CONTEXT_BUDGET)
                loop.context_manager.max_tokens = new_budget
                budget_note = f"，压缩预算 {new_budget}"
        usage.model = getattr(inner, "model", None)
        # 新 provider 的 key 也纳入脱敏集合（旧 key 保留，避免历史旧内容漏网）
        if redactor is not None:
            redactor.add(getattr(inner, "api_key", None))
        return f"已切换: {provider.name} / {getattr(inner, 'model', '?')}{budget_note}"

    return switch


def build_parser() -> argparse.ArgumentParser:
    """Backward-compatible import for tests and embedders.

    CLI ownership now lives in :mod:`krelcode.entrypoints.main`.
    """
    from .entrypoints.main import build_parser as build_full_parser

    return build_full_parser()


async def async_main(args: argparse.Namespace) -> int:
    input_format = getattr(args, "input_format", "text")
    if input_format == "stream-json":
        if args.message is not None:
            print("--input-format stream-json 不能与 -m/--message 同时使用",
                  file=sys.stderr)
            return 2
        if args.output_format != "stream-json":
            print("--input-format stream-json 需要 --output-format stream-json",
                  file=sys.stderr)
            return 2
    if (
        args.output_format == "stream-json"
        and args.message is None
        and input_format != "stream-json"
    ):
        print("--output-format stream-json 需要 -m 或 --input-format stream-json",
              file=sys.stderr)
        return 2

    quiet = (
        args.message is not None
        and args.output_format in {"json", "stream-json"}
    ) or input_format == "stream-json"
    note = (lambda msg: print(msg, file=sys.stderr)) if quiet else print
    config_path = Path(args.config) if args.config else None
    config = load_config(config_path)
    approval_broker = (
        ApprovalBroker()
        if resolve_run_mode(args) is RunMode.REPL and not getattr(args, "no_sandbox", False)
        else None
    )
    input_broker = (
        UserInputBroker()
        if resolve_run_mode(args) is RunMode.REPL
        else None
    )
    try:
        loop, subagents = await build_loop(
            args,
            config,
            log=note,
            approver=approval_broker.request if approval_broker is not None else None,
            user_input_port=input_broker,
        )
    except SandboxError as e:
        print(f"Sandbox 配置错误: {e}", file=sys.stderr)
        return 2
    redactor = SecretRedactor()
    redactor.add(getattr(loop.llm, "api_key", None))
    loop.redactor = redactor
    subagents.redactor = redactor
    supervisor = MCPSupervisor(
        loop.registry, log=note,
        sampler=build_mcp_sampler(loop.llm),
        roots=build_mcp_roots(loop.tool_context.workspace_root))
    if not args.no_mcp:
        await supervisor.sync(config.get("mcp_servers") or {})

    async def reload_mcp():
        """REPL /mcp reload：重读配置 → diff 同步 → 重启已断开的服务器。"""
        new_config = load_config(config_path)
        await supervisor.sync(new_config.get("mcp_servers") or {})
        restarted = await supervisor.check_and_restart()
        await supervisor.refresh_prompts()
        return restarted

    runtime = AgentRuntime(
        loop,
        subagents=subagents,
        mcp_supervisor=supervisor,
        mcp_reload=reload_mcp,
    )
    checkpoints = None
    if not args.no_checkpoint:
        checkpoints = CheckpointManager(loop.tool_context.workspace_root)
        checkpoints.attach(loop.bus)
    usage = UsageTracker(model=getattr(loop.llm, "model", None))
    usage.attach(loop.bus)
    model_switch = build_model_switcher(
        args, config, loop, usage, subagents=subagents, redactor=redactor)

    def reload_skills():
        report = loop.skill_manager.reload()
        refresh_skill_system_prompt(loop)
        return report
    hook_specs = parse_hooks(config)
    hooks = None
    if hook_specs:
        hooks = HookRunner(
            loop.tool_context.workspace_root, hook_specs, log=note,
            redactor=redactor)
        hooks.attach(loop.bus)
        loop.hook_port = hooks
        subagents.hook_port = hooks
        note(f"[hooks] 已加载 {len(hook_specs)} 个钩子: "
             + ", ".join(sorted({h.on for h in hook_specs})))
    input_processor = InputProcessor(
        loop.tool_context.workspace_root,
        hook_context_provider=(
            hooks.collect_input_context if hooks is not None else None
        ),
    )
    if args.auto_commit:
        committer = AutoCommitter(loop.tool_context.workspace_root)
        if await committer.is_git_repo():
            await committer.ensure_local_exclude()
            loop.bus.subscribe(committer.on_event)
            note("[git] 自动提交已开启（每轮有变更时 commit；.krelcode/ 已本地排除）")
        else:
            note("[git] 工作区不是 git 仓库，--auto-commit 未生效")
    try:
        return await run_session(
            args,
            runtime,
            supervisor=supervisor,
            checkpoints=checkpoints,
            usage=usage,
            model_switch=model_switch,
            skills_reload=reload_skills,
            redactor=redactor,
            input_processor=input_processor,
            log=note,
            repl_runner=run_repl,
            approval_broker=approval_broker,
            user_input_broker=input_broker,
            hooks=hooks,
        )
    finally:
        if approval_broker is not None:
            approval_broker.close()
        if input_broker is not None:
            input_broker.close()
        await runtime.close()


def main(argv=None) -> int:
    """Backward-compatible delegator to the process entrypoint."""
    from .entrypoints.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
