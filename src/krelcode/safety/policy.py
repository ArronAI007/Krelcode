"""ApprovalPolicy 端口与实现。

- AllowAllPolicy：全放行（--no-sandbox / 测试用）
- SandboxPolicy（Step 6）：
  · 只读工具直接放行；文件写工具已被工作区路径约束兜底，放行
  · run_command 走 safety.commands 风险分级：DENY 拒绝 / ALLOW 放行 / ASK 审批
  · ASK：先查持久化 allow 规则 → auto_approve → 交互式 approver → 都没有则拒绝
  · 未知工具保守 ASK
"""

from __future__ import annotations

import fnmatch
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Awaitable, Callable, Mapping, Optional, Protocol, Union,
)

from ..core.events import ApprovalRequested, EventBus
from ..core.messages import ToolCall
from ..core.secret_guard import classify_secret_path
from ..contracts.runtime import PermissionMode
from ..contracts.tool import ToolContext
from .commands import RISK_ALLOW, RISK_DENY, classify_command
from .contract import local_read_only_tool, restricts_to_local_read_only
from .rules import AllowRules, RuleDecision, RuleMatch


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


ALLOW = Decision(True)


def deny(reason: str) -> Decision:
    return Decision(False, reason)


class ApprovalPolicy(Protocol):
    async def check(self, tool_call: ToolCall, ctx: ToolContext) -> Decision:
        ...


class ManagedPolicyPort(Protocol):
    """Enterprise policy surface enforced before local approval."""

    allow_auto_approve: bool

    def deny_tool_reason(
        self,
        tool_call: ToolCall,
        workspace_root: Path,
    ) -> Optional[str]: ...


class AllowAllPolicy:
    """显式关闭安全策略时使用的全放行实现（--no-sandbox / 测试）。"""

    async def check(self, tool_call: ToolCall, ctx: ToolContext) -> Decision:
        return ALLOW


# 工具风险分类（descriptor 缺失时的兼容兜底；正常路径优先使用工具描述符）。
READONLY_TOOLS = {
    "get_time", "read_file", "grep", "glob",
    "find_refs", "repo_map", "check_syntax",
    "ask_user", "update_plan", "job_output", "job_list",
    "list_skills", "load_skill", "list_skill_resources", "read_skill_resource",
    "web_fetch", "web_search",
}
WORKSPACE_WRITE_TOOLS = {"write_file", "edit_file"}
SHELL_TOOLS = {"run_command", "run_skill_script", "job_start"}
JOB_CONTROL_TOOLS = {"job_kill"}

# approver：async (tool_call, reason) -> "yes" | "no" | "always"
Approver = Callable[[ToolCall, str], Union[str, Awaitable[str]]]


