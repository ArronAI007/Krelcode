"""组装根（composition root）：唯一知道所有具体类的地方。

- Provider 经统一工厂按「显式参数 > TOML > 环境变量 > 默认」创建
- MCP 服务器按 TOML [mcp_servers.*] 启动，远程工具适配为本地 Tool 注册
- 工具、审批策略、上下文管理、会话录制在这里接线注入 AgentLoop
"""

from __future__ import annotations

import argparse
import importlib
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ...agent.compression import DEFAULT_MAX_TOKENS as DEFAULT_CONTEXT_BUDGET
from ...agent.compression import ContextManager
from ...agent.pricing import default_budget_for
from ...agent.context import (
    CONTEXT_ENGINE_V3,
    ContextOptions,
    build_workspace_context_bundle,
)
from ...agent.cyber_risk import cyber_risk_fragment
from ...agent.loop import AgentLoop
from ...agent.memory_contract import memory_contract_fragment
from ...agent.context_assembler import ContextAssembler
from ...agent.context_source import (
    DirectoryRulesContextSource,
    default_context_sources,
)
from ...agent.fragments import (
    PromptFragmentRegistry,
    apply_prompt_snapshot,
    base_fragments,
    refresh_registry_snapshot,
    replace_base_fragments,
    skills_fragment,
    sync_rule_fragments,
)
from ...agent.prompts import (
    prompt_authoring_enabled_from_config,
    prompt_calibration_paths_from_config,
    prompt_profile_from_config,
    prompt_release_id_from_config,
    resolve_prompt_profile,
)
from ...agent.prompt_authoring import prompt_authoring_fragment
from ...agent.prompt_release_pipeline import (
    PromptReleaseApprovalService,
    PromptReleasePipeline,
    PromptReleaseProposalStore,
)
from ...agent.prompt_surface_catalog import refresh_prompt_surfaces
from ...agent.rules import RuleLoader
from ...agent.runtime import AgentRuntime
from ...agent.usage import UsageTracker
from ...agent.working_context import WorkingContextStore
from ...agent.prompt_parity import load_prompt_baseline
from ...browser import BrowserOptions, BrowserRuntime
from ...config import load_config_layers, load_managed_config
from ...core.events import EventBus
from ...core.errors import ConfigError
from ...core.redact import SecretRedactor
from ...enterprise.auth import (
    AuthError,
    OIDCClient,
    SSOConfig,
    load_sso_config,
)
from ...enterprise.audit import AuditError, ComplianceAuditor
from ...enterprise.policy import (
    EnterprisePolicy,
    EnterprisePolicyError,
    PolicyViolation,
    effective_permission_mode,
    load_enterprise_policy,
)
from ...extensions import ExtensionRegistry, replace_runtime_extensions
from ...hooks import HookRunner, parse_hooks
from ...lsp.manager import LSPManager, LSPOptions
from ...lsp.tools import build_lsp_tools
from ...output_styles import OutputStyleConfig, OutputStyleManager
from ...web.tools import WebOptions, WebRuntime
from ...jobs import ShellJobManager
from ...core.messages import Message, ToolCall
from ...llm.base import LLMProvider, collect
from ...llm.providers.mock import MockProvider
from ...llm.registry import create_provider, resolve_settings
from ...llm.retry import RetryPolicy, RetryingProvider
from ...mcp.supervisor import MCPSupervisor
from ...plugins import PluginManager
from ...plugins.signing import PluginSignatureError
from ...plugins.trust import PluginTrustError, PluginTrustPolicy, PluginTrustStore
from ...safety.policy import (
    AllowAllPolicy,
    EnterpriseGuardPolicy,
    SandboxPolicy,
)
from ...safety.sandbox import ExecutionSandbox, SandboxError
from ...session.autocommit import AutoCommitter
from ...session.checkpoint import CheckpointManager
from ...session.compact_checkpoint import CompactCheckpointStore
from ...session.context_checkpoint import (
    ContextCheckpointCoordinator,
    ContextCheckpointStore,
)
from ...session.commands import (
    sync_custom_commands,
    sync_mcp_prompt_commands,
    sync_plugin_commands,
    sync_skill_commands,
)
from ...session.commands.custom import CustomCommandManager
from ...session.input import InputProcessor
from ...session.elicitation_bridge import MCPElicitationBridge
from ...session.interactions import UserInputBroker
from ...session.memory import MemoryStore, sync_memory_fragment
from ...session.memory_candidates import (
    DEFAULT_AUTO_THRESHOLD,
    DEFAULT_SUGGEST_THRESHOLD,
    AutoMemoryCoordinator,
    AutoMemoryMode,
    coerce_auto_memory_mode,
)
from ...session.restore import (
    refresh_skill_system_prompt,
    restore_session as _restore_session,
)
from ...session.runner import run_session
from ...skills import BUILTIN_SKILL_ROOT, SkillManager, build_skill_tools
from ...subagent.definition import AgentDefinitionManager
from ...telemetry import build_telemetry_runtime
from ...subagent.manager import (
    SubagentManager,
    build_subagent_system_prompt,
)
from ...subagent.model_aliases import resolve_agent_model_spec
from ...contracts.tool import (
    PermissionMode,
    RunMode,
    RuntimeSnapshot,
    Tool,
    ToolContext,
    ToolDescriptor,
    ToolSource,
)
from ...tools.builtin.check_syntax import CheckSyntaxTool
from ...tools.builtin.database_migration import (
    MigrationOptions,
    build_migration_tools,
)
from ...tools.builtin.edit_file import EditFileTool
from ...tools.builtin.glob import GlobTool
from ...tools.builtin.grep import GrepTool
from ...tools.builtin.job_tools import (
    JobKillTool,
    JobListTool,
    JobOutputTool,
    JobStartTool,
)
from ...session.memory_tool import MemorySearchTool
from ...tools.builtin.notebook import NotebookOptions, build_notebook_tools
from ...agent.prompt_lab_tool import PromptLabTool
from ...tools.builtin.read_file import ReadFileTool
from ...tools.builtin.repo_map import RepoMapTool
from ...tools.builtin.run_command import RunCommandTool
from ...tools.builtin.find_refs import FindRefsTool
from ...tools.builtin.time_tool import GetTimeTool
from ...tools.builtin.tool_search import ToolSearchTool
from ...tools.builtin.write_file import WriteFileTool
from ...tools.extension.ask_user import AskUserTool
from ...tools.extension.update_plan import UpdatePlanTool
from ...tools.registry import ToolRegistry
from ...ui.approval import ApprovalBroker
from ...ui.repl import run_repl
from ...ui.status_line import StatusLineOptions, StatusLineRenderer

# Backward-compatible public re-export used by embedders and older tests.
restore_session = _restore_session
