"""组装根（composition root）：唯一知道所有具体类的地方。

- Provider 经统一工厂按「显式参数 > TOML > 环境变量 > 默认」创建
- MCP 服务器按 TOML [mcp_servers.*] 启动，远程工具适配为本地 Tool 注册
- 工具、审批策略、上下文管理、会话录制在这里接线注入 AgentLoop
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .agent.compression import DEFAULT_MAX_TOKENS as DEFAULT_CONTEXT_BUDGET
from .agent.compression import ContextManager
from .agent.pricing import default_budget_for
from .agent.context import (
    CONTEXT_ENGINE_V3,
    ContextOptions,
    build_workspace_context_bundle,
)
from .agent.cyber_risk import cyber_risk_fragment
from .agent.loop import AgentLoop
from .agent.memory_contract import memory_contract_fragment
from .agent.context_assembler import ContextAssembler
from .agent.context_source import (
    DirectoryRulesContextSource,
    default_context_sources,
)
from .agent.fragments import (
    PromptFragmentRegistry,
    apply_prompt_snapshot,
    base_fragments,
    refresh_registry_snapshot,
    replace_base_fragments,
    skills_fragment,
    sync_rule_fragments,
)
from .agent.prompts import (
    prompt_authoring_enabled_from_config,
    prompt_calibration_paths_from_config,
    prompt_profile_from_config,
    prompt_release_id_from_config,
    resolve_prompt_profile,
)
from .agent.prompt_authoring import prompt_authoring_fragment
from .agent.prompt_release_pipeline import (
    PromptReleaseApprovalService,
    PromptReleasePipeline,
    PromptReleaseProposalStore,
)
from .agent.prompt_surface_catalog import refresh_prompt_surfaces
from .agent.rules import RuleLoader
from .agent.runtime import AgentRuntime
from .agent.usage import UsageTracker
from .agent.working_context import WorkingContextStore
from .benchmarks.parity import load_prompt_baseline
from .browser import BrowserOptions, BrowserRuntime
from .config import load_config_layers, load_managed_config
from .core.events import EventBus
from .core.errors import ConfigError
from .core.redact import SecretRedactor
from .enterprise.auth import (
    AuthError,
    OIDCClient,
    SSOConfig,
    load_sso_config,
)
from .enterprise.audit import AuditError, ComplianceAuditor
from .enterprise.policy import (
    EnterprisePolicy,
    EnterprisePolicyError,
    PolicyViolation,
    effective_permission_mode,
    load_enterprise_policy,
)
from .extensions import ExtensionRegistry, replace_runtime_extensions
from .hooks import HookRunner, parse_hooks
from .lsp.manager import LSPManager, LSPOptions
from .lsp.tools import build_lsp_tools
from .output_styles import OutputStyleConfig, OutputStyleManager
from .web.tools import WebOptions, WebRuntime
from .jobs import ShellJobManager
from .core.messages import Message, ToolCall
from .llm.base import LLMProvider, collect
from .llm.providers.mock import MockProvider
from .llm.registry import create_provider, resolve_settings
from .llm.retry import RetryPolicy, RetryingProvider
from .mcp.supervisor import MCPSupervisor
from .plugins import PluginManager
from .plugins.signing import PluginSignatureError
from .plugins.trust import PluginTrustError, PluginTrustPolicy, PluginTrustStore
from .safety.policy import (
    AllowAllPolicy,
    EnterpriseGuardPolicy,
    SandboxPolicy,
)
from .safety.sandbox import ExecutionSandbox, SandboxError
from .session.autocommit import AutoCommitter
from .session.checkpoint import CheckpointManager
from .session.compact_checkpoint import CompactCheckpointStore
from .session.context_checkpoint import (
    ContextCheckpointCoordinator,
    ContextCheckpointStore,
)
from .session.commands import (
    sync_custom_commands,
    sync_mcp_prompt_commands,
    sync_plugin_commands,
    sync_skill_commands,
)
from .session.commands.custom import CustomCommandManager
from .session.input import InputProcessor
from .session.elicitation_bridge import MCPElicitationBridge
from .session.interactions import UserInputBroker
from .session.memory import MemoryStore, sync_memory_fragment
from .session.memory_candidates import (
    DEFAULT_AUTO_THRESHOLD,
    DEFAULT_SUGGEST_THRESHOLD,
    AutoMemoryCoordinator,
    AutoMemoryMode,
    coerce_auto_memory_mode,
)
from .session.restore import (
    refresh_skill_system_prompt,
    restore_session as _restore_session,
)
from .session.runner import run_session
from .skills import BUILTIN_SKILL_ROOT, SkillManager, build_skill_tools
from .subagent.definition import AgentDefinitionManager
from .subagent.manager import (
    SubagentManager,
    build_subagent_system_prompt,
)
from .subagent.model_aliases import resolve_agent_model_spec
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
from .tools.builtin.database_migration import (
    MigrationOptions,
    build_migration_tools,
)
from .tools.builtin.edit_file import EditFileTool
from .tools.builtin.glob import GlobTool
from .tools.builtin.grep import GrepTool
from .tools.builtin.job_tools import (
    JobKillTool,
    JobListTool,
    JobOutputTool,
    JobStartTool,
)
from .tools.builtin.memory_search import MemorySearchTool
from .tools.builtin.notebook import NotebookOptions, build_notebook_tools
from .tools.builtin.prompt_lab import PromptLabTool
from .tools.builtin.read_file import ReadFileTool
from .tools.builtin.repo_map import RepoMapTool
from .tools.builtin.run_command import RunCommandTool
from .tools.builtin.find_refs import FindRefsTool
from .tools.builtin.time_tool import GetTimeTool
from .tools.builtin.tool_search import ToolSearchTool
from .tools.builtin.write_file import WriteFileTool
from .tools.extension.ask_user import AskUserTool
from .tools.extension.update_plan import UpdatePlanTool
from .tools.registry import ToolRegistry
from .ui.approval import ApprovalBroker
from .ui.repl import run_repl
from .ui.status_line import StatusLineOptions, StatusLineRenderer

# Backward-compatible public re-export used by embedders and older tests.
restore_session = _restore_session


def default_tools(
    memory_store: Optional[MemoryStore] = None,
    *,
    lsp_manager: Optional[LSPManager] = None,
    web_runtime: Optional[WebRuntime] = None,
    browser_runtime: Optional[BrowserRuntime] = None,
    notebook_options: Optional[NotebookOptions] = None,
    migration_options: Optional[MigrationOptions] = None,
) -> List[Tool]:
    """内置工具集（17 个 + 9 个子代理工具在 build_loop 注册）。
    MCP 远程工具在运行时追加注册；Memory、Web、Browser、Notebook 与数据库
    迁移能力只在对应运行时/配置显式传入时装配，保持基础工具集向后兼容。"""
    tools: List[Tool] = [
        GetTimeTool(), ReadFileTool(), WriteFileTool(), EditFileTool(),
        RunCommandTool(), GrepTool(), GlobTool(), RepoMapTool(),
        FindRefsTool(), CheckSyntaxTool(), AskUserTool(), UpdatePlanTool(),
        JobStartTool(), JobOutputTool(), JobListTool(), JobKillTool(),
        ToolSearchTool(),
    ]
    if memory_store is not None:
        tools.append(MemorySearchTool(memory_store))
    if lsp_manager is not None and lsp_manager.enabled:
        tools.extend(build_lsp_tools(lsp_manager))
    if web_runtime is not None and web_runtime.options.enabled:
        tools.extend(web_runtime.build_tools())
    if browser_runtime is not None and browser_runtime.options.enabled:
        from .browser.tools import build_browser_tools

        tools.extend(build_browser_tools(browser_runtime))
    if notebook_options is not None and notebook_options.enabled:
        tools.extend(build_notebook_tools(notebook_options))
    if migration_options is not None and migration_options.enabled:
        tools.extend(build_migration_tools(migration_options))
    return tools


def resolve_auto_memory_mode(
    args: argparse.Namespace,
    config: Dict[str, Any],
) -> AutoMemoryMode:
    """CLI > ``[memory].mode`` > conservative ``suggest`` default."""
    explicit = getattr(args, "memory_mode", None)
    if explicit:
        return coerce_auto_memory_mode(explicit)
    section = config.get("memory") or {}
    configured = section.get("mode") if isinstance(section, dict) else None
    return coerce_auto_memory_mode(configured or AutoMemoryMode.SUGGEST.value)


def resolve_auto_memory_thresholds(
    args: argparse.Namespace,
    config: Dict[str, Any],
) -> tuple[float, float]:
    section = config.get("memory") or {}
    if not isinstance(section, dict):
        section = {}
    suggest = getattr(args, "memory_suggest_threshold", None)
    auto = getattr(args, "memory_auto_threshold", None)
    try:
        suggest_value = float(
            suggest if suggest is not None else
            section.get("suggest_threshold", DEFAULT_SUGGEST_THRESHOLD))
        auto_value = float(
            auto if auto is not None else
            section.get("auto_threshold", DEFAULT_AUTO_THRESHOLD))
    except (TypeError, ValueError):
        suggest_value = DEFAULT_SUGGEST_THRESHOLD
        auto_value = DEFAULT_AUTO_THRESHOLD
    suggest_value = max(0.0, min(suggest_value, 1.0))
    auto_value = max(suggest_value, min(auto_value, 1.0))
    return suggest_value, auto_value


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
    config: Optional[Dict[str, Any]] = None,
    enterprise_policy: Optional[EnterprisePolicy] = None,
):
    """Build approval policy while preserving the managed security ceiling."""
    if enterprise_policy is None:
        # A direct embedder must not be able to convert project/user TOML
        # into enterprise authority.  The managed layer is the only source.
        managed = load_enterprise_policy(
            load_managed_config(workspace=workspace_root))
    else:
        managed = enterprise_policy
    managed.validate_cli(args)
    if args.no_sandbox:
        base = AllowAllPolicy()
        return EnterpriseGuardPolicy(base, managed) if managed.enabled else base
    ui_section = (config or {}).get("ui") or {}
    configured_review = (
        bool(ui_section.get("diff_review", True))
        if isinstance(ui_section, dict) else True
    )
    review_writes = bool(
        configured_review
        and not getattr(args, "no_diff_review", False)
        and not getattr(args, "auto_approve", False)
        and approver is not None
        and resolve_run_mode(args) is RunMode.REPL
    )
    base = SandboxPolicy(
        approver=approver,
        rules_path=workspace_root / ".krelcode" / "allow_rules.json",
        bus=bus,
        auto_approve=bool(
            getattr(args, "auto_approve", False)
            and (not managed.enabled or managed.allow_auto_approve)),
        workspace_root=workspace_root,
        review_writes=review_writes,
        managed_policy=managed if managed.enabled else None,
    )
    return EnterpriseGuardPolicy(base, managed) if managed.enabled else base


def resolve_permission_mode(
    args: argparse.Namespace,
    config: Dict[str, Any],
    enterprise_policy: Optional[EnterprisePolicy] = None,
) -> PermissionMode:
    """Resolve CLI/config mode and clamp it to the managed ceiling."""
    explicit = getattr(args, "permission_mode", None)
    if explicit:
        selected = PermissionMode(explicit)
    else:
        section = config.get("permissions") or {}
        configured = section.get("mode") if isinstance(section, dict) else None
        if configured:
            selected = PermissionMode(str(configured))
        elif getattr(args, "auto_approve", False):
            selected = PermissionMode.AUTO_APPROVE
        else:
            selected = PermissionMode.DEFAULT
    managed = enterprise_policy or load_enterprise_policy(config)
    return effective_permission_mode(selected, managed)

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


def configured_skill_paths(
    args: argparse.Namespace, config: Dict[str, Any],
) -> list[Any]:
    """Return explicit Skill roots ordered below plugin/CLI overrides."""
    skill_config = config.get("skills") or {}
    configured = skill_config.get("paths") or []
    if isinstance(configured, str):
        configured = [configured]
    cli_paths = getattr(args, "skills_dir", None) or []
    return list(configured) + list(cli_paths)


def build_skill_manager(
    args: argparse.Namespace,
    config: Dict[str, Any],
    *,
    plugin_paths: Optional[List[Any]] = None,
) -> SkillManager:
    """Build SkillManager with plugin roots before explicit CLI/project roots."""
    skill_config = config.get("skills") or {}
    enabled = (not getattr(args, "no_skills", False)
               and bool(skill_config.get("enabled", True)))
    explicit_paths = configured_skill_paths(args, config)
    return SkillManager(
        Path(args.workspace).resolve(),
        builtin_paths=(BUILTIN_SKILL_ROOT,),
        extra_paths=list(plugin_paths or ()) + explicit_paths,
        enabled=enabled,
    )


def build_plugin_manager(
    args: argparse.Namespace,
    config: Dict[str, Any],
    workspace_root: Path,
    *,
    enterprise_policy: Optional[EnterprisePolicy] = None,
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
        and not (enterprise_policy and enterprise_policy.disable_plugins)
    )
    trust_section = section.get("trust")
    if trust_section is not None and not isinstance(trust_section, dict):
        trust_section = None
    trust_policy = PluginTrustPolicy.from_config(
        section, trust_section=trust_section)
    trust_store_path = section.get("trust_store")
    if trust_store_path:
        configured_store = Path(str(trust_store_path)).expanduser()
        if not configured_store.is_absolute():
            configured_store = workspace_root / configured_store
    else:
        configured_store = None
    return PluginManager(
        workspace_root,
        extra_paths=list(configured) + list(cli_paths),
        enabled=enabled,
        trust_policy=trust_policy,
        trust_store_path=configured_store,
    )


def build_custom_command_manager(
    config: Dict[str, Any], workspace_root: Path,
) -> CustomCommandManager:
    section = config.get("commands") or {}
    if not isinstance(section, dict):
        section = {}
    configured = section.get("paths") or []
    if isinstance(configured, str):
        configured = [configured]
    return CustomCommandManager(workspace_root, extra_paths=configured)


def compose_mcp_config(
    config: Dict[str, Any],
    plugin_servers: Dict[str, Any],
) -> Dict[str, Any]:
    """Merge plugin MCP defaults with explicit layered configuration."""
    merged = dict(plugin_servers)
    configured = config.get("mcp_servers") or {}
    if isinstance(configured, dict):
        merged.update(configured)
    return merged


def compose_hook_specs(
    config: Dict[str, Any],
    plugin_hooks: tuple[Dict[str, Any], ...],
):
    """Merge layered config hooks with plugin-provided hook declarations."""
    raw_hooks = config.get("hooks") or []
    if not isinstance(raw_hooks, list):
        raw_hooks = []
    return parse_hooks({
        "hooks": [*raw_hooks, *plugin_hooks],
    })


def build_subagent_llm_resolver(
    args: argparse.Namespace,
    config: Dict[str, Any],
    parent: LLMProvider | Callable[[], LLMProvider],
    redactor: Optional[SecretRedactor] = None,
    enterprise_policy: Optional[EnterprisePolicy] = None,
) -> Callable[[str], LLMProvider]:
    """Resolve Definition.model without changing the parent's provider.

    Logical tiers (``sonnet``/``haiku``/``opus``) map to Anthropic only when the
    parent already uses Anthropic. Other providers inherit the parent unless the
    user supplies an explicit ``[agent_models]`` override.
    """
    def resolve(spec: str) -> LLMProvider:
        parent_provider = parent() if callable(parent) else parent
        raw = str(spec or "").strip()
        if not raw:
            return parent_provider
        parent_name = str(getattr(parent_provider, "name", "openai") or "openai")
        resolved = resolve_agent_model_spec(
            raw, parent_provider=parent_name, config=config)
        if resolved is None:
            return parent_provider
        provider_name, model_name = resolved
        if enterprise_policy is not None:
            enterprise_policy.validate_provider(provider_name)
        if provider_name == "mock":
            raise ValueError("mock provider 不支持 Agent Definition 独立模型")
        settings = resolve_settings(
            provider=provider_name,
            model=model_name,
            base_url=(args.base_url if provider_name == parent_name else None),
            config=config,
        )
        inner = create_provider(settings)
        if getattr(inner, "api_key", None) is None:
            raise ValueError(
                f"Agent Definition 模型 {provider_name}/{model_name} 缺少 API key")
        provider: LLMProvider = inner
        if args.retries > 0:
            provider = RetryingProvider(
                inner,
                RetryPolicy(max_retries=args.retries),
                on_retry=lambda attempt, delay, error: print(
                    f"[retry:subagent] LLM 调用失败"
                    f"（{type(error).__name__}），第 {attempt} 次重试，"
                    f"{delay:g}s 后…"),
            )
        if redactor is not None:
            redactor.add(getattr(inner, "api_key", None))
        return provider

    return resolve


async def build_loop(
    args: argparse.Namespace,
    config: Dict[str, Any],
    extra_tools: Optional[List[Tool]] = None,
    log=print,
    approver=None,
    user_input_port=None,
    enterprise_policy: Optional[EnterprisePolicy] = None,
    principal=None,
):
    """返回 (AgentLoop, SubagentManager)。

    子代理共享 provider/工作区/审批策略，但工具集由 default_tools() 单独构建
    （不含 spawn 系工具 → 防递归），历史与总线各自独立。
    """
    workspace_root = Path(args.workspace).resolve()
    if enterprise_policy is None:
        policy = load_enterprise_policy(
            load_managed_config(workspace=workspace_root))
    else:
        policy = enterprise_policy
    policy.validate_cli(args)
    if policy.require_sso and principal is None:
        raise PolicyViolation("企业策略要求 SSO，但当前没有有效身份")
    context_options = ContextOptions.from_config(config.get("context"))
    rule_loader = RuleLoader(
        workspace_root,
        limits=context_options.rule_limits(),
        imports_enabled=context_options.rules_imports,
    )
    workspace_context = None
    if not args.no_context:
        workspace_context = await build_workspace_context_bundle(
            workspace_root, options=context_options, rule_loader=rule_loader)
        if workspace_context.rule_bundle is not None:
            rule_bundle = workspace_context.rule_bundle
            if rule_bundle.content:
                log(
                    f"[rules] 已加载 {rule_bundle.entry_count} 个入口"
                    f" / {rule_bundle.import_count} 个导入"
                    f"（{rule_bundle.chars} chars）")
            for issue in rule_bundle.issues:
                log(
                    f"[rules] 跳过 {issue.code}: {issue.path}: "
                    f"{issue.message}")
    bus = EventBus()
    prompt_authoring_enabled = prompt_authoring_enabled_from_config(config)
    llm = build_llm(args, config, log=log)
    policy.validate_provider(str(getattr(llm, "name", "") or ""))
    prompt_profile_setting = prompt_profile_from_config(config)
    prompt_release_setting = prompt_release_id_from_config(config)
    prompt_calibrations = _load_prompt_calibrations(config, workspace_root)
    prompt_profile_resolution = resolve_prompt_profile(
        llm,
        getattr(llm, "model", ""),
        configured=prompt_profile_setting,
        release_id=prompt_release_setting,
        calibrations=prompt_calibrations,
    )
    memory_store = MemoryStore(workspace_root)
    base_skill_paths = configured_skill_paths(args, config)
    custom_commands = build_custom_command_manager(config, workspace_root)
    try:
        plugin_manager = build_plugin_manager(
            args, config, workspace_root, enterprise_policy=policy)
        await plugin_manager.load()
    except (PluginTrustError, PluginSignatureError) as exc:
        # Trust metadata is security-sensitive: a malformed store disables the
        # plugin plane for this process instead of silently loading untrusted code.
        empty_store = PluginTrustStore(
            workspace_root / ".krelcode" / ".plugin-trust-disabled.json",
            load=False,
        )
        plugin_manager = PluginManager(
            workspace_root, enabled=False, trust_store=empty_store)
        log(f"[plugins] 信任库无效，插件系统已 fail-closed 禁用: {exc}")
    plugin_tools = list(plugin_manager.tools)
    skill_manager = build_skill_manager(
        args, config, plugin_paths=list(plugin_manager.skill_paths))
    skill_tools = build_skill_tools(skill_manager) if skill_manager.enabled else []
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
            permission_mode=resolve_permission_mode(args, config, policy),
            features=_configured_features(config),
            disabled_tools=_disabled_tools(config),
            managed_tool_allowlist=(
                frozenset(policy.allowed_tools)
                if policy.allowed_tools else None),
            managed_tool_denylist=(
                frozenset(policy.disabled_tools)
                if policy.disabled_tools else None),
        ),
    )
    from .daemon.options import resolve_daemon_options

    daemon_options = resolve_daemon_options(args, config)
    daemon_owned = daemon_options.enabled and not bool(
        getattr(args, "daemon_worker", False))
    if daemon_owned:
        from .daemon.client import DaemonJobManager

        shell_jobs = DaemonJobManager(
            workspace_root,
            config_path=daemon_options.config_path,
            idle_timeout=daemon_options.idle_timeout,
            bus=bus,
        )
    else:
        shell_jobs = ShellJobManager(bus, workspace_root=workspace_root)
    tool_context.shell_jobs = shell_jobs
    approval_policy = build_policy(
        args, workspace_root, bus, approver=approver, config=config,
        enterprise_policy=policy)
    lsp_manager = LSPManager(
        workspace_root, options=LSPOptions.from_config(config.get("lsp")))
    web_options = WebOptions.from_config(
        config.get("web"), config.get("web_search"))
    if getattr(args, "no_web", False):
        web_options = WebOptions(
            enabled=False, search_enabled=False,
            max_bytes=web_options.max_bytes, timeout=web_options.timeout,
            max_redirects=web_options.max_redirects,
            max_chars=web_options.max_chars,
            allowed_ports=web_options.allowed_ports,
            allow_private=web_options.allow_private,
            search_provider=web_options.search_provider,
            search_api_key_env=web_options.search_api_key_env,
            search_endpoint=web_options.search_endpoint,
            search_max_results=web_options.search_max_results,
        )
    web_runtime = WebRuntime.from_options(web_options)
    browser_options = BrowserOptions.from_config(config.get("browser"))
    if getattr(args, "no_web", False) or getattr(args, "no_browser", False):
        browser_options = replace(browser_options, enabled=False)
    browser_runtime = BrowserRuntime.from_options(browser_options)
    notebook_options = NotebookOptions.from_config(config.get("notebook"))
    if getattr(args, "no_notebook", False):
        notebook_options = replace(notebook_options, enabled=False)
    migration_options = MigrationOptions.from_config(config.get("database"))
    if getattr(args, "no_database", False):
        migration_options = replace(migration_options, enabled=False)
    builtin_tools = default_tools(
        memory_store,
        lsp_manager=lsp_manager,
        web_runtime=web_runtime,
        browser_runtime=browser_runtime,
        notebook_options=notebook_options,
        migration_options=migration_options,
    )
    if prompt_authoring_enabled:
        builtin_tools.append(PromptLabTool())

    def child_tool_factory() -> List[Tool]:
        tools = (
            default_tools(
                memory_store,
                lsp_manager=lsp_manager,
                web_runtime=web_runtime,
                browser_runtime=browser_runtime,
                notebook_options=notebook_options,
                migration_options=migration_options,
            )
            + skill_tools
            + list(extra_tools or [])
            + list(plugin_manager.tools)
        )
        if prompt_authoring_enabled:
            tools.append(PromptLabTool())
        # MCP is registered asynchronously after loop construction; resolve it
        # lazily so spawned agents see the current connected server catalogue.
        for name in registry.names():
            descriptor = registry.descriptor(name)
            if descriptor is not None and descriptor.source is ToolSource.MCP:
                tool = registry.get(name)
                if tool is not None:
                    tools.append(tool)
        return tools
    registry = ToolRegistry(builtin_tools)
    for tool in skill_tools:
        registry.register(
            tool, ToolDescriptor.from_tool(tool, source=ToolSource.SKILL))
    for tool in extra_tools or []:
        try:
            registry.register(
                tool, ToolDescriptor.from_tool(tool, source=ToolSource.PLUGIN))
        except ValueError as exc:
            log(f"[plugins] 工具注册失败，已隔离: {getattr(tool, 'name', '?')}: {exc}")
    for tool in plugin_tools:
        try:
            registry.register(
                tool, ToolDescriptor.from_tool(tool, source=ToolSource.PLUGIN))
        except ValueError as exc:
            log(f"[plugins] 工具注册失败，已隔离: {tool.name}: {exc}")
    agent_definitions = AgentDefinitionManager(
        workspace_root, extra_paths=plugin_manager.agent_paths)
    if agent_definitions.list():
        log("[agents] 已加载 Agent Definition: "
            + ", ".join(item.name for item in agent_definitions.list()))
    for issue in agent_definitions.issues:
        log(f"[agents] 跳过无效定义: {issue.path}: {issue.message}")
    redactor = SecretRedactor()
    redactor.add(getattr(llm, "api_key", None))
    def subagent_base_prompt(child_llm: LLMProvider) -> str:
        return build_subagent_system_prompt(
            profile=prompt_profile_setting,
            provider=child_llm,
            model=getattr(child_llm, "model", ""),
        )

    def subagent_catalog_fragments():
        fragments = []
        catalog = skills_fragment(skill_manager.prompt_index())
        if catalog is not None:
            fragments.append(catalog)
        if prompt_authoring_enabled:
            fragments.append(prompt_authoring_fragment())
        return tuple(fragments)

    if daemon_owned:
        from .daemon.client import DaemonSubagentManager

        manager = DaemonSubagentManager(
            workspace_root,
            config_path=daemon_options.config_path,
            redactor=redactor,
            idle_timeout=daemon_options.idle_timeout,
        )
        manager.llm = llm
        manager.prompt_profile = prompt_profile_setting
        manager.prompt_release_id = prompt_release_setting
        manager.prompt_calibrations = tuple(prompt_calibrations)
    else:
        manager = SubagentManager(
            llm=llm,
            tool_factory=child_tool_factory,
            tool_context=tool_context,
            policy=approval_policy,
            bus=bus,
            max_steps=args.max_steps,
            context_budget=context_budget,   # 子代理长任务同样受压缩保护
            # Keep the legacy composite view; new assembly splits base (L1) and
            # the skill catalog (L6) into independent source-attributed fragments.
            system_prompt_provider=(
                lambda: skill_manager.apply_to_prompt(
                    subagent_base_prompt(manager.llm))),
            base_prompt_for_llm=subagent_base_prompt,
            prompt_profile=prompt_profile_setting,
            prompt_release_id=prompt_release_setting,
            prompt_calibrations=prompt_calibrations,
            catalog_fragments_provider=subagent_catalog_fragments,
            definitions=agent_definitions,
            memory_store=memory_store,
            llm_resolver=build_subagent_llm_resolver(
                args, config, lambda: manager.llm, redactor,
                enterprise_policy=policy),
            mcp_servers_provider=lambda: tuple(
                compose_mcp_config(config, dict(plugin_manager.mcp_servers))
            ),
        )
    for tool in manager.build_tools():
        registry.register(
            tool, ToolDescriptor.from_tool(tool, source=ToolSource.SUBAGENT))
    output_style_config = OutputStyleConfig.from_config(
        config.get("output_styles"),
        workspace=workspace_root,
        cli_paths=getattr(args, "output_style_dir", ()) or (),
        cli_active=getattr(args, "output_style", None),
    )
    if getattr(args, "no_output_styles", False):
        output_style_config = OutputStyleConfig(
            enabled=False, active="", paths=output_style_config.paths,
            home=output_style_config.home)
    output_style_manager = OutputStyleManager(
        workspace_root, config=output_style_config)
    prompt_registry = PromptFragmentRegistry(
        base_fragments(prompt_profile_resolution))
    prompt_registry.register(cyber_risk_fragment())
    if memory_store is not None:
        prompt_registry.register(memory_contract_fragment())
    if workspace_context is not None:
        sync_rule_fragments(prompt_registry, workspace_context)
    # v3 keeps Memory out of the system Prompt Registry. It is assembled as a
    # separate, untrusted knowledge layer at request time.
    if context_options.engine != CONTEXT_ENGINE_V3:
        memory_fragment = memory_store.fragment()
        if memory_fragment is not None:
            prompt_registry.register(memory_fragment)
    if prompt_authoring_enabled:
        prompt_registry.register(prompt_authoring_fragment())
    output_style_manager.apply_to_registry(prompt_registry)
    prompt_snapshot = prompt_registry.snapshot()
    # prompt_lab is a read-only execution tool and must inspect the same
    # per-agent Registry used by PromptAssembler. ToolContext is copied for
    # every tool call by AgentLoop, so assigning it here propagates naturally.
    tool_context.prompt_registry = prompt_registry
    prompt_release_pipeline = None
    prompt_release_approval_service = None
    if prompt_authoring_enabled:
        release_store = PromptReleaseProposalStore(workspace_root)
        prompt_release_pipeline = PromptReleasePipeline(
            lambda: prompt_registry,
            isolation_context=tool_context,
            store=release_store,
        )
        prompt_release_approval_service = PromptReleaseApprovalService(
            release_store)
        tool_context.prompt_release_pipeline = prompt_release_pipeline
    directory_rules_enabled = (
        not args.no_context
        and context_options.include_rules
        and context_options.include_directory_rules
    )
    working_context_store = WorkingContextStore(workspace_root)
    compact_checkpoint_store = (
        None if args.no_checkpoint else CompactCheckpointStore(workspace_root))
    context_checkpoint_store = (
        None if args.no_checkpoint else ContextCheckpointStore(workspace_root))
    context_sources = default_context_sources(
        rule_loader if directory_rules_enabled else None,
        working_context_store=working_context_store,
        engine=context_options.engine,
        memory_store=memory_store,
    )
    context_assembler = ContextAssembler(
        context_sources, engine=context_options.engine)
    directory_rule_source = next(
        (source for source in context_sources
         if isinstance(source, DirectoryRulesContextSource)),
        None,
    )
    loop = AgentLoop(
        llm=llm,
        registry=registry,
        tool_context=tool_context,
        policy=approval_policy,
        bus=bus,
        system_prompt=prompt_snapshot.text,
        max_steps=args.max_steps,
        repeat_failure_limit=args.repeat_failure_limit,
        context_manager=context_manager,
        token_budget=args.token_budget,
        context_assembler=context_assembler,
        user_input_port=user_input_port,
        working_context_store=working_context_store,
        compact_checkpoint_store=compact_checkpoint_store,
    )
    loop.enterprise_policy = policy
    loop.principal = principal
    loop.tool_context.enterprise_policy = policy
    loop.tool_context.principal = principal
    if getattr(loop, "context_manager", None) is not None:
        loop.context_manager.prompt_surface_registry = (
            loop.prompt_surface_registry)
    loop.redactor = redactor
    loop.prompt_release_pipeline = prompt_release_pipeline
    loop.prompt_release_approval_service = prompt_release_approval_service
    # fork_context 数据源：getter 形式，/resume 重绑定 loop.history 后依然正确
    manager.get_parent_history = lambda: loop.history
    # Subagent instruction inheritance compares rule/policy hashes from the
    # parent's latest real request; history remains intentionally separate.
    manager.cache_ledger_provider = lambda: loop.last_context_cache_ledger
    loop.skill_manager = skill_manager
    loop.plugin_manager = plugin_manager
    loop.custom_command_manager = custom_commands
    loop.agent_definitions = agent_definitions
    loop.base_skill_paths = tuple(base_skill_paths)
    loop.prompt_registry = prompt_registry
    loop.prompt_profile = prompt_profile_resolution.profile
    loop.prompt_profile_resolution = prompt_profile_resolution
    loop.prompt_profile_name = prompt_profile_resolution.name
    loop.prompt_profile_config = prompt_profile_setting
    loop.prompt_authoring_enabled = prompt_authoring_enabled
    loop.context_options = context_options
    loop.context_engine = context_options.engine
    loop.context_enabled = not args.no_context
    loop.workspace_context = workspace_context
    loop.rule_loader = rule_loader
    loop.directory_rule_source = directory_rule_source
    loop.memory_store = memory_store
    loop.lsp_manager = lsp_manager
    loop.output_style_manager = output_style_manager
    loop.web_runtime = web_runtime
    loop.browser_runtime = browser_runtime
    loop.notebook_options = notebook_options
    loop.migration_options = migration_options
    loop.status_line_renderer = StatusLineRenderer(
        StatusLineOptions.from_config(config.get("status_line")))
    loop.context_checkpoint_store = context_checkpoint_store
    loop.sync_context_attachments(include_mcp=False)
    if context_checkpoint_store is not None:
        context_coordinator = ContextCheckpointCoordinator(
            loop=loop,
            store=context_checkpoint_store,
            compact_store=compact_checkpoint_store,
            memory_store=memory_store,
            log=log,
        )
        context_coordinator.attach(bus)
        loop.context_checkpoint_coordinator = context_coordinator
    apply_prompt_snapshot(loop, prompt_snapshot)
    refresh_prompt_surfaces(loop)

    suggest_threshold, auto_threshold = resolve_auto_memory_thresholds(
        args, config)
    auto_memory = AutoMemoryCoordinator(
        loop,
        memory_store,
        mode=resolve_auto_memory_mode(args, config),
        suggest_threshold=suggest_threshold,
        auto_threshold=auto_threshold,
        on_memory_changed=lambda: sync_memory_fragment(loop, memory_store),
        log=log,
    )
    auto_memory.attach(bus)
    loop.auto_memory = auto_memory

    extension_registry = ExtensionRegistry()
    loop.extension_registry = extension_registry

    def refresh_extensions_basic():
        result = replace_runtime_extensions(
            extension_registry,
            plugins=plugin_manager,
            skills=skill_manager,
            agents=agent_definitions,
            tools=registry,
        )
        refresh_prompt_surfaces(loop)
        return result

    refresh_extensions_basic()

    async def reload_plugins():
        old_tool_names = {tool.name for tool in plugin_manager.tools}
        records = await plugin_manager.reload()
        for name in old_tool_names:
            registry.unregister(name)
        for plugin_tool in plugin_manager.tools:
            try:
                registry.register(
                    plugin_tool,
                    ToolDescriptor.from_tool(
                        plugin_tool, source=ToolSource.PLUGIN))
            except ValueError as exc:
                log(f"[plugins] 工具注册失败，已隔离: {plugin_tool.name}: {exc}")
        skill_manager.set_extra_paths(
            [*plugin_manager.skill_paths, *loop.base_skill_paths],
            reload=True,
        )
        agent_definitions.set_extra_paths(
            plugin_manager.agent_paths, reload=True)
        refresh_skill_system_prompt(loop)
        loop.sync_context_attachments(include_mcp=True)
        refresh_extensions_basic()
        return records

    loop.extension_refresh = refresh_extensions_basic
    loop.plugin_reload = reload_plugins
    return loop, manager


def build_model_switcher(args: argparse.Namespace, config: Dict[str, Any],
                         loop: AgentLoop, usage: UsageTracker, subagents=None,
                         redactor: Optional[SecretRedactor] = None,
                         enterprise_policy: Optional[EnterprisePolicy] = None):
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
            profile = getattr(loop, "prompt_profile_name", "") or "unknown"
            return (
                f"当前: {current_base()} / {model} · prompt {profile}"
                "（/model <名称> 或 <provider>/<名称> 切换）"
            )
        if "/" in new_spec:
            provider_name, model_name = new_spec.split("/", 1)
        else:
            provider_name, model_name = current_base(), new_spec
        if provider_name == "mock":
            return ("mock 模式请显式指定目标 provider，如: /model openai/gpt-4o-mini")
        try:
            if enterprise_policy is not None:
                enterprise_policy.validate_provider(provider_name)
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

        try:
            calibration_workspace = loop.tool_context.workspace_root
            calibration_records = _load_prompt_calibrations(
                config, calibration_workspace)
            profile_resolution = resolve_prompt_profile(
                inner,
                getattr(inner, "model", ""),
                configured=prompt_profile_from_config(config),
                release_id=prompt_release_id_from_config(config),
                calibrations=calibration_records,
            )
        except ConfigError as exc:
            return f"切换失败: {exc}"
        loop.llm = provider
        if subagents is not None:
            subagents.llm = provider   # 之后 spawn 的子代理用新模型
            subagents.prompt_profile = prompt_profile_from_config(config)
            subagents.prompt_release_id = prompt_release_id_from_config(config)
            subagents.prompt_calibrations = calibration_records
        loop.prompt_profile = profile_resolution.profile
        loop.prompt_profile_resolution = profile_resolution
        loop.prompt_profile_name = profile_resolution.name
        prompt_note = ""
        registry = getattr(loop, "prompt_registry", None)
        if registry is not None:
            replace_base_fragments(
                registry,
                profile_resolution,
                provider=inner,
                model=getattr(inner, "model", ""),
            )
            refresh_registry_snapshot(loop)
            prompt_note = f"，prompt {profile_resolution.name}"
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
        return (
            f"已切换: {provider.name} / {getattr(inner, 'model', '?')}"
            f"{budget_note}{prompt_note}"
        )

    return switch


def _load_prompt_calibrations(config: Dict[str, Any], workspace: Path):
    calibrations = []
    for raw_path in prompt_calibration_paths_from_config(config):
        source = Path(raw_path).expanduser()
        if not source.is_absolute():
            source = Path(workspace).expanduser().resolve() / source
        try:
            baseline = load_prompt_baseline(source)
        except ValueError as exc:
            raise ConfigError(f"Prompt calibration 加载失败: {source}: {exc}") from exc
        calibrations.append(baseline.calibration)
    return tuple(calibrations)


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
    config_path = Path(args.config).expanduser() if args.config else None
    try:
        config_bundle = load_config_layers(
            workspace=Path(args.workspace),
            explicit_path=config_path,
        )
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    if config_path is not None and not config_bundle.sources:
        print(f"配置错误: 指定的配置文件不存在: {config_path}", file=sys.stderr)
        return 2
    config = config_bundle.data
    current_config = config
    try:
        # ``--config`` is intentionally an exclusive user configuration.  It
        # must not become an enterprise-policy bypass, so managed policy is
        # loaded independently in that case.
        if config_path is not None:
            managed_config = load_managed_config(
                workspace=Path(args.workspace))
            enterprise_policy = load_enterprise_policy(managed_config)
        else:
            enterprise_policy = load_enterprise_policy(
                config, config_bundle=config_bundle)
        enterprise_policy.validate_cli(args)
        sso_client = None
        principal = None
        sso_config = load_sso_config(enterprise_policy)
        if sso_config.enabled:
            sso_client = OIDCClient(sso_config)
            status = sso_client.status()
            if enterprise_policy.require_sso:
                principal = await sso_client.require_authenticated()
            elif status.authenticated:
                principal = status.principal
    except (ConfigError, EnterprisePolicyError, PolicyViolation, AuthError) as exc:
        print(f"企业策略/SSO 错误: {exc}", file=sys.stderr)
        return 2
    run_mode = resolve_run_mode(args)
    editor_stream = getattr(args, "input_format", "text") == "stream-json"
    approval_broker = (
        ApprovalBroker()
        if (run_mode is RunMode.REPL or editor_stream)
        and not getattr(args, "no_sandbox", False)
        else None
    )
    input_broker = (
        UserInputBroker()
        if run_mode is RunMode.REPL or editor_stream
        else None
    )
    try:
        loop, subagents = await build_loop(
            args,
            config,
            log=note,
            approver=approval_broker.request if approval_broker is not None else None,
            user_input_port=input_broker,
            enterprise_policy=enterprise_policy,
            principal=principal,
        )
    except SandboxError as e:
        print(f"Sandbox 配置错误: {e}", file=sys.stderr)
        return 2
    except (EnterprisePolicyError, PolicyViolation, AuthError) as e:
        print(f"企业策略/SSO 错误: {e}", file=sys.stderr)
        return 2
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2
    custom_commands = loop.custom_command_manager
    redactor = getattr(loop, "redactor", None) or SecretRedactor()
    redactor.add(getattr(loop.llm, "api_key", None))
    loop.redactor = redactor
    if getattr(loop, "working_context_store", None) is not None:
        loop.working_context_store.redactor = redactor
    if getattr(loop, "context_manager", None) is not None:
        loop.context_manager.redactor = redactor
    if getattr(loop, "compact_checkpoint_store", None) is not None:
        loop.compact_checkpoint_store.redactor = redactor
    if getattr(loop, "memory_store", None) is not None:
        loop.memory_store.redactor = redactor
        sync_memory_fragment(loop, loop.memory_store)
    if getattr(loop, "auto_memory", None) is not None:
        loop.auto_memory.set_redactor(redactor)
    subagents.redactor = redactor
    subagents.sidechains.redactor = redactor
    if getattr(loop, "context_checkpoint_store", None) is not None:
        loop.context_checkpoint_store.redactor = redactor
    if getattr(loop, "context_checkpoint_coordinator", None) is not None:
        loop.context_checkpoint_coordinator.redactor = redactor
    if getattr(loop.tool_context, "shell_jobs", None) is not None:
        loop.tool_context.shell_jobs.redactor = redactor
    if sso_client is not None:
        for secret in sso_client.redaction_secrets():
            redactor.add(secret)
    auditor = None
    if enterprise_policy.audit and bool(enterprise_policy.audit.get("enabled", False)):
        try:
            auditor = ComplianceAuditor.from_config(
                enterprise_policy.audit,
                workspace=loop.tool_context.workspace_root,
                organization=enterprise_policy.organization,
                principal=(principal.subject if principal is not None else ""),
                policy_version=enterprise_policy.policy_version,
                redactor=redactor,
            )
            auditor.attach(loop.bus)
            loop.auditor = auditor
        except AuditError as exc:
            print(f"企业审计错误: {exc}", file=sys.stderr)
            return 2
    elicitor = (
        MCPElicitationBridge(input_broker, loop.bus.emit, redactor=redactor)
        if input_broker is not None else None
    )
    supervisor = MCPSupervisor(
        loop.registry, log=note,
        sampler=build_mcp_sampler(loop.llm),
        roots=build_mcp_roots(loop.tool_context.workspace_root),
        elicitor=elicitor,
        redactor=redactor,
        prompt_surface_registry=loop.prompt_surface_registry)
    loop.mcp_supervisor = supervisor
    def merged_mcp_config(source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        active = current_config if source is None else source
        return compose_mcp_config(
            active, dict(loop.plugin_manager.mcp_servers))

    if not args.no_mcp:
        await supervisor.sync(merged_mcp_config())
        loop.sync_context_attachments(include_mcp=True)

    async def reload_mcp():
        """REPL /mcp reload：重读分层配置 → diff 同步 → 刷新 Prompt。"""
        nonlocal current_config
        new_bundle = load_config_layers(
            workspace=Path(args.workspace),
            explicit_path=config_path,
        )
        current_config = new_bundle.data
        await supervisor.sync(merged_mcp_config(new_bundle.data))
        restarted = await supervisor.check_and_restart()
        await supervisor.refresh_prompts()
        loop.sync_context_attachments(include_mcp=True)
        refresh = getattr(loop, "extension_refresh", None)
        if refresh is not None:
            refresh()
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
        args, config, loop, usage, subagents=subagents, redactor=redactor,
        enterprise_policy=enterprise_policy)

    def merged_hook_specs(source: Optional[Dict[str, Any]] = None) -> list[Any]:
        active = current_config if source is None else source
        return compose_hook_specs(
            active, loop.plugin_manager.hook_definitions)

    hooks = None

    def ensure_hooks(specs: list[Any]) -> None:
        nonlocal hooks
        if hooks is None:
            hooks = HookRunner(
                loop.tool_context.workspace_root, specs, log=note,
                redactor=redactor,
                prompt_runner=run_prompt_hook,
                prompt_surface_registry=loop.prompt_surface_registry)
            hooks.attach(loop.bus)
        else:
            hooks.replace_specs(specs)
        loop.hook_port = hooks
        subagents.hook_port = hooks
        if specs:
            note(f"[hooks] 已加载 {len(specs)} 个钩子: "
                 + ", ".join(sorted({h.on for h in specs})))

    async def run_prompt_hook(
        prompt: str, _payload: Dict[str, Any],
    ) -> str:
        finished = await collect(loop.llm, [Message.user(prompt)])
        return finished.message.content or ""

    ensure_hooks(merged_hook_specs())

    async def collect_hook_context(prompt: str) -> tuple[str, ...]:
        if hooks is None:
            return ()
        return await hooks.collect_input_context(prompt)

    input_processor = InputProcessor(
        loop.tool_context.workspace_root,
        hook_context_provider=collect_hook_context,
    )
    loop.command_registry = input_processor.command_registry
    sync_custom_commands(input_processor.command_registry, custom_commands)
    sync_plugin_commands(input_processor.command_registry, loop.plugin_manager)
    sync_skill_commands(input_processor.command_registry, loop.skill_manager)
    sync_mcp_prompt_commands(input_processor.command_registry, supervisor)

    def refresh_extensions():
        result = replace_runtime_extensions(
            loop.extension_registry,
            plugins=loop.plugin_manager,
            skills=loop.skill_manager,
            agents=loop.agent_definitions,
            tools=loop.registry,
            commands=input_processor.command_registry,
            hooks=hooks,
            mcp_servers=merged_mcp_config(),
            mcp_supervisor=supervisor,
        )
        refresh_prompt_surfaces(
            loop, hooks=hooks, mcp_supervisor=supervisor)
        return result

    loop.extension_refresh = refresh_extensions
    async def refresh_mcp_prompt_commands() -> None:
        sync_mcp_prompt_commands(input_processor.command_registry, supervisor)
        refresh_extensions()

    supervisor.on_prompts_changed = refresh_mcp_prompt_commands
    refresh_extensions()

    def reload_skills():
        report = loop.skill_manager.reload()
        refresh_skill_system_prompt(loop)
        sync_skill_commands(input_processor.command_registry, loop.skill_manager)
        refresh_extensions()
        return report

    def reload_custom_commands():
        custom_commands.reload()
        sync_custom_commands(input_processor.command_registry, custom_commands)
        refresh_extensions()
        return custom_commands

    loop.custom_commands_reload = reload_custom_commands

    base_plugin_reload = loop.plugin_reload

    async def reload_plugins():
        records = await base_plugin_reload()
        ensure_hooks(merged_hook_specs())
        if not args.no_mcp:
            await supervisor.sync(merged_mcp_config())
            await supervisor.refresh_prompts()
        sync_plugin_commands(input_processor.command_registry, loop.plugin_manager)
        sync_mcp_prompt_commands(input_processor.command_registry, supervisor)
        refresh_extensions()
        return records

    loop.plugin_reload = reload_plugins
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
        if auditor is not None:
            auditor.detach()
        if approval_broker is not None:
            approval_broker.close()
        if input_broker is not None:
            input_broker.close()
        if getattr(loop, "lsp_manager", None) is not None:
            await loop.lsp_manager.close()
        await runtime.close()


def main(argv=None) -> int:
    """Backward-compatible delegator to the process entrypoint."""
    from .entrypoints.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