class SandboxPolicy:
    """规则分级 + 持久化 allow 规则 + 交互审批（fail-closed）。"""

    def __init__(
        self,
        approver: Optional[Approver] = None,
        rules_path: Optional[Path] = None,
        bus: Optional[EventBus] = None,
        auto_approve: bool = False,
        workspace_root: Optional[Path] = None,
        review_writes: bool = False,
        managed_policy: Optional[ManagedPolicyPort] = None,
    ) -> None:
        self.approver = approver
        self.rules = AllowRules(
            rules_path,
            workspace_root=workspace_root,
        )
        self.bus = bus
        self.auto_approve = auto_approve
        self.review_writes = bool(review_writes)
        # An optional EnterprisePolicy-like object.  It is checked before
        # user allow rules and before every auto-approval path.
        self.managed_policy = managed_policy

    async def check(self, tool_call: ToolCall, ctx: ToolContext) -> Decision:
        name = tool_call.name
        if self.managed_policy is not None:
            reason = self.managed_policy.deny_tool_reason(
                tool_call, ctx.workspace_root)
            if reason:
                return deny(reason)
        descriptor = ctx.tool_descriptor
        local_read_only = bool(
            descriptor is not None
            and local_read_only_tool(descriptor.source, descriptor.read_only)
        )
        rule_match = _combine_rule_matches(
            self.rules.evaluate_tool(name),
            self.rules.evaluate_paths(_tool_paths(tool_call.arguments)),
        )
        if rule_match is not None and rule_match.decision is RuleDecision.DENY:
            return deny(f"权限规则拒绝: {rule_match.reason}")

        restricted = restricts_to_local_read_only(ctx.runtime)
        if restricted and not (local_read_only or name in READONLY_TOOLS):
            return deny(
                "当前 read-only/plan/review 权限模式禁止有副作用的工具: "
                f"{name}"
            )

        if local_read_only or name in READONLY_TOOLS:
            if rule_match is not None and rule_match.decision is RuleDecision.ASK:
                return await self._ask(
                    tool_call, rule_match.reason, ctx=ctx,
                    ignore_rules=True, force_approval=True)
            return ALLOW

        if name in WORKSPACE_WRITE_TOOLS:
            if rule_match is not None and rule_match.decision is RuleDecision.ASK:
                return await self._ask(
                    tool_call, rule_match.reason, ctx=ctx,
                    ignore_rules=True, force_approval=True)
            protected = classify_protected_path(
                str(tool_call.arguments.get("path", "")), ctx.workspace_root)
            if protected:
                return await self._ask(
                    tool_call, f"写入受保护路径（{protected}）", ctx=ctx)
            if rule_match is not None and rule_match.decision is RuleDecision.ALLOW:
                return Decision(True, rule_match.reason)
            if self.review_writes and self.approver is not None:
                return await self._ask(
                    tool_call,
                    "REPL diff review: 文件内容变更需要先审阅再写入",
                    ctx=ctx,
                )
            return ALLOW

        if name in SHELL_TOOLS:
            return await self._check_command(tool_call, ctx, rule_match)

        if name in JOB_CONTROL_TOOLS:
            if rule_match is not None and rule_match.decision is RuleDecision.ASK:
                return await self._ask(
                    tool_call, rule_match.reason, ctx=ctx,
                    ignore_rules=True, force_approval=True)
            return Decision(True, rule_match.reason if rule_match else "受管后台任务控制")

        # 未知/未来新工具：显式 allow 才能免审批，其他情况保守 ASK。
        if rule_match is not None and rule_match.decision is RuleDecision.ALLOW:
            return Decision(True, rule_match.reason)
        reason = (
            rule_match.reason
            if rule_match is not None
            else f"未分级工具 {name}，保守请求审批"
        )
        return await self._ask(
            tool_call, reason, ctx=ctx,
            ignore_rules=rule_match is not None,
            force_approval=(
                rule_match is not None
                and rule_match.decision is RuleDecision.ASK),
        )

    async def _check_command(
        self,
        tool_call: ToolCall,
        ctx: ToolContext,
        rule_match: Optional[RuleMatch] = None,
    ) -> Decision:
        if tool_call.name == "run_skill_script":
            script = str(tool_call.arguments.get("script", ""))
            raw_args = tool_call.arguments.get("args") or []
            suffix = " ".join(str(a) for a in raw_args) if isinstance(raw_args, list) else ""
            command = f"skill-script {script} {suffix}".strip()
        else:
            command = str(tool_call.arguments.get("command", ""))
        if tool_call.name == "job_start" and not command.strip():
            return deny("job_start 缺少 command")
        command_match = _combine_rule_matches(
            rule_match, self.rules.evaluate_command(command))
        if command_match is not None and command_match.decision is RuleDecision.DENY:
            return deny(f"权限规则拒绝: {command_match.reason}")

        sandbox = getattr(ctx, "sandbox", None)
        process_platform = getattr(sandbox, "process_platform", None)
        shell_dialect = getattr(process_platform, "shell_dialect", "posix")
        risk, reason = classify_command(command, shell_dialect=shell_dialect)
        if risk == RISK_DENY:
            return deny(reason)
        if command_match is not None:
            if command_match.decision is RuleDecision.ASK:
                return await self._ask(
                    tool_call,
                    f"{command_match.reason}；风险：{reason}",
                    command=command,
                    ctx=ctx,
                    ignore_rules=True,
                    force_approval=True,
                )
            if command_match.decision is RuleDecision.ALLOW:
                return Decision(True, command_match.reason)
        if risk == RISK_ALLOW:
            return ALLOW
        return await self._ask(tool_call, reason, command=command, ctx=ctx)

    async def _ask(
        self,
        tool_call: ToolCall,
        reason: str,
        command: Optional[str] = None,
        *,
        ctx: Optional[ToolContext] = None,
        ignore_rules: bool = False,
        force_approval: bool = False,
    ) -> Decision:
        # 1) 旧版持久化规则仅在没有显式 ask/更高优先级规则时生效。
        if not ignore_rules and command is not None and self.rules.matches(command):
            return Decision(True, f"命中已存 allow 规则: {reason}")
        # 2) 全自动批准模式
        runtime_auto = bool(
            ctx is not None
            and ctx.runtime.permission_mode is PermissionMode.AUTO_APPROVE
        )
        auto_disabled = (
            self.managed_policy is not None
            and not self.managed_policy.allow_auto_approve
        )
        if not force_approval and not auto_disabled and (
                self.auto_approve or runtime_auto):
            source = "运行时权限模式" if runtime_auto else "--auto-approve"
            return Decision(True, f"{source}自动批准: {reason}")
        # 3) 交互审批
        if self.bus is not None:
            await self.bus.emit(ApprovalRequested(tool_call, reason))
        if self.approver is None:
            return deny(f"非交互模式且无审批器，已拒绝（{reason}）")
        answer = self.approver(tool_call, reason)
        if inspect.isawaitable(answer):
            answer = await answer
        if answer == "always":
            if command is not None:
                self.rules.add(command)
            return Decision(True, f"用户批准（always）: {reason}")
        if answer == "yes":
            return Decision(True, f"用户批准: {reason}")
        return deny(f"用户拒绝（{reason}）")


class EnterpriseGuardPolicy:
    """Delegate normal approval while enforcing managed policy first.

    This wrapper is used for ``--no-sandbox`` too: disabling the local
    Sandbox must never disable organization-level restrictions.
    """

    def __init__(
        self, inner: ApprovalPolicy, managed_policy: ManagedPolicyPort,
    ) -> None:
        self.inner = inner
        self.managed_policy = managed_policy

    def __getattr__(self, name: str) -> object:
        # Keep diagnostics and legacy policy attributes (review_writes,
        # auto_approve, rules) observable without weakening the guard.
        return getattr(self.inner, name)

    async def check(self, tool_call: ToolCall, ctx: ToolContext) -> Decision:
        reason = self.managed_policy.deny_tool_reason(
            tool_call, ctx.workspace_root)
        if reason:
            return deny(reason)
        return await self.inner.check(tool_call, ctx)


def _tool_paths(arguments: Mapping[str, object]) -> tuple[str, ...]:
    """提取需要参与路径规则匹配的参数；未知工具也使用同一安全边界。"""
    values: list[str] = []
    for key in ("path", "file_path"):
        value = arguments.get(key)
        if value not in (None, ""):
            values.append(str(value))
    return tuple(values)


def _combine_rule_matches(
    *matches: Optional[RuleMatch],
) -> Optional[RuleMatch]:
    for decision in (RuleDecision.DENY, RuleDecision.ASK, RuleDecision.ALLOW):
        for match in matches:
            if match is not None and match.decision is decision:
                return match
    return None


# ---------------------------------------------------------------------------
# 受保护路径（写保护 → ASK；非交互场景 fail-closed 拒绝）
# ---------------------------------------------------------------------------

# glob 同时匹配「相对工作区路径」与「文件基名」
PROTECTED_GLOBS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "secrets.*", "credentials*",
    ".npmrc", ".netrc", ".pgpass", ".htpasswd",
    # agent 自身的配置：防 LLM 篡改配置注入 hooks / 改 provider 外传数据
    "krelcode.toml", "krelcode.local.toml",
)
# 目录级保护（路径中任一段命中即保护）：
# - .git：仓库内部（hooks/config 可执行任意代码）
# - .krelcode：审批白名单、检查点、会话、hook 运行时状态——防自我提权与痕迹篡改
PROTECTED_DIRS = (".git", ".krelcode")


def classify_protected_path(raw_path: str, workspace_root: Path) -> Optional[str]:
    """目标路径若受保护，返回原因字符串；否则 None。纯路径计算，不触网。"""
    if not raw_path.strip():
        return None
    root = Path(workspace_root)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        rel = candidate.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        rel = candidate.as_posix()  # 解析失败（如指向工作区外）用字面路径判断
    parts = rel.split("/")
    for part in parts:
        if part in PROTECTED_DIRS:
            return f"{part}/ 目录（agent/仓库内部状态）"
    base = parts[-1]
    for pattern in PROTECTED_GLOBS:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(base, pattern):
            return f"敏感文件模式 {pattern}"
    secret_category = classify_secret_path(raw_path, workspace_root)
    if secret_category is not None:
        return f"凭据文件（{secret_category}）"
    return None
